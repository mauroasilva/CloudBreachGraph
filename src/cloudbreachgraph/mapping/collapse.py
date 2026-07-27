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


# --------------------------------------------------------------------------- #
# Collapse Auto Scaling groups into a single node (a view transform, §5.7 Part 4)
# --------------------------------------------------------------------------- #
# How many member instance ids to keep on the ASG node (a sample; the full count is separate).
_ASG_INSTANCE_SAMPLE = 8


def collapse_autoscaling_groups(graph: Graph) -> Graph:
    """Return a copy of ``graph`` with each Auto Scaling group collapsed into one node.

    An ASG's **members** are (a) every EC2 instance in the group and (b) every ENI attached to
    those instances — **current and historical** (``docs/02_architecture.md §5.7`` Part 4).
    Membership comes from the ``aws:autoscaling:groupName`` tag: a current instance node carries it
    as an ``asg_name`` attribute (from ``describe-instances``); a current ENI inherits its
    instance's group via the ``attached_to`` edge; a historical ENI/instance carries ``asg_name``
    directly (from the CloudTrail ``RunInstances`` ``tagSet``).

    All member nodes are removed and replaced by one ``autoscaling_group`` node per group (id
    ``asg:<group-name>``). Edges are re-pointed:

    * an edge with **one** member endpoint has that endpoint moved to the ASG node (direction
      preserved); parallels are then merged (``ports`` unioned, ``in_subnet`` kept once per distinct
      subnet the fleet used, so the ASG nests in its VPC cluster);
    * an edge with **both** endpoints in the *same* group becomes a self-loop and is **dropped**
      (intra-fleet ``connects_to`` and every ENI→instance ``attached_to``);
    * a flow between two *different* groups' members becomes a single ASG → ASG edge.

    Subnets, VPCs, security groups, reachability sources, ``flow_peer``s and any ENI/instance
    **not** in an ASG are untouched (only their edges to former members re-point). Deterministic and
    idempotent; a graph with no ASG members is returned unchanged (so a run without
    ``--collapse-asgs`` is byte-for-byte identical)."""
    nodes = {n.id: n for n in graph.nodes}
    node_type = {n.id: n.type for n in graph.nodes}

    instance_asg = {
        n.id: n.attributes["asg_name"]
        for n in graph.nodes
        if n.type == "ec2_instance" and n.attributes.get("asg_name")
    }
    eni_instance = {
        e.source: e.target
        for e in graph.edges
        if e.relationship == "attached_to" and node_type.get(e.source) == "eni"
    }

    asg_of_node: dict[str, str] = {}
    for n in graph.nodes:
        if n.type == "ec2_instance":
            asg = n.attributes.get("asg_name")
        elif n.type == "eni":
            asg = n.attributes.get("asg_name") or instance_asg.get(eni_instance.get(n.id))
        else:
            asg = None
        if asg:
            asg_of_node[n.id] = asg

    if not asg_of_node:
        return graph  # no ASG members — nothing to collapse (idempotent / flag-off byte-identical)

    def _asg_id(name: str) -> str:
        return f"asg:{name}"

    eni_subnet = {e.source: e.target for e in graph.edges if e.relationship == "in_subnet"}
    subnet_vpc = {e.source: e.target for e in graph.edges if e.relationship == "in_vpc"}

    out = Graph(meta=dict(graph.meta))

    # Non-member nodes carry over verbatim.
    for n in graph.nodes:
        if n.id not in asg_of_node:
            out.add_node(Node(id=n.id, type=n.type, label=n.label, attributes=dict(n.attributes)))

    # One autoscaling_group node per group, summarising its (current + historical) members.
    members_by_asg: dict[str, list[Node]] = {}
    for nid, asg in asg_of_node.items():
        members_by_asg.setdefault(asg, []).append(nodes[nid])
    for asg in sorted(members_by_asg):
        out.add_node(_asg_node(asg, members_by_asg[asg], eni_subnet, subnet_vpc))

    # Re-point edges. Edges with no member endpoint pass through verbatim (so ports strings etc.
    # stay byte-identical); member-touching edges are re-pointed and merged.
    merged: dict[tuple[str, str, str], dict] = {}
    for e in graph.edges:
        s_member = e.source in asg_of_node
        t_member = e.target in asg_of_node
        if not s_member and not t_member:
            out.add_edge(Edge(e.source, e.target, e.relationship, dict(e.attributes)))
            continue
        src = _asg_id(asg_of_node[e.source]) if s_member else e.source
        tgt = _asg_id(asg_of_node[e.target]) if t_member else e.target
        if src == tgt:
            continue  # intra-fleet edge (self-loop) — dropped
        bucket = merged.setdefault((src, tgt, e.relationship), {"ports": set(), "attrs": {}})
        ports = e.attributes.get("ports")
        if ports:
            bucket["ports"].update(p.strip() for p in ports.split(",") if p.strip())
        for k, v in e.attributes.items():
            if k != "ports":
                bucket["attrs"].setdefault(k, v)

    for src, tgt, rel in sorted(merged):
        bucket = merged[(src, tgt, rel)]
        attrs = dict(bucket["attrs"])
        if bucket["ports"]:
            attrs["ports"] = ", ".join(sorted(bucket["ports"]))
        out.add_edge(Edge(source=src, target=tgt, relationship=rel, attributes=attrs))

    return out


def _asg_node(
    asg: str,
    members: list[Node],
    eni_subnet: dict[str, str],
    subnet_vpc: dict[str, str],
) -> Node:
    """Build the single ``autoscaling_group`` node summarising a group's collapsed members."""
    instances = sorted((n for n in members if n.type == "ec2_instance"), key=lambda n: n.id)
    enis = sorted((n for n in members if n.type == "eni"), key=lambda n: n.id)

    def _hist(seq: list[Node]) -> int:
        return sum(1 for n in seq if n.attributes.get("historical"))

    subnets: set[str] = set()
    vpc_id: str | None = None
    private_ips: set[str] = set()
    for eni in enis:
        subnet = eni_subnet.get(eni.id)
        if subnet:
            subnets.add(subnet)
            vpc_id = vpc_id or subnet_vpc.get(subnet)
        private_ips.update(eni.attributes.get("private_ips") or [])
    for n in (*instances, *enis):
        vpc_id = vpc_id or n.attributes.get("vpc_id")

    instance_ids = [n.id for n in instances]
    hist_inst, hist_eni = _hist(instances), _hist(enis)
    return Node(
        id=f"asg:{asg}",
        type="autoscaling_group",
        label=asg,
        attributes={
            "asg_name": asg,
            "vpc_id": vpc_id,
            "subnets": sorted(subnets),
            "instance_count": len(instances),
            "eni_count": len(enis),
            "current_instance_count": len(instances) - hist_inst,
            "historical_instance_count": hist_inst,
            "current_eni_count": len(enis) - hist_eni,
            "historical_eni_count": hist_eni,
            "private_ips": sorted(private_ips),
            "instance_ids": instance_ids[:_ASG_INSTANCE_SAMPLE],
            "instance_id_count": len(instance_ids),
        },
    )
