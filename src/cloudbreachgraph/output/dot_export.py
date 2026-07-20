"""Graphviz DOT export — render a :class:`~cloudbreachgraph.model.graph.Graph`.

``write_dot(graph, path)`` emits plain-text Graphviz DOT (``docs/02_architecture.md §7``):

* **Nodes are colored/shaped by type** (``eni``, ``ec2_instance``, ``load_balancer``,
  ``subnet``, ``vpc``); ``synthetic``/``unresolved`` placeholders get a dashed outline.
* **Subnets and ENIs are grouped inside their VPC** using ``subgraph cluster_*`` so the
  layout visually nests reachability inside each VPC. EC2 instances and load balancers are
  clustered too when their ``vpc_id`` is known; anything without a resolvable VPC is drawn
  at the top level.
* **Edges are labeled by relationship** (``in_subnet``/``in_vpc``/``attached_to``), and
  load-balancer attachment edges additionally show their ``match_rule``.

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
    """Which VPC a node belongs to, for clustering (``None`` -> draw at top level)."""
    if node.type == "vpc":
        return node.id
    if node.type == "subnet":
        return vpc_of_subnet.get(node.id) or node.attributes.get("vpc_id")
    if node.type == "eni":
        subnet = subnet_of_eni.get(node.id)
        return vpc_of_subnet.get(subnet) if subnet else None
    if node.type in ("ec2_instance", "load_balancer"):
        return node.attributes.get("vpc_id")
    return None


def _node_lines(node: Node) -> list[str]:
    """The human-readable label lines for a node (type + label + a key attribute)."""
    lines = [f"[{node.type}]", node.label]
    attrs = node.attributes
    if node.type == "eni" and attrs.get("interface_type"):
        lines.append(str(attrs["interface_type"]))
    elif node.type == "load_balancer" and attrs.get("lb_type"):
        lines.append(str(attrs["lb_type"]))
    elif node.type == "subnet" and attrs.get("cidr"):
        lines.append(str(attrs["cidr"]))
    elif node.type == "vpc" and attrs.get("cidr"):
        lines.append(str(attrs["cidr"]))
    elif node.type == "ec2_instance" and attrs.get("state"):
        lines.append(str(attrs["state"]))
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
    dashed = ', style="dashed"' if edge.relationship in ("in_subnet", "in_vpc") else ""
    return f'"{_esc(edge.source)}" -> "{_esc(edge.target)}" [label="{_label(lines)}"{dashed}];'


def _dot_lines(graph: Graph) -> list[str]:
    subnet_of_eni = {e.source: e.target for e in graph.edges if e.relationship == "in_subnet"}
    vpc_of_subnet = {e.source: e.target for e in graph.edges if e.relationship == "in_vpc"}

    # Group nodes by their owning VPC (None -> top level). Insertion order follows the
    # already-sorted node list, so the emitted DOT is deterministic.
    by_vpc: dict[str | None, list[Node]] = {}
    vpc_labels: dict[str, list[str]] = {}
    for node in graph.nodes:
        if node.type == "vpc":
            vpc_labels[node.id] = _node_lines(node)
        vpc = _node_vpc(node, subnet_of_eni, vpc_of_subnet)
        by_vpc.setdefault(vpc, []).append(node)

    out: list[str] = [
        "digraph cloudbreachgraph {",
        "  rankdir=LR;",
        "  compound=true;",
        '  node [fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9, color="#607D8B"];',
    ]

    # One cluster per VPC (deterministic order by VPC id).
    for vpc_id in sorted(k for k in by_vpc if k is not None):
        members = by_vpc[vpc_id]
        label = _label(vpc_labels.get(vpc_id, ["[vpc]", vpc_id]))
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

    # Edges (already deterministically sorted by the Graph).
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
