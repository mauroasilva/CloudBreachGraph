"""``cloudbreachgraph-merge`` — enrich an existing ``graph.json`` from offline sources.

An auxiliary CLI (separate console entry point from the main ``cloudbreachgraph``) that takes a
graph already written by a previous run — ``graph.json`` — and **merges in** what a live run could
not reconstruct, from **either or both** of two offline, AWS-free sources:

* ``--data PATH`` — a user **data file** describing ENIs the tool couldn't resolve: each ENI's real
  private IP(s), its owner node (an EC2 instance / NAT gateway / …) and its Auto Scaling group.
* ``--cloudtrail PATH`` — a file of **older CloudTrail ``lookup-events``** (create/run/delete/
  terminate) reaching further into the past than the live 90-day window, reconstructed through the
  same shared parser the live collector uses (``aws/cloudtrail_enis.py::enis_from_events``) — so
  there is exactly one reconstruction.

Both may be given **in tandem**; either may be omitted. For every ENI the sources confirm, the
merge:

* **adds or upgrades** its ``eni`` node — a matching ``unrecognised`` node (from the flow-log pass,
  ``mapping/flowlogs.py``) has its guessed ``inferred_private_ips`` replaced by the confirmed
  ``private_ips`` and its ``unrecognised`` / ``needs_review`` flags cleared;
* **absorbs the guesses it displaces** — an external ``flow_peer`` node, or a ``/32`` ``cidr``
  reachability source, whose IP the ENI actually owns is removed and its edges re-pointed to the ENI
  (a guess becomes a confirmed ENI↔ENI / source→ENI edge);
* **attaches the owner** (``attached_to``) and **ASG membership** (``asg_name`` on the ENI + its
  instance, ready for ``--collapse-asgs``), placing the ENI in its subnet/VPC when given.

``--template`` emits a skeleton **data file** listing the graph's ``needs_review`` ENIs (with the
inferred guesses echoed as a hint), ready to fill in and feed back via ``--data``.

Read-only and AWS-free (local file I/O only); stdlib only; output round-trips through the model's
deterministic :func:`~cloudbreachgraph.output.json_export.write_json` like every other writer.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .aws.cloudtrail_enis import enis_from_events
from .graph_io import GraphLoadError, load_graph
from .model.graph import Edge, Graph, Node
from .output.json_export import write_json

_DEFAULT_OUTPUT = "merged_graph.json"


class MergeError(Exception):
    """A source file can't be read or isn't the expected shape."""


# --------------------------------------------------------------------------- #
# The resolved-ENI intermediate — one confirmed ENI, however it was learned
# --------------------------------------------------------------------------- #
@dataclass
class _ResolvedEni:
    """One ENI confirmed from a source (data file or CloudTrail), before it is merged in."""

    id: str
    private_ips: list[str] = field(default_factory=list)
    owner: dict[str, str] | None = None  # {"id", "type", "name"}
    asg: str | None = None
    subnet_id: str | None = None
    vpc_id: str | None = None
    name: str | None = None
    description: str | None = None
    source: str = "data_file"


def _informative(r: _ResolvedEni) -> bool:
    """Whether a resolved ENI carries anything to merge (so blank template rows are skipped)."""
    return bool(r.private_ips or r.owner or r.asg or r.subnet_id)


def _fold(resolved: dict[str, _ResolvedEni], r: _ResolvedEni) -> None:
    """Fold one resolved ENI into the by-id map; a later source's non-null fields override.

    Private IPs union (first-seen order); scalar fields take the incoming value when it is set,
    so ``--data`` (folded after ``--cloudtrail``) is the operator's override for an ENI CloudTrail
    also described."""
    ex = resolved.get(r.id)
    if ex is None:
        resolved[r.id] = r
        return
    for ip in r.private_ips:
        if ip not in ex.private_ips:
            ex.private_ips.append(ip)
    ex.owner = r.owner or ex.owner
    ex.asg = r.asg or ex.asg
    ex.subnet_id = r.subnet_id or ex.subnet_id
    ex.vpc_id = r.vpc_id or ex.vpc_id
    ex.name = r.name or ex.name
    if r.description is not None:
        ex.description = r.description
    ex.source = r.source


# --------------------------------------------------------------------------- #
# Parsing the two sources into resolved ENIs
# --------------------------------------------------------------------------- #
def _resolved_from_cloudtrail(raw: Any) -> list[_ResolvedEni]:
    """Reconstruct ENIs from a CloudTrail file: a ``{"Events": [...]}`` object or a bare list.

    Uses the shared :func:`~cloudbreachgraph.aws.cloudtrail_enis.enis_from_events` parser, then maps
    each reconstructed record to a resolved ENI (its instance becomes an ``ec2_instance`` owner)."""
    if isinstance(raw, dict) and isinstance(raw.get("Events"), list):
        events = raw["Events"]
    elif isinstance(raw, list):
        events = raw
    else:
        raise MergeError(
            "cloudtrail file must be a CloudTrail lookup-events object ('Events': [...]) "
            "or a list of events"
        )
    out: list[_ResolvedEni] = []
    for rec in enis_from_events(events):
        eni_id = rec.get("NetworkInterfaceId")
        if not eni_id:
            continue
        owner = None
        if rec.get("InstanceId"):
            owner = {
                "id": rec["InstanceId"],
                "type": "ec2_instance",
                "name": rec.get("Name") or "",
            }
        out.append(
            _ResolvedEni(
                id=eni_id,
                private_ips=[ip for ip in (rec.get("PrivateIpAddresses") or []) if ip],
                owner=owner,
                asg=rec.get("AsgName"),
                subnet_id=rec.get("SubnetId"),
                vpc_id=rec.get("VpcId"),
                name=rec.get("Name"),
                description=rec.get("Description"),
                source="cloudtrail",
            )
        )
    return out


def _resolved_from_data(data: Any) -> list[_ResolvedEni]:
    """Parse a user data file: ``{"enis": [{id, private_ips, owner, asg, subnet_id, vpc_id, …}]}``.

    Leniently ignores blank/malformed rows (a template row left untouched contributes nothing) and
    unknown keys (e.g. the ``inferred_private_ips`` hint the template echoes back)."""
    entries = data.get("enis") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise MergeError("data file must be an object with an 'enis' list")
    out: list[_ResolvedEni] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eni_id = entry.get("id")
        if not eni_id:
            continue
        owner = entry.get("owner")
        owner_norm: dict[str, str] | None = None
        if isinstance(owner, dict) and owner.get("id"):
            owner_norm = {
                "id": owner["id"],
                "type": owner.get("type") or "ec2_instance",
                "name": owner.get("name") or "",
            }
        out.append(
            _ResolvedEni(
                id=eni_id,
                private_ips=[ip for ip in (entry.get("private_ips") or []) if ip],
                owner=owner_norm,
                asg=entry.get("asg") or None,
                subnet_id=entry.get("subnet_id") or None,
                vpc_id=entry.get("vpc_id") or None,
                name=entry.get("name") or None,
                description=entry.get("description"),
                source="data_file",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# The merge itself — a deterministic view transform over the loaded graph
# --------------------------------------------------------------------------- #
def enrich(graph: Graph, resolved: dict[str, _ResolvedEni]) -> Graph:
    """Return a copy of ``graph`` enriched by the resolved ENIs (see the module docstring).

    Deterministic and idempotent-ish: with no informative resolved ENIs the graph is returned
    unchanged (byte-for-byte)."""
    active = {i: r for i, r in resolved.items() if _informative(r)}
    if not active:
        return graph

    # A confirmed ENI reclaims any external guess for one of its IPs: an existing ``flow_peer`` /
    # ``/32`` ``cidr`` node with that IP is absorbed into the ENI (its edges re-pointed).
    existing_ids = {n.id for n in graph.nodes}
    remap: dict[str, str] = {}
    for eni_id, r in sorted(active.items()):
        for ip in r.private_ips:
            for cand in (f"flow-peer:{ip}", f"cidr:{ip}/32"):
                if cand in existing_ids and cand != eni_id and cand not in remap:
                    remap[cand] = eni_id

    out = Graph(meta=dict(graph.meta))
    for n in graph.nodes:
        if n.id in remap:
            continue  # absorbed into an ENI
        if n.id in active:
            out.add_node(_confirmed_eni_node(n, active[n.id]))
        else:
            out.add_node(Node(n.id, n.type, n.label, dict(n.attributes)))
    # Resolved ENIs that had no node yet (incl. one present only as an absorbed peer/cidr guess).
    for eni_id, r in sorted(active.items()):
        if out.get_node(eni_id) is None:
            out.add_node(_confirmed_eni_node(None, r))
    # Copy edges, re-pointing absorbed guesses; drop self-loops, dedup on (src, tgt, rel).
    for e in graph.edges:
        src = remap.get(e.source, e.source)
        tgt = remap.get(e.target, e.target)
        if src == tgt:
            continue
        out.add_edge(Edge(src, tgt, e.relationship, dict(e.attributes)))
    # Attach owners, ASG membership and subnet/VPC placement.
    for eni_id, r in sorted(active.items()):
        _attach_owner_and_placement(out, eni_id, r)
    return out


def _confirmed_eni_node(existing: Node | None, r: _ResolvedEni) -> Node:
    """Build the confirmed ``eni`` node, upgrading a matching ``unrecognised`` node in place.

    Confirmed ``private_ips`` supersede the guess: ``inferred_private_ips`` and the
    ``unrecognised`` / ``needs_review`` flags are dropped, and ``origin`` records which source
    confirmed it."""
    if existing is not None:
        attrs = dict(existing.attributes)
        label = existing.label
        eni_id = existing.id
    else:
        attrs = {}
        label = r.name or r.id
        eni_id = r.id
    attrs["private_ips"] = sorted(set(attrs.get("private_ips") or []) | set(r.private_ips))
    attrs.pop("inferred_private_ips", None)
    attrs.pop("unrecognised", None)
    attrs.pop("needs_review", None)
    attrs["origin"] = r.source
    if r.asg:
        attrs["asg_name"] = r.asg
    if r.description is not None:
        attrs.setdefault("description", r.description)
    if r.name:
        label = r.name
    return Node(eni_id, "eni", label, attrs)


def _attach_owner_and_placement(out: Graph, eni_id: str, r: _ResolvedEni) -> None:
    """Add the resolved ENI's owner (``attached_to``), ASG membership and subnet/VPC edges."""
    if r.owner:
        owner_id = r.owner["id"]
        owner_type = r.owner["type"]
        owner_name = r.owner.get("name")
        node = out.get_node(owner_id)
        if node is None:
            attrs: dict[str, Any] = {}
            if r.asg and owner_type == "ec2_instance":
                attrs["asg_name"] = r.asg
            out.add_node(Node(owner_id, owner_type, owner_name or owner_id, attrs))
        elif r.asg and owner_type == "ec2_instance":
            node.attributes.setdefault("asg_name", r.asg)
        out.add_edge(Edge(eni_id, owner_id, "attached_to", {"match_rule": f"merge_{r.source}"}))
    if r.subnet_id:
        _ensure_node(out, r.subnet_id, "subnet")
        out.add_edge(Edge(eni_id, r.subnet_id, "in_subnet"))
        if r.vpc_id:
            _ensure_node(out, r.vpc_id, "vpc")
            out.add_edge(Edge(r.subnet_id, r.vpc_id, "in_vpc"))


def _ensure_node(out: Graph, node_id: str, node_type: str) -> None:
    """Add a minimal node of ``node_type`` when the graph has none yet for ``node_id``."""
    if out.get_node(node_id) is None:
        out.add_node(Node(node_id, node_type, node_id, {}))


# --------------------------------------------------------------------------- #
# Template — a skeleton data file of the graph's needs_review ENIs
# --------------------------------------------------------------------------- #
def build_template(graph: Graph) -> dict[str, Any]:
    """A skeleton ``--data`` file listing every ``needs_review`` (unrecognised) ENI to fill in.

    Each row echoes the graph's ``inferred_private_ips`` as a hint and leaves ``private_ips`` /
    ``owner`` / ``asg`` / ``subnet_id`` / ``vpc_id`` blank for the operator. Deterministic (nodes
    are already id-sorted within their type)."""
    enis: list[dict[str, Any]] = []
    for n in graph.nodes:
        if n.type != "eni":
            continue
        if not (n.attributes.get("needs_review") or n.attributes.get("unrecognised")):
            continue
        enis.append(
            {
                "id": n.id,
                "inferred_private_ips": n.attributes.get("inferred_private_ips") or [],
                "private_ips": [],
                "owner": {"id": "", "type": "ec2_instance", "name": ""},
                "asg": "",
                "subnet_id": "",
                "vpc_id": "",
            }
        )
    return {"enis": enis}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_json_file(path: str) -> Any:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MergeError(f"file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise MergeError(f"invalid JSON in {p}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloudbreachgraph-merge",
        description="Merge an existing CloudBreachGraph graph.json with a user data file and/or a "
        "file of older CloudTrail logs into an enriched graph.json. Local only — no AWS calls.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("input", metavar="FILE", help="path to a graph.json file")
    p.add_argument(
        "--data",
        metavar="PATH",
        help='a user data file: {"enis": [{id, private_ips, owner, asg, subnet_id, vpc_id}]}',
    )
    p.add_argument(
        "--cloudtrail",
        metavar="PATH",
        help="a file of older CloudTrail lookup-events ('Events': [...] or a bare list) for the "
        "ENIs to reconstruct",
    )
    p.add_argument(
        "--template",
        action="store_true",
        help="instead of merging, emit a skeleton --data file of the graph's needs_review ENIs "
        "(to -o, else stdout) and exit",
    )
    p.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help=f"output path (merge default: {_DEFAULT_OUTPUT} beside the input; --template: stdout)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    in_path = Path(args.input)

    try:
        graph = load_graph(in_path, fmt="json")
    except GraphLoadError as exc:
        print(f"cloudbreachgraph-merge: {exc}", file=sys.stderr)
        return 2

    if args.template:
        text = json.dumps(build_template(graph), indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            sys.stdout.write(text)
        return 0

    if not args.data and not args.cloudtrail:
        print(
            "cloudbreachgraph-merge: nothing to do — give --data and/or --cloudtrail "
            "(or --template to emit a skeleton data file)",
            file=sys.stderr,
        )
        return 2

    resolved: dict[str, _ResolvedEni] = {}
    try:
        if args.cloudtrail:
            for r in _resolved_from_cloudtrail(_load_json_file(args.cloudtrail)):
                _fold(resolved, r)
        if args.data:
            for r in _resolved_from_data(_load_json_file(args.data)):
                _fold(resolved, r)
    except MergeError as exc:
        print(f"cloudbreachgraph-merge: {exc}", file=sys.stderr)
        return 2

    merged = enrich(graph, resolved)
    out_path = Path(args.output) if args.output else in_path.parent / _DEFAULT_OUTPUT
    written = write_json(merged, out_path)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
