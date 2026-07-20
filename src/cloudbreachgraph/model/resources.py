"""Domain models for the five AWS resource types CloudBreachGraph maps.

Each dataclass has a ``from_collected(dict)`` classmethod that consumes the **normalized
dicts** produced by Phase 1's collectors (see ``docs/learnings/learnings_phase1.md §2b``).
The normalized dicts keep the original AWS key names, so these constructors read fields
like ``d["Attachment"]["InstanceId"]`` exactly as documented in ``docs/02_architecture.md §4``.

ELBv2 (ALB/NLB/GWLB) and Classic ELB are **separate** AWS APIs with distinct response shapes,
so they get **separate** dataclasses — :class:`Elbv2LoadBalancer` and
:class:`ClassicLoadBalancer` — each with its own ``from_collected``. Keeping them apart means a
future change to one (identity is ARN vs. name; ``VpcId`` vs. Classic's odd ``VPCId`` spelling;
token vs. name description-matching) can't ripple into the other. Both still render to the single
``load_balancer`` graph node — the mapping layer treats them through a small structural protocol.
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
    public_ips: list[str] = field(default_factory=list)
    security_groups: list[str] = field(default_factory=list)

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Eni:
        attachment = d.get("Attachment") or {}
        private_ips = [
            ip.get("PrivateIpAddress")
            for ip in d.get("PrivateIpAddresses", [])
            if ip.get("PrivateIpAddress")
        ]
        # Public IPs come from the ``Association`` blocks: one per private IP that has an
        # Elastic/public IP, plus the interface-level ``Association`` for the primary IP.
        # De-duplicate while preserving first-seen order (an EIP appears both places).
        public_ips: list[str] = []
        for candidate in [
            (d.get("Association") or {}).get("PublicIp"),
            *(
                (ip.get("Association") or {}).get("PublicIp")
                for ip in d.get("PrivateIpAddresses", [])
            ),
        ]:
            if candidate and candidate not in public_ips:
                public_ips.append(candidate)
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
            public_ips=public_ips,
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
class Elbv2LoadBalancer:
    """An ELBv2 load balancer — ALB, NLB or GWLB (``docs/02_architecture.md §5.4.1``).

    Identity is the ``LoadBalancerArn``; ENIs are attributed by matching their ``Description``
    token against the ARN suffix (see :attr:`elb_token`).
    """

    arn: str | None
    name: str | None
    lb_type: str | None  # "application" | "network" | "gateway"
    scheme: str | None
    dns_name: str | None
    vpc_id: str | None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> Elbv2LoadBalancer:
        """Build from a ``load_balancers_v2`` normalized dict."""
        return cls(
            arn=d.get("LoadBalancerArn"),
            name=d.get("LoadBalancerName"),
            lb_type=d.get("Type"),
            scheme=d.get("Scheme"),
            dns_name=d.get("DNSName"),
            vpc_id=d.get("VpcId"),
        )

    @property
    def node_id(self) -> str | None:
        """Graph node id for an ELBv2 LB: its ARN."""
        return self.arn

    @property
    def elb_token(self) -> str | None:
        """The ``app/<name>/<id>`` (or ``net/``/``gwy/``) token an ENI ``Description`` matches.

        Derived from the suffix of the ELBv2 ARN (``...:loadbalancer/app/<name>/<id>``).
        """
        if self.arn and ":loadbalancer/" in self.arn:
            return self.arn.rsplit(":loadbalancer/", 1)[-1]
        return None


@dataclass
class ClassicLoadBalancer:
    """A Classic ELB (``docs/02_architecture.md §5.4.2``).

    Identity is the ``LoadBalancerName`` (Classic ELBs have no ARN in the normalized shape);
    ENIs are attributed by matching their ``Description`` (``"ELB <name>"``) against the name.
    """

    name: str | None
    scheme: str | None
    dns_name: str | None
    vpc_id: str | None

    @classmethod
    def from_collected(cls, d: dict[str, Any]) -> ClassicLoadBalancer:
        """Build from a ``load_balancers_classic`` normalized dict (note the ``VPCId`` key)."""
        return cls(
            name=d.get("LoadBalancerName"),
            scheme=d.get("Scheme"),
            dns_name=d.get("DNSName"),
            # Classic ELB uses "VPCId" (capital P-C), unlike every other resource; see Phase 1.
            vpc_id=d.get("VPCId"),
        )

    @property
    def node_id(self) -> str | None:
        """Graph node id for a Classic ELB: its name."""
        return self.name

    @property
    def lb_type(self) -> str:
        """Classic ELBs have a fixed type used for the graph node attribute."""
        return "classic"


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
