"""``build_graph(collected) -> Graph`` — turn Phase 1's collected bundle into the graph.

The traversal follows the order requested in ``docs/01_overview.md`` and the rules in
``docs/02_architecture.md §5``:

1. Enumerate every **ENI** (the anchor nodes).
2. Attribute each ENI to its **EC2 instance** *or* **load balancer** (never both), using the
   priority order in §5.4 and recording which rule fired in the edge's ``match_rule``.
3. Map each ENI to its **subnet** (``in_subnet``).
4. Connect each subnet to its **VPC** (``in_vpc``).
5. Read each ENI's **security-group inbound rules** and map who can reach it (§5.5). With SGs
   shown (default) the SG is a node between the ENI and its sources; hidden
   (``show_security_groups=False``) the IP sources connect straight to the ENI with the
   routability split (§5.6).

Referenced-but-missing subnets, VPCs, instances and load balancers become ``synthetic`` /
``unresolved`` placeholder nodes so no edge ever dangles.
"""

from __future__ import annotations

from typing import Any, Protocol

from .. import __version__
from ..model.graph import Edge, Graph, Node
from ..model.resources import (
    ClassicLoadBalancer,
    Ec2Instance,
    Elbv2LoadBalancer,
    Eni,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
)
from .routing import RouteResolver

# Interface types that signal an LB-owned ENI even when the description didn't resolve (§5.4.3).
_LB_INTERFACE_TYPES = {"network_load_balancer", "gateway_load_balancer"}

# CIDRs that mean "the entire internet" — a rule allowing one exposes the ENI to the world (§5.5).
_INTERNET_CIDRS = {"0.0.0.0/0", "::/0"}

# The node type for a security group (used when SGs are shown as first-class nodes, §5.5).
_SG_TYPE = "security_group"


class _LoadBalancerLike(Protocol):
    """What the graph needs from any load balancer, ELBv2 or Classic.

    :class:`~cloudbreachgraph.model.resources.Elbv2LoadBalancer` and
    :class:`~cloudbreachgraph.model.resources.ClassicLoadBalancer` each satisfy this
    structurally without sharing a base class, so the two resource types stay decoupled while
    still rendering to the one ``load_balancer`` node.
    """

    node_id: str | None
    name: str | None
    lb_type: str | None
    scheme: str | None
    dns_name: str | None
    vpc_id: str | None


def _parse_elb_description(description: str) -> tuple[str | None, str | None]:
    """Parse an ENI ``Description`` into ``(elbv2_token, classic_name)`` (§5.4).

    * ELBv2 ENIs: ``"ELB app/<name>/<id>"`` (also ``net/``, ``gwy/``) -> the token after
      ``"ELB "`` is returned as ``elbv2_token`` (matched against an ARN suffix).
    * Classic ELB ENIs: ``"ELB <name>"`` (no slash) -> ``classic_name``.
    * Anything else (e.g. ``"Interface for NAT Gateway ..."``) -> ``(None, None)``.
    """
    if not description or not description.startswith("ELB "):
        return None, None
    rest = description[len("ELB ") :].strip()
    if rest.startswith(("app/", "net/", "gwy/")):
        return rest, None
    if "/" not in rest and rest:
        return None, rest
    return None, None


# --------------------------------------------------------------------------- #
# Node factories
# --------------------------------------------------------------------------- #
def _eni_node(eni: Eni) -> Node:
    return Node(
        id=eni.id,
        type="eni",
        label=eni.id,
        attributes={
            "interface_type": eni.interface_type,
            "status": eni.status,
            "availability_zone": eni.availability_zone,
            "description": eni.description,
            "requester_id": eni.requester_id,
            "requester_managed": eni.requester_managed,
            "private_ips": eni.private_ips,
            "public_ips": eni.public_ips,
            "security_groups": eni.security_groups,
        },
    )


def _instance_node(inst: Ec2Instance) -> Node:
    return Node(
        id=inst.id,
        type="ec2_instance",
        label=inst.name or inst.id,
        attributes={
            "state": inst.state,
            "instance_type": inst.instance_type,
            "vpc_id": inst.vpc_id,
            "subnet_id": inst.subnet_id,
        },
    )


def _lb_node(lb: _LoadBalancerLike) -> Node:
    return Node(
        id=lb.node_id,
        type="load_balancer",
        label=lb.name or lb.node_id,
        attributes={
            "lb_type": lb.lb_type,
            "scheme": lb.scheme,
            "dns_name": lb.dns_name,
            "vpc_id": lb.vpc_id,
        },
    )


def _subnet_node(subnet: Subnet) -> Node:
    return Node(
        id=subnet.id,
        type="subnet",
        label=subnet.name or subnet.id,
        attributes={
            "cidr": subnet.cidr,
            "availability_zone": subnet.availability_zone,
            "vpc_id": subnet.vpc_id,
        },
    )


def _vpc_node(vpc: Vpc) -> Node:
    return Node(
        id=vpc.id,
        type="vpc",
        label=vpc.name or vpc.id,
        attributes={"cidr": vpc.cidr, "is_default": vpc.is_default},
    )


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_graph(
    collected: dict[str, Any],
    *,
    include_orphans: bool = False,
    show_security_groups: bool = True,
) -> Graph:
    """Build the topology graph from a Phase 1 ``collect_all()`` bundle.

    The graph is ENI-anchored: by default only the resources an ENI (transitively) references
    appear. Set ``include_orphans=True`` to *also* emit every collected resource that no ENI
    references — subnets (each still with its ``in_vpc`` edge), VPCs, EC2 instances and load
    balancers — as isolated nodes. Phase 3's CLI exposes this as ``--include-orphans`` (default
    off, matching the ENI-anchored view).

    ``show_security_groups`` controls how reachability is rendered (``docs/02_architecture.md
    §5.5``):

    * **True** (default) — security groups are **nodes**: each ENI links to its SGs
      (``secured_by``) and each SG's inbound sources (CIDRs / Internet / peer SGs) link to the SG
      (``can_reach``). This collapses the source fan-out through the shared SG nodes.
    * **False** (``--no-security-groups``) — SGs are hidden and only the **IPs behind** them are
      brought forward, connected **directly** to the ENIs, with the routability split (§5.6). A
      peer-SG reference is expanded to the private IPs of that SG's member ENIs (``/32`` CIDRs).
    """
    meta = dict(collected.get("meta", {}))
    meta.setdefault("tool_version", __version__)
    graph = Graph(meta=meta)

    enis = [Eni.from_collected(x) for x in collected.get("network_interfaces", [])]
    instances = {
        i.id: i for i in map(Ec2Instance.from_collected, collected.get("ec2_instances", []))
    }
    subnets = {s.id: s for s in map(Subnet.from_collected, collected.get("subnets", []))}
    vpcs = {v.id: v for v in map(Vpc.from_collected, collected.get("vpcs", []))}

    elbv2 = list(map(Elbv2LoadBalancer.from_collected, collected.get("load_balancers_v2", [])))
    classic = list(
        map(ClassicLoadBalancer.from_collected, collected.get("load_balancers_classic", []))
    )
    elbv2_by_token = {lb.elb_token: lb for lb in elbv2 if lb.elb_token}
    classic_by_name = {lb.name: lb for lb in classic if lb.name}

    security_groups = {
        sg.id: sg
        for sg in map(SecurityGroup.from_collected, collected.get("security_groups", []))
        if sg.id
    }
    route_tables = [RouteTable.from_collected(x) for x in collected.get("route_tables", [])]
    route_resolver = RouteResolver(route_tables, subnets, vpcs)

    # 1. Anchor: every ENI is a node.
    for eni in enis:
        graph.add_node(_eni_node(eni))

    # 2. Attribution: each ENI -> at most one instance or load balancer.
    for eni in enis:
        _attribute_eni(graph, eni, instances, elbv2_by_token, classic_by_name)

    # 3. ENI -> subnet (always). Remember which VPC each subnet lives in for step 4.
    subnet_vpc_hint: dict[str, str | None] = {}
    for eni in enis:
        subnet_id = eni.subnet_id
        if not subnet_id:
            continue
        _ensure_subnet_node(graph, subnet_id, subnets)
        subnet_vpc_hint.setdefault(subnet_id, eni.vpc_id)
        graph.add_edge(Edge(source=eni.id, target=subnet_id, relationship="in_subnet"))

    # The subnets that get an in_vpc edge: those referenced by an ENI, plus (optionally) every
    # collected subnet that no ENI references. dict.fromkeys keeps insertion order deterministic.
    subnet_ids = dict.fromkeys(subnet_vpc_hint)
    if include_orphans:
        for subnet_id in subnets:
            subnet_ids.setdefault(subnet_id)

    # 4. Subnet -> VPC (always), exactly one edge per subnet node.
    for subnet_id in subnet_ids:
        subnet = subnets.get(subnet_id)
        vpc_id = subnet.vpc_id if subnet else subnet_vpc_hint.get(subnet_id)
        if not vpc_id:
            vpc_id = f"unknown-vpc:{subnet_id}"
        _ensure_subnet_node(graph, subnet_id, subnets)  # covers orphan subnets too
        _ensure_vpc_node(graph, vpc_id, vpcs)
        graph.add_edge(Edge(source=subnet_id, target=vpc_id, relationship="in_vpc"))

    # 5. Reachability (§5.5): read each ENI's security-group inbound rules to find who can reach
    #    it. Always on — it is the point of this pass, not an orphan extra. Two shapes:
    #    with SGs shown, sources fan into shared SG nodes; hidden, they connect straight to ENIs
    #    with the routability split (§5.6).
    if show_security_groups:
        _map_reachability_via_sgs(graph, enis, security_groups)
    else:
        _map_reachability_direct(graph, enis, security_groups, route_resolver)

    # 6. Orphans — only when requested: surface every collected resource that no ENI references,
    #    as an isolated node. Re-adding an already-referenced resource is an idempotent merge, so
    #    this cleanly adds just the unreferenced ones (VPCs with no subnet, instances with no ENI,
    #    load balancers with no ENI). Instances and LBs have no outgoing edges in this model, so an
    #    orphan of either is a standalone node carrying its own subnet/vpc metadata.
    if include_orphans:
        for vpc in vpcs.values():
            _ensure_vpc_node(graph, vpc.id, vpcs)
        for inst in instances.values():
            if inst.id:
                graph.add_node(_instance_node(inst))
        for lb in (*elbv2, *classic):
            if lb.node_id:
                graph.add_node(_lb_node(lb))

    return graph


def _attribute_eni(
    graph: Graph,
    eni: Eni,
    instances: dict[str, Ec2Instance],
    elbv2_by_token: dict[str, Elbv2LoadBalancer],
    classic_by_name: dict[str, ClassicLoadBalancer],
) -> None:
    """Resolve at most one ``attached_to`` edge for an ENI (``docs/02_architecture.md §5``)."""
    # 5.3 — instance attachment wins outright; when present, never attribute to an LB.
    instance_id = eni.attachment_instance_id
    if instance_id:
        inst = instances.get(instance_id)
        if inst is not None:
            graph.add_node(_instance_node(inst))
        else:
            graph.add_node(
                Node(
                    id=instance_id,
                    type="ec2_instance",
                    label=instance_id,
                    attributes={"synthetic": True, "unresolved": True},
                )
            )
        graph.add_edge(Edge(source=eni.id, target=instance_id, relationship="attached_to"))
        return

    # 5.4 — service-managed ENI: try to resolve a load balancer, in priority order.
    token, classic_name = _parse_elb_description(eni.description)

    # 5.4.1 — ELBv2 (ALB/NLB/GWLB) via Description prefix matched to an ARN suffix.
    if token and token in elbv2_by_token:
        lb = elbv2_by_token[token]
        graph.add_node(_lb_node(lb))
        graph.add_edge(Edge(eni.id, lb.node_id, "attached_to", {"match_rule": "elbv2_description"}))
        return

    # 5.4.2 — Classic ELB via Description ("ELB <name>") matched to LoadBalancerName.
    if classic_name and classic_name in classic_by_name:
        lb = classic_by_name[classic_name]
        graph.add_node(_lb_node(lb))
        graph.add_edge(
            Edge(eni.id, lb.node_id, "attached_to", {"match_rule": "classic_elb_description"})
        )
        return

    # 5.4.3 — InterfaceType fallback: an LB-type ENI whose description didn't resolve.
    if eni.interface_type in _LB_INTERFACE_TYPES:
        key = token or classic_name or f"unresolved-lb:{eni.id}"
        label = token.split("/")[1] if token and "/" in token else (classic_name or key)
        graph.add_node(
            Node(
                id=key,
                type="load_balancer",
                label=label,
                attributes={
                    "synthetic": True,
                    "unresolved": True,
                    "interface_type": eni.interface_type,
                },
            )
        )
        graph.add_edge(Edge(eni.id, key, "attached_to", {"match_rule": "interface_type_fallback"}))
        return

    # Otherwise: no compute/LB attachment (NAT gateway, VPC endpoint, RDS, Lambda, ...).
    # The ENI node is already tagged with its InterfaceType — do not invent an attachment.


def _sg_node(sg_id: str, security_groups: dict[str, SecurityGroup]) -> Node:
    """A ``security_group`` node keyed by the raw SG id, labelled with its name when collected."""
    sg = security_groups.get(sg_id)
    attributes: dict[str, Any] = {"group_id": sg_id}
    if sg is not None and sg.vpc_id:
        attributes["vpc_id"] = sg.vpc_id
    label = sg.name if sg is not None and sg.name else sg_id
    return Node(id=sg_id, type=_SG_TYPE, label=label, attributes=attributes)


def _map_reachability_via_sgs(
    graph: Graph,
    enis: list[Eni],
    security_groups: dict[str, SecurityGroup],
) -> None:
    """Reachability with **security groups shown** (the default, ``docs/02_architecture.md §5.5``).

    Security groups are first-class nodes so the source fan-out collapses through them:

    * each ENI links to every SG it carries — edge ``secured_by`` (ENI -> SG);
    * each SG's **inbound** rules add a source per distinct allowance, linked to the SG —
      edge ``can_reach`` (source -> SG), ``ports`` summarising the protocol/port ranges:
      the whole internet (``0.0.0.0/0`` / ``::/0``) -> a per-SG ``internet:<sg-id>`` node; any
      other CIDR -> a shared ``cidr:<cidr>`` node; a referencing security group -> that SG's own
      node (an SG -> SG ``can_reach`` edge).

    Routability (§5.6) is **not** represented here: it is a property of a *(source, ENI)* path,
    but an SG can front ENIs in different subnets, so there is no single verdict for a source -> SG
    edge. The ``--no-security-groups`` view (:func:`_map_reachability_direct`) is where the
    per-ENI routable/not-routable split lives.
    """
    attached_sgs: set[str] = set()
    for eni in enis:
        for sg_id in eni.security_groups:
            attached_sgs.add(sg_id)
            graph.add_node(_sg_node(sg_id, security_groups))
            graph.add_edge(Edge(source=eni.id, target=sg_id, relationship="secured_by"))

    # (source_id, sg_id) -> aggregated ports; the source node is remembered once by id.
    reach: dict[tuple[str, str], set[str]] = {}
    source_nodes: dict[str, Node] = {}

    def _record(node: Node, sg_id: str, port: str) -> None:
        source_nodes.setdefault(node.id, node)
        reach.setdefault((node.id, sg_id), set()).add(port)

    for sg_id in sorted(attached_sgs):
        sg = security_groups.get(sg_id)
        if sg is None:  # attached but its rules weren't collected — leave it as a bare node
            continue
        for rule in sg.ingress:
            port = rule.port_label()
            for cidr in (*rule.cidrs, *rule.ipv6_cidrs):
                if cidr in _INTERNET_CIDRS:
                    sid = f"internet:{sg_id}"  # per-SG, never one shared Internet node
                    _record(Node(id=sid, type="internet", label="Internet"), sg_id, port)
                else:
                    _record(Node(f"cidr:{cidr}", "cidr", cidr, {"cidr": cidr}), sg_id, port)
            for gid in rule.referenced_group_ids:
                _record(_sg_node(gid, security_groups), sg_id, port)

    for sid in sorted(source_nodes):
        graph.add_node(source_nodes[sid])
    for sid, sg_id in sorted(reach):
        ports = ", ".join(sorted(reach[(sid, sg_id)]))
        graph.add_edge(
            Edge(source=sid, target=sg_id, relationship="can_reach", attributes={"ports": ports})
        )


def _map_reachability_direct(
    graph: Graph,
    enis: list[Eni],
    security_groups: dict[str, SecurityGroup],
    resolver: RouteResolver,
) -> None:
    """Reachability with **security groups hidden** (``--no-security-groups``, §5.5 + §5.6).

    Only the **IPs behind** the security groups are brought forward, connected directly to the
    ENIs; no ``security_group`` node is emitted. Each source -> ENI edge carries the routability
    split (§5.6): the whole internet -> a per-ENI ``internet:<eni-id>`` node; a specific CIDR -> a
    shared ``cidr:<cidr>`` node; and a **peer-SG reference** is expanded to the private IPs of that
    SG's member ENIs (each a ``/32`` ``cidr`` node), so the actual addresses that a referencing
    group lets in are surfaced rather than dropped.
    """
    # SG -> its member ENIs (from the collected ENIs), for expanding peer-SG references to IPs.
    members: dict[str, list[Eni]] = {}
    for eni in enis:
        for sg_id in eni.security_groups:
            members.setdefault(sg_id, []).append(eni)

    reach: dict[tuple[str, str], dict] = {}
    source_nodes: dict[str, Node] = {}
    eni_by_id: dict[str, Eni] = {}

    def _record(node: Node, eni: Eni, cidr: str | None, port: str) -> None:
        eni_by_id.setdefault(eni.id, eni)
        source_nodes.setdefault(node.id, node)
        entry = reach.setdefault(
            (node.id, eni.id), {"ports": set(), "kind": node.type, "cidr": cidr}
        )
        entry["ports"].add(port)

    for eni in enis:
        for sg_id in eni.security_groups:
            sg = security_groups.get(sg_id)
            if sg is None:  # SG not in the collected set — skip, no guess
                continue
            for rule in sg.ingress:
                port = rule.port_label()
                for cidr in (*rule.cidrs, *rule.ipv6_cidrs):
                    if cidr in _INTERNET_CIDRS:
                        sid = f"internet:{eni.id}"  # per-ENI, never shared
                        _record(Node(id=sid, type="internet", label="Internet"), eni, cidr, port)
                    else:
                        _record(Node(f"cidr:{cidr}", "cidr", cidr, {"cidr": cidr}), eni, cidr, port)
                for gid in rule.referenced_group_ids:
                    for member in members.get(gid, []):
                        if member.id == eni.id:  # a self-referencing SG rule — skip the ENI itself
                            continue
                        for ip in member.private_ips:
                            c = f"{ip}/32"
                            _record(Node(f"cidr:{c}", "cidr", c, {"cidr": c}), eni, c, port)

    contexts = {eid: resolver.context(eni) for eid, eni in eni_by_id.items()}

    for sid in sorted(source_nodes):
        graph.add_node(source_nodes[sid])
    for sid, eni_id in sorted(reach):
        entry = reach[(sid, eni_id)]
        rel = resolver.classify(entry["kind"], entry["cidr"], contexts[eni_id])
        ports = ", ".join(sorted(entry["ports"]))
        graph.add_edge(
            Edge(source=sid, target=eni_id, relationship=rel, attributes={"ports": ports})
        )


def _ensure_subnet_node(graph: Graph, subnet_id: str, subnets: dict[str, Subnet]) -> None:
    subnet = subnets.get(subnet_id)
    if subnet is not None:
        graph.add_node(_subnet_node(subnet))
    else:
        graph.add_node(
            Node(
                id=subnet_id,
                type="subnet",
                label=subnet_id,
                attributes={"synthetic": True, "unresolved": True},
            )
        )


def _ensure_vpc_node(graph: Graph, vpc_id: str, vpcs: dict[str, Vpc]) -> None:
    vpc = vpcs.get(vpc_id)
    if vpc is not None:
        graph.add_node(_vpc_node(vpc))
    else:
        graph.add_node(
            Node(
                id=vpc_id,
                type="vpc",
                label=vpc_id,
                attributes={"synthetic": True, "unresolved": True},
            )
        )
