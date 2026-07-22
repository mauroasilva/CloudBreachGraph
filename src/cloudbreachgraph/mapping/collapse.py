"""Collapse the security-group layer of an already-built graph (a view transform).

The builder produces either shape natively (``build_graph(show_security_groups=...)``), but the
``cloudbreachgraph-to-html`` converter starts from a *finished* ``graph.json`` / ``graph.dot`` and
can only rewrite what is already there. This module provides that rewrite: given a graph where
security groups are **shown** (``ENI ─secured_by→ SG ←can_reach─ source``), it returns an
equivalent graph where the SG intermediary is removed and the **IP sources are brought forward**,
connected straight to the ENIs (``docs/02_architecture.md §5.5``):

* an ``internet:<sg>`` source becomes a per-ENI ``internet:<eni>`` node;
* a ``cidr`` source connects directly to each ENI the SG secured;
* a **peer security group** source is expanded to the private IPs of *its* member ENIs
  (each a ``/32`` ``cidr`` node), mirroring the builder's ``--no-security-groups`` mode.

Because the graph carries no route-table data, the collapsed edges are plain ``can_reach`` (no
routable / not-routable split — that verdict only exists when the builder runs with the route
tables). A graph that already has no security-group nodes is returned unchanged.
"""

from __future__ import annotations

from ..model.graph import Edge, Graph, Node

_STRUCTURAL_RELS = frozenset({"in_subnet", "in_vpc", "attached_to"})
_REACH_RELS = frozenset({"can_reach", "routable_can_reach", "not_routable_can_reach"})


def collapse_security_groups(graph: Graph) -> Graph:
    """Return a copy of ``graph`` with the security-group layer collapsed (see module docstring)."""
    nodes = {n.id: n for n in graph.nodes}
    sg_ids = {n.id for n in graph.nodes if n.type == "security_group"}
    # Only a graph built with SGs shown has the ``secured_by`` membership this collapse relies on.
    # Without it there is no SG layer to collapse — the graph is already collapsed, or is an
    # older/foreign shape whose reachability we must not silently strip — so return it unchanged.
    if not sg_ids or not any(e.relationship == "secured_by" for e in graph.edges):
        return graph

    members: dict[str, list[str]] = {}  # SG -> member ENI ids (from secured_by)
    sources_of: dict[str, list[tuple[str, str]]] = {}  # SG -> [(source id, ports)] (from can_reach)
    for e in graph.edges:
        if e.relationship == "secured_by" and e.target in sg_ids:
            members.setdefault(e.target, []).append(e.source)
        elif e.relationship in _REACH_RELS and e.target in sg_ids:
            sources_of.setdefault(e.target, []).append((e.source, e.attributes.get("ports", "")))

    out = Graph(meta=dict(graph.meta))
    # Keep every non-reachability node and the structural edges verbatim.
    for n in graph.nodes:
        if n.type not in ("security_group", "internet", "cidr"):
            out.add_node(Node(id=n.id, type=n.type, label=n.label, attributes=dict(n.attributes)))
    for e in graph.edges:
        if e.relationship in _STRUCTURAL_RELS:
            out.add_edge(Edge(e.source, e.target, e.relationship, dict(e.attributes)))

    # Bring each SG's sources forward to the ENIs it secures. Aggregate ports per (source, ENI).
    reach: dict[tuple[str, str], set[str]] = {}
    source_nodes: dict[str, Node] = {}

    def _emit(node: Node, eni: str, ports: str) -> None:
        source_nodes.setdefault(node.id, node)
        bucket = reach.setdefault((node.id, eni), set())
        if ports:
            bucket.update(p.strip() for p in ports.split(",") if p.strip())

    for sg_id in sorted(sg_ids):
        for eni in members.get(sg_id, []):
            for src_id, ports in sources_of.get(sg_id, []):
                src = nodes.get(src_id)
                if src is None:
                    continue
                if src.type == "internet":
                    nid = f"internet:{eni}"
                    _emit(Node(id=nid, type="internet", label="Internet"), eni, ports)
                elif src.type == "cidr":
                    _emit(Node(src.id, "cidr", src.label, dict(src.attributes)), eni, ports)
                elif src.type == "security_group":  # peer SG -> its members' private IPs
                    for peer_eni in members.get(src.id, []):
                        if peer_eni == eni:
                            continue
                        for ip in (nodes.get(peer_eni) or Node("", "", "")).attributes.get(
                            "private_ips", []
                        ):
                            cidr = f"{ip}/32"
                            _emit(Node(f"cidr:{cidr}", "cidr", cidr, {"cidr": cidr}), eni, ports)

    for sid in sorted(source_nodes):
        out.add_node(source_nodes[sid])
    for sid, eni in sorted(reach):
        ports = ", ".join(sorted(reach[(sid, eni)]))
        out.add_edge(
            Edge(source=sid, target=eni, relationship="can_reach", attributes={"ports": ports})
        )
    return out
