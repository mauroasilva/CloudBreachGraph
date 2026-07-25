"""Graphviz DOT export — render a :class:`~cloudbreachgraph.model.graph.Graph`.

``write_dot(graph, path)`` emits plain-text Graphviz DOT (``docs/02_architecture.md §7``):

* **Nodes are colored/shaped by type** (``eni``, ``ec2_instance``, ``load_balancer``,
  ``subnet``, ``vpc``); ``synthetic``/``unresolved`` placeholders get a dashed outline.
* **Subnets and ENIs are grouped inside their VPC** using ``subgraph cluster_*`` so the
  layout visually nests reachability inside each VPC. EC2 instances and load balancers are
  clustered too when their ``vpc_id`` is known; anything without a resolvable VPC is drawn
  at the top level.
* **Edges are labeled by relationship** (``in_subnet``/``in_vpc``/``attached_to``/``secured_by``
  and the reachability ``*_can_reach`` family), load-balancer attachment edges additionally show
  their ``match_rule``, and reachability edges show the protocol/port range that reaches the ENI.
* **Reachability sources** (``internet``/``cidr`` nodes, §5.5) link to what they can reach. With
  security groups **shown** (default) each ENI links to its ``security_group`` nodes
  (``secured_by``, clustered inside their VPC) and sources link to the SG; **hidden**
  (``--no-security-groups``) the sources link straight to the ENIs with the routability split.
  In the hidden view the reachability edge's **routability** (§5.6) is colored:
  ``routable_can_reach`` solid red (a real path exists), ``not_routable_can_reach`` grey dashed
  (allowed but no route), plain ``can_reach`` default (undetermined).

``render(dot_path, fmt)`` optionally rasterizes the ``.dot`` with the system ``dot`` binary
(``dot -T<fmt>``) — but only if ``dot`` is on ``PATH``. It returns ``None`` when ``dot`` is
absent so the CLI can degrade gracefully (the ``.dot`` is always written regardless). No
third-party dependency is ever required (``docs/04_conventions.md``); the optional
``graphviz`` Python package is unrelated to this text emitter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..model.graph import Edge, Graph, Node

# Fill color + shape per node type. Kept deliberately light so labels stay readable.
_TYPE_STYLE: dict[str, tuple[str, str]] = {
    "vpc": ("#E8EAF6", "box"),  # cluster boxes (indigo tint)
    "subnet": ("#E3F2FD", "box"),  # light blue
    "eni": ("#E8F5E9", "ellipse"),  # light green
    "ec2_instance": ("#FFF3E0", "box3d"),  # light orange
    "load_balancer": ("#F3E5F5", "component"),  # light purple
    # NAT gateways / VPC endpoints share the load balancer's role class (network in/out of the
    # VPC), so they use the ``component`` shape too, in their own distinct fills (§5.4).
    "nat_gateway": ("#E0F2F1", "component"),  # teal
    "vpc_endpoint": ("#E8EAF6", "component"),  # indigo tint
    # Reachability sources (docs/02_architecture.md §5.5) — who can reach an ENI.
    "internet": ("#FFEBEE", "doubleoctagon"),  # the whole internet (0.0.0.0/0 / ::/0), per-ENI
    "cidr": ("#FFF8E1", "note"),  # a specific source CIDR
    "security_group": ("#FCE4EC", "tab"),  # a referencing security group
    # Flow logs (docs/02_architecture.md §5.7) — config, destinations, observed connection peers.
    "flow_log": ("#E1F5FE", "cds"),  # a VPC flow-log configuration
    "log_group": ("#E0F7FA", "cylinder"),  # CloudWatch Logs destination
    "log_bucket": ("#FFF3E0", "folder"),  # S3 destination
    "flow_peer": ("#ECEFF1", "hexagon"),  # an external address observed in the flow logs
}
_DEFAULT_STYLE = ("#FFFFFF", "box")


def _esc(value: object) -> str:
    """Escape a value for use inside a double-quoted DOT string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _label(lines: list[str]) -> str:
    """Join label lines with a DOT line break (``\\n``), each line escaped."""
    return "\\n".join(_esc(line) for line in lines if line not in (None, ""))


def _cluster_id(vpc_id: str) -> str:
    """A DOT-safe identifier for a ``subgraph cluster_*`` from a VPC id."""
    safe = "".join(c if c.isalnum() else "_" for c in vpc_id)
    return f"cluster_{safe}"


def _node_vpc(
    node: Node,
    subnet_of_eni: dict[str, str],
    vpc_of_subnet: dict[str, str],
) -> str | None:
    """Which VPC a node's *cluster* is, for grouping (``None`` -> draw at top level).

    A ``vpc`` node is **not** placed inside its own cluster — it is its own top-level node
    that the subnets in the cluster connect up to via ``in_vpc`` edges. The cluster groups
    the VPC's *contents* (subnets, ENIs, and — when known — instances/LBs), not the VPC
    node itself.
    """
    if node.type == "vpc":
        return None
    if node.type == "subnet":
        return vpc_of_subnet.get(node.id) or node.attributes.get("vpc_id")
    if node.type == "eni":
        subnet = subnet_of_eni.get(node.id)
        return vpc_of_subnet.get(subnet) if subnet else None
    if node.type in (
        "ec2_instance",
        "load_balancer",
        "nat_gateway",
        "vpc_endpoint",
        "security_group",
    ):
        # These are all VPC-scoped; cluster them with their VPC when known (§5.4/§5.5).
        return node.attributes.get("vpc_id")
    return None


def _node_lines(node: Node) -> list[str]:
    """The human-readable label lines for a node (type + identity + a key attribute).

    The identity line is the AWS id, annotated with the ``Name`` tag when the node has one:
    ``"<aws-id> [<name>]"`` if named, else just ``"<aws-id>"``. (``node.label`` is the name
    when present, otherwise a copy of ``node.id`` — so ``label == id`` means "no name".)
    """
    named = node.label not in ("", node.id)
    identity = f"{node.id} [{node.label}]" if named else node.id
    lines = [f"[{node.type}]", identity]
    attrs = node.attributes
    if node.type == "eni":
        if attrs.get("interface_type"):
            lines.append(str(attrs["interface_type"]))
        if attrs.get("private_ips"):
            lines.append("Private IP: " + ", ".join(attrs["private_ips"]))
        if attrs.get("public_ips"):
            lines.append("Public IP: " + ", ".join(attrs["public_ips"]))
        if attrs.get("ip_allocations"):  # IP history (§5.7) — earliest allocation time
            allocated = [
                a["allocated_at"] for a in attrs["ip_allocations"] if a.get("allocated_at")
            ]
            if allocated:
                lines.append("IP since: " + min(allocated))
    elif node.type == "load_balancer" and attrs.get("lb_type"):
        lines.append(str(attrs["lb_type"]))
    elif node.type == "nat_gateway":
        if attrs.get("state"):
            lines.append(str(attrs["state"]))
        if attrs.get("public_ips"):
            lines.append("Public IP: " + ", ".join(attrs["public_ips"]))
    elif node.type == "vpc_endpoint" and attrs.get("service_name"):
        lines.append(str(attrs["service_name"]))
    elif node.type == "subnet" and attrs.get("cidr"):
        lines.append(str(attrs["cidr"]))
    elif node.type == "vpc" and attrs.get("cidr"):
        lines.append(str(attrs["cidr"]))
    elif node.type == "ec2_instance" and attrs.get("state"):
        lines.append(str(attrs["state"]))
    elif node.type == "flow_log":
        detail = ", ".join(
            str(attrs[k]) for k in ("traffic_type", "destination_type") if attrs.get(k)
        )
        if detail:
            lines.append(detail)
    if attrs.get("synthetic"):
        lines.append("(unresolved)")
    return lines


def _node_stmt(node: Node) -> str:
    """A single ``"id" [ ... ];`` DOT node statement."""
    fill, shape = _TYPE_STYLE.get(node.type, _DEFAULT_STYLE)
    style = "filled,dashed" if node.attributes.get("synthetic") else "filled"
    return (
        f'"{_esc(node.id)}" [label="{_label(_node_lines(node))}", '
        f'shape={shape}, style="{style}", fillcolor="{fill}"];'
    )


def _edge_stmt(edge: Edge) -> str:
    """A single ``"src" -> "dst" [label=...];`` DOT edge statement."""
    lines = [edge.relationship]
    match_rule = edge.attributes.get("match_rule")
    if match_rule:
        lines.append(f"({match_rule})")
    ports = edge.attributes.get("ports")
    if ports:  # reachability edges annotate the protocol/port range that reaches
        lines.append(str(ports))
    return (
        f'"{_esc(edge.source)}" -> "{_esc(edge.target)}" '
        f'[label="{_label(lines)}"{_edge_extra(edge)}];'
    )


def _edge_extra(edge: Edge) -> str:
    """Extra DOT edge attributes by relationship: containment edges dashed; reachability edges
    colored by routability so *routable* exposure stands out from a merely *allowed* rule."""
    rel = edge.relationship
    if rel in ("in_subnet", "in_vpc"):
        return ', style="dashed"'
    if rel == "secured_by":  # ENI -> its security group (membership)
        return ', style="dashed", color="#7E57C2"'
    if rel == "routable_can_reach":  # allowed AND routed -> real reachability
        return ', color="#E53935", penwidth=1.5'
    if rel == "not_routable_can_reach":  # allowed but no route -> muted, dashed
        return ', style="dashed", color="#9E9E9E"'
    if rel == "connects_to":  # an observed flow-log connection (§5.7) -> solid blue
        return ', color="#1E88E5", penwidth=1.3'
    if rel in ("logs_to", "delivers_to"):  # flow-log config plumbing -> dotted
        return ', style="dotted", color="#00838F"'
    return ""


def _dot_lines(graph: Graph) -> list[str]:
    subnet_of_eni = {e.source: e.target for e in graph.edges if e.relationship == "in_subnet"}
    vpc_of_subnet = {e.source: e.target for e in graph.edges if e.relationship == "in_vpc"}

    # Group each VPC's *contents* (subnets/ENIs/instances/LBs) into a cluster; VPC nodes
    # themselves fall into the ``None`` bucket and render at the top level as their own
    # nodes. Insertion order follows the already-sorted node list, so the DOT is
    # deterministic.
    by_vpc: dict[str | None, list[Node]] = {}
    vpc_names: dict[str, str] = {}
    for node in graph.nodes:
        if node.type == "vpc":
            vpc_names[node.id] = node.label
        vpc = _node_vpc(node, subnet_of_eni, vpc_of_subnet)
        by_vpc.setdefault(vpc, []).append(node)

    out: list[str] = [
        "digraph cloudbreachgraph {",
        "  rankdir=LR;",
        "  compound=true;",
        '  node [fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9, color="#607D8B"];',
    ]

    # One cluster per VPC, grouping that VPC's contents (deterministic order by VPC id).
    # The cluster is labeled "VPC <name>" for context; the VPC itself is a separate
    # top-level node (below) that these subnets connect to via in_vpc edges.
    for vpc_id in sorted(k for k in by_vpc if k is not None):
        members = by_vpc[vpc_id]
        label = _esc(f"VPC {vpc_names.get(vpc_id, vpc_id)}")
        out.append(f"  subgraph {_cluster_id(vpc_id)} {{")
        out.append(f'    label="{label}";')
        out.append('    style="rounded";')
        out.append('    color="#9FA8DA";')
        out.append('    labeljust="l";')
        for node in members:
            out.append(f"    {_node_stmt(node)}")
        out.append("  }")

    # Nodes with no resolvable VPC: draw at the top level.
    for node in by_vpc.get(None, []):
        out.append(f"  {_node_stmt(node)}")

    # Edges (already deterministically sorted by the Graph). Reachability (``can_reach``) edges
    # and their internet/CIDR/security-group source nodes are ordinary graph nodes/edges now
    # (docs/02_architecture.md §5.5), so they render through the loops above — no special case.
    for edge in graph.edges:
        out.append(f"  {_edge_stmt(edge)}")

    out.append("}")
    return out


def write_dot(graph: Graph, path: str | Path) -> Path:
    """Write ``graph`` to ``path`` as Graphviz DOT. Returns the :class:`~pathlib.Path`."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(_dot_lines(graph)) + "\n", encoding="utf-8")
    return out


def dot_available() -> bool:
    """``True`` if the Graphviz ``dot`` binary is on ``PATH``."""
    return shutil.which("dot") is not None


def render(dot_path: str | Path, fmt: str) -> Path | None:
    """Rasterize ``dot_path`` to ``<stem>.<fmt>`` via ``dot -T<fmt>``.

    Returns the output path on success, or ``None`` when the ``dot`` binary is not on
    ``PATH`` (so callers can warn and continue — the ``.dot`` file is still valid). Raises
    :class:`RuntimeError` if ``dot`` is present but the render itself fails, surfacing its
    stderr.
    """
    if not dot_available():
        return None
    src = Path(dot_path)
    out = src.with_suffix(f".{fmt}")
    proc = subprocess.run(
        ["dot", f"-T{fmt}", str(src), "-o", str(out)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"dot failed to render {src} (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )
    return out
