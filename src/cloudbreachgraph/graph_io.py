"""Load a :class:`~cloudbreachgraph.model.graph.Graph` back from a written output file.

The pipeline's writers are one-way (``output/json_export.py`` / ``output/dot_export.py``);
this is their inverse, so an already-produced ``graph.json`` / ``graph.dot`` can be
re-loaded — chiefly to feed the interactive HTML view (``output/html_export.py``) without
re-collecting from AWS. Used by the ``cloudbreachgraph-to-html`` auxiliary CLI
(``convert.py``).

* **JSON** (``load_json`` / :func:`graph_from_dict`) is a **lossless** round-trip of
  ``Graph.to_dict()`` — every node/edge attribute is restored verbatim.
* **DOT** (``load_dot``) is a **best-effort** parser for *this tool's own* emitted DOT
  (not arbitrary Graphviz). DOT is a lossy rendering, so it recovers what the ``.dot``
  actually encodes: node id, type, name, the public-IP/synthetic flags and the one display
  attribute per type (interface type, LB type, CIDR, instance state), plus every edge and
  its ``match_rule``. Reachability sources (``internet``/``cidr``/``security_group``, §5.5) and
  their ``can_reach`` edges round-trip as ordinary nodes/edges. A **legacy** shared ``Internet``
  decoration (older ``.dot`` output, before per-ENI internet nodes existed) is still folded back
  into ``public_ips`` on the ENI it exposed, so old captures keep matching the model.

Deterministic by construction: :class:`Graph` sorts nodes/edges, so a reloaded graph
serializes byte-for-byte identically to the original.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model.graph import Edge, Graph, Node


class GraphLoadError(Exception):
    """Raised when an input file can't be parsed into a graph."""


# --------------------------------------------------------------------------- #
# JSON  (lossless inverse of Graph.to_dict / json_export)
# --------------------------------------------------------------------------- #
def graph_from_dict(data: dict[str, Any]) -> Graph:
    """Rebuild a :class:`Graph` from a ``Graph.to_dict()`` mapping.

    Raises :class:`GraphLoadError` if the mapping is missing the expected ``nodes``/``edges``
    lists or a node/edge lacks its required keys.
    """
    if not isinstance(data, dict) or "nodes" not in data or "edges" not in data:
        raise GraphLoadError("not a CloudBreachGraph JSON graph (missing 'nodes'/'edges')")
    graph = Graph(meta=dict(data.get("meta") or {}))
    try:
        for n in data["nodes"]:
            graph.add_node(
                Node(
                    id=n["id"],
                    type=n["type"],
                    label=n.get("label", n["id"]),
                    attributes=dict(n.get("attributes") or {}),
                )
            )
        for e in data["edges"]:
            graph.add_edge(
                Edge(
                    source=e["source"],
                    target=e["target"],
                    relationship=e["relationship"],
                    attributes=dict(e.get("attributes") or {}),
                )
            )
    except (KeyError, TypeError) as exc:
        raise GraphLoadError(f"malformed node/edge in JSON graph: {exc}") from exc
    return graph


def load_json(path: str | Path) -> Graph:
    """Load a :class:`Graph` from a ``graph.json`` file (lossless)."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GraphLoadError(f"invalid JSON in {path}: {exc}") from exc
    return graph_from_dict(data)


# --------------------------------------------------------------------------- #
# DOT  (best-effort inverse of dot_export, for this tool's own output)
# --------------------------------------------------------------------------- #
# A DOT string body is `(?:[^"\\]|\\.)*` — any char except a quote/backslash, or an escaped
# pair. Node statements start with a quoted id + `[label="..."`; edges have `->` between two.
_QSTR = r'(?:[^"\\]|\\.)*'
_NODE_RE = re.compile(rf'^\s*"({_QSTR})"\s+\[label="({_QSTR})"(.*)\];\s*$')
_EDGE_RE = re.compile(rf'^\s*"({_QSTR})"\s*->\s*"({_QSTR})"\s+\[label="({_QSTR})"(.*)\];\s*$')
_STYLE_RE = re.compile(r'style="([^"]*)"')
# Identity label line: `<id> [<name>]` when named, else just `<id>`.
_NAMED_RE = re.compile(r"^(.+) \[(.+)\]$")

# Which node type's single bare (unlabeled) attribute line maps to which attribute name.
_BARE_ATTR: dict[str, str] = {
    "eni": "interface_type",
    "load_balancer": "lb_type",
    "nat_gateway": "state",
    "vpc_endpoint": "service_name",
    "subnet": "cidr",
    "vpc": "cidr",
    "ec2_instance": "state",
}


def _dot_unescape(s: str) -> str:
    """Reverse ``dot_export._esc``: ``\\"`` -> ``"`` and ``\\\\`` -> ``\\``."""
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _split_label(raw: str) -> list[str]:
    """Split a DOT label on its ``\\n`` separators and unescape each line."""
    return [_dot_unescape(part) for part in raw.split("\\n")]


def load_dot(path: str | Path) -> Graph:
    """Best-effort load of a :class:`Graph` from *this tool's* ``graph.dot`` output.

    Recovers nodes (id/type/name + public/synthetic flags + the per-type display attribute)
    and edges (relationship + ``match_rule``). The synthetic ``Internet`` node and its
    ``public_ip`` edges are folded back into ``public_ips`` on the source ENIs. Not a general
    Graphviz parser. Raises :class:`GraphLoadError` if nothing graph-like is found.
    """
    graph = Graph(meta={})
    public_from_internet: set[str] = set()
    saw_any = False

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        edge_m = _EDGE_RE.match(line)
        if edge_m:
            src, dst, label = edge_m.group(1), edge_m.group(2), edge_m.group(3)
            src, dst = _dot_unescape(src), _dot_unescape(dst)
            if dst == "Internet":  # DOT-only exposure decoration -> a flag on the ENI
                public_from_internet.add(src)
                continue
            parts = _split_label(label)
            rel = parts[0] if parts else ""
            attrs: dict[str, Any] = {}
            if len(parts) > 1 and parts[1].startswith("(") and parts[1].endswith(")"):
                attrs["match_rule"] = parts[1][1:-1]
            graph.add_edge(Edge(source=src, target=dst, relationship=rel, attributes=attrs))
            saw_any = True
            continue

        node_m = _NODE_RE.match(line)
        if node_m:
            node_id, label, tail = node_m.group(1), node_m.group(2), node_m.group(3)
            node_id = _dot_unescape(node_id)
            if node_id == "Internet":  # DOT-only decoration, not a model node
                continue
            _add_node_from_dot(graph, node_id, _split_label(label), tail)
            saw_any = True

    if not saw_any:
        raise GraphLoadError(f"no CloudBreachGraph nodes or edges found in {path}")

    for eni_id in public_from_internet:  # merge the Internet-derived exposure flag
        node = graph.get_node(eni_id)
        if node is not None and not node.attributes.get("public_ips"):
            node.attributes.setdefault("public_ips", ["(exposed)"])
    return graph


def _add_node_from_dot(graph: Graph, node_id: str, lines: list[str], tail: str) -> None:
    """Turn one parsed DOT node (label lines + attribute tail) into a graph :class:`Node`."""
    node_type = ""
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        node_type = lines[0][1:-1]

    label = node_id
    if len(lines) > 1:
        named = _NAMED_RE.match(lines[1])
        if named:
            # `<id> [<name>]` -> keep the name as the label (id already came from the quoted id)
            label = named.group(2)

    attrs: dict[str, Any] = {}
    style_m = _STYLE_RE.search(tail)
    if style_m and "dashed" in style_m.group(1):
        attrs["synthetic"] = True

    for extra in lines[2:]:
        if extra == "(unresolved)":
            attrs["synthetic"] = True
        elif extra.startswith("Private IP: "):
            attrs["private_ips"] = [p.strip() for p in extra[len("Private IP: ") :].split(",")]
        elif extra.startswith("Public IP: "):
            attrs["public_ips"] = [p.strip() for p in extra[len("Public IP: ") :].split(",")]
        elif node_type in _BARE_ATTR:
            attrs.setdefault(_BARE_ATTR[node_type], extra)

    graph.add_node(Node(id=node_id, type=node_type, label=label, attributes=attrs))


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def load_graph(path: str | Path, fmt: str = "auto") -> Graph:
    """Load a graph from ``path``, choosing the loader by ``fmt`` or the file extension.

    ``fmt`` is ``"auto"`` (default; ``.json`` -> JSON, ``.dot``/``.gv`` -> DOT), ``"json"``,
    or ``"dot"``. Raises :class:`GraphLoadError` on an unknown/unsupported format or a file
    that can't be read.
    """
    p = Path(path)
    if fmt == "auto":
        suffix = p.suffix.lower()
        if suffix == ".json":
            fmt = "json"
        elif suffix in (".dot", ".gv"):
            fmt = "dot"
        else:
            raise GraphLoadError(
                f"cannot infer format from '{p.name}': use --format json|dot "
                f"(expected a .json or .dot extension)"
            )
    try:
        if fmt == "json":
            return load_json(p)
        if fmt == "dot":
            return load_dot(p)
    except FileNotFoundError as exc:
        raise GraphLoadError(f"file not found: {p}") from exc
    raise GraphLoadError(f"unsupported format: {fmt!r} (expected 'json' or 'dot')")
