"""``build_graph(collected) -> Graph`` — turn Phase 1's collected bundle into the graph.

The traversal follows the order requested in ``docs/01_overview.md`` and the rules in
``docs/02_architecture.md §5``:

1. Enumerate every **ENI** (the anchor nodes).
2. Attribute each ENI to its **EC2 instance** *or* **load balancer** (never both), using the
   priority order in §5.4 and recording which rule fired in the edge's ``match_rule``.
3. Map each ENI to its **subnet** (``in_subnet``).
4. Connect each subnet to its **VPC** (``in_vpc``).

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
    Subnet,
    Vpc,
)

# Interface types that signal an LB-owned ENI even when the description didn't resolve (§5.4.3).
_LB_INTERFACE_TYPES = {"network_load_balancer", "gateway_load_balancer"}


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
def build_graph(collected: dict[str, Any], *, include_orphans: bool = False) -> Graph:
    """Build the topology graph from a Phase 1 ``collect_all()`` bundle.

    The graph is ENI-anchored: by default only the resources an ENI (transitively) references
    appear. Set ``include_orphans=True`` to *also* emit every collected resource that no ENI
    references — subnets (each still with its ``in_vpc`` edge), VPCs, EC2 instances and load
    balancers — as isolated nodes. Phase 3's CLI exposes this as ``--include-orphans`` (default
    off, matching the ENI-anchored view).
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

    # 5. Orphans — only when requested: surface every collected resource that no ENI references,
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
