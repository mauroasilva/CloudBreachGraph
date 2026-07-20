"""Domain models for the five AWS resource types CloudBreachGraph maps.

Each dataclass has a ``from_collected(dict)`` classmethod that consumes the **normalized
dicts** produced by Phase 1's collectors (see ``docs/learnings/learnings_phase1.md §2b``).
The normalized dicts keep the original AWS key names, so these constructors read fields
like ``d["Attachment"]["InstanceId"]`` exactly as documented in ``docs/02_architecture.md §4``.

:class:`LoadBalancer` covers both the ELBv2 (ALB/NLB/GWLB) and Classic-ELB shapes, which
differ enough that it exposes two constructors: :meth:`LoadBalancer.from_collected` for the
``load_balancers_v2`` shape and :meth:`LoadBalancer.from_classic` for ``load_balancers_classic``
(note Classic's odd ``VPCId`` spelling — see the Phase 1 learnings).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _name_tag(tags: list[dict] | None) -> str | None:
    """Return the value of the ``Name`` tag from an AWS ``Tags`` list, if present."""
    for tag in tags or []:
        if tag.get("Key") == "Name":
            return tag.get("Value")
    return None


@dataclass
class Eni:
    """A network interface — the anchor node of the whole graph."""

    id: str
    subnet_id: str | None
    vpc_id: str | None
    interface_type: str | None
    description: str
    status: str | None
    availability_zone: str | None
    requester_id: str | None
    requester_managed: bool | None
    attachment_instance_id: str | None
    private_ips: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Eni:
        attachment = d.get("Attachment") or {}
        private_ips = [
            ip.get("PrivateIpAddress")
            for ip in d.get("PrivateIpAddresses", [])
            if ip.get("PrivateIpAddress")
        ]
        groups = [g.get("GroupId") for g in d.get("Groups", []) if g.get("GroupId")]
        return cls(
            id=d.get("NetworkInterfaceId"),
            subnet_id=d.get("SubnetId"),
            vpc_id=d.get("VpcId"),
            interface_type=d.get("InterfaceType"),
            description=d.get("Description", "") or "",
            status=d.get("Status"),
            availability_zone=d.get("AvailabilityZone"),
            requester_id=d.get("RequesterId"),
            requester_managed=d.get("RequesterManaged"),
            attachment_instance_id=attachment.get("InstanceId"),
            private_ips=private_ips,
            security_groups=groups,
        )


@dataclass
class Ec2Instance:
    """An EC2 instance an ENI may be attached to (``docs/02_architecture.md §5.3``)."""

    id: str
    state: str | None
    instance_type: str | None
    vpc_id: str | None
    subnet_id: str | None
    name: str | None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Ec2Instance:
        return cls(
            id=d.get("InstanceId"),
            state=(d.get("State") or {}).get("Name"),
            instance_type=d.get("InstanceType"),
            vpc_id=d.get("VpcId"),
            subnet_id=d.get("SubnetId"),
            name=_name_tag(d.get("Tags")),
        )


@dataclass
class LoadBalancer:
    """A load balancer an ENI may belong to (``docs/02_architecture.md §5.4``).

    ``node_id`` is the graph node id: the ELBv2 ``LoadBalancerArn`` for ALB/NLB/GWLB, or the
    ``LoadBalancerName`` for a Classic ELB (which has no ARN in the normalized shape).
    """

    node_id: str
    name: str | None
    lb_type: str | None  # "application" | "network" | "gateway" | "classic"
    scheme: str | None
    dns_name: str | None
    vpc_id: str | None
    arn: str | None = None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> LoadBalancer:
        """Build from a ``load_balancers_v2`` (ELBv2: ALB/NLB/GWLB) normalized dict."""
        arn = d.get("LoadBalancerArn")
        return cls(
            node_id=arn,
            name=d.get("LoadBalancerName"),
            lb_type=d.get("Type"),
            scheme=d.get("Scheme"),
            dns_name=d.get("DNSName"),
            vpc_id=d.get("VpcId"),
            arn=arn,
        )

    @classmethod
    def from_classic(cls, d: dict[str, Any]) -> LoadBalancer:
        """Build from a ``load_balancers_classic`` normalized dict (note the ``VPCId`` key)."""
        name = d.get("LoadBalancerName")
        return cls(
            node_id=name,
            name=name,
            lb_type="classic",
            scheme=d.get("Scheme"),
            dns_name=d.get("DNSName"),
            vpc_id=d.get("VPCId"),  # Classic ELB uses "VPCId" (capital P-C); see Phase 1 learnings.
            arn=None,
        )

    @property
    def elb_token(self) -> str | None:
        """The ``app/<name>/<id>`` (or ``net/``/``gwy/``) token an ENI ``Description`` matches.

        Derived from the suffix of the ELBv2 ARN (``...:loadbalancer/app/<name>/<id>``).
        ``None`` for Classic ELBs, which are matched by name instead.
        """
        if self.arn and ":loadbalancer/" in self.arn:
            return self.arn.rsplit(":loadbalancer/", 1)[-1]
        return None


@dataclass
class Subnet:
    """A subnet an ENI lives in (``docs/02_architecture.md §5.1``)."""

    id: str
    vpc_id: str | None
    cidr: str | None
    availability_zone: str | None
    name: str | None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Subnet:
        return cls(
            id=d.get("SubnetId"),
            vpc_id=d.get("VpcId"),
            cidr=d.get("CidrBlock"),
            availability_zone=d.get("AvailabilityZone"),
            name=_name_tag(d.get("Tags")),
        )


@dataclass
class Vpc:
    """A VPC a subnet belongs to (``docs/02_architecture.md §5.2``)."""

    id: str
    cidr: str | None
    is_default: bool | None
    name: str | None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Vpc:
        return cls(
            id=d.get("VpcId"),
            cidr=d.get("CidrBlock"),
            is_default=d.get("IsDefault"),
            name=_name_tag(d.get("Tags")),
        )
