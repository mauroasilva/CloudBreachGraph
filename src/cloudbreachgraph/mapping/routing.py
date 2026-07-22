"""Routability analysis — is a reachability *source* actually routed to an ENI?

``mapping/builder.py`` finds who is **allowed** to reach an ENI from its security-group inbound
rules (``docs/02_architecture.md §5.5``). This module answers the follow-up: given the ENI's
**route table**, is there a network path for that source to actually arrive? The verdict splits
each ``can_reach`` edge into one of three relationships (``docs/02_architecture.md §5.6``):

* ``routable_can_reach``     — allowed **and** a route exists.
* ``not_routable_can_reach`` — allowed but **no** route (e.g. a ``0.0.0.0/0`` rule on an ENI in a
  private subnet, or with no public IP).
* ``can_reach``              — routability **undetermined** (no route-table data was collected, or
  the ENI's subnet resolves to no route table), so we don't claim either way.

The model is deliberately simple and documented rather than a full route simulator (NACLs, TGW
route propagation, VPN/DX are out of scope):

* **internet** source (``0.0.0.0/0`` / ``::/0``): routable iff the ENI's subnet is *public* (its
  route table has an active default route to an internet gateway ``igw-``) **and** the ENI has a
  public/Elastic IP (so it is addressable from outside).
* **cidr** source: routable if the CIDR is inside the VPC (a ``local`` route always covers it), or
  a route explicitly covers it via a connective gateway (``vgw-``/``tgw-``/``pcx-``), or the ENI is
  internet-reachable as above; otherwise not routable.
* **security_group** source: a referencing SG is VPC-local (SG references don't cross accounts, and
  the local route always covers the VPC), so it is treated as routable.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from ..model.resources import Eni, RouteTable, Subnet, Vpc

RELATIONSHIP_UNDETERMINED = "can_reach"
RELATIONSHIP_ROUTABLE = "routable_can_reach"
RELATIONSHIP_NOT_ROUTABLE = "not_routable_can_reach"

# Next-hop id prefixes that carry *inbound* traffic from outside the local VPC without needing a
# public IP (a private connective path): VPN gateway, transit gateway, VPC peering.
_CONNECTIVE_PREFIXES = ("vgw-", "tgw-", "pcx-")


def _network(cidr: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return None


def _subnet_of(a: str, b: str) -> bool:
    """Whether network ``a`` is contained in (or equal to) network ``b`` (same family)."""
    na, nb = _network(a), _network(b)
    if na is None or nb is None or na.version != nb.version:
        return False
    return na.subnet_of(nb)


@dataclass
class _EniRouting:
    """The routing facts about one ENI that a verdict depends on."""

    route_table: RouteTable | None
    vpc_cidrs: list[str]
    has_public_ip: bool


class RouteResolver:
    """Resolve each ENI's effective route table and classify its reachability edges.

    Built once per ``build_graph`` from the collected route tables, subnets and VPCs. When no route
    tables were collected, :meth:`classify` always returns :data:`RELATIONSHIP_UNDETERMINED` so the
    graph never over-claims routability it couldn't compute.
    """

    def __init__(
        self,
        route_tables: list[RouteTable],
        subnets: dict[str, Subnet],
        vpcs: dict[str, Vpc],
    ) -> None:
        self._has_data = bool(route_tables)
        self._by_subnet: dict[str, RouteTable] = {}
        self._main_by_vpc: dict[str, RouteTable] = {}
        for rt in route_tables:
            for sid in rt.subnet_ids:
                self._by_subnet.setdefault(sid, rt)
            if rt.is_main and rt.vpc_id:
                self._main_by_vpc.setdefault(rt.vpc_id, rt)
        self._subnet_vpc = {s.id: s.vpc_id for s in subnets.values()}
        self._vpc_cidrs = {v.id: [c for c in (v.cidr,) if c] for v in vpcs.values()}

    def _route_table_for(self, subnet_id: str | None, vpc_id: str | None) -> RouteTable | None:
        if subnet_id and subnet_id in self._by_subnet:
            return self._by_subnet[subnet_id]
        if vpc_id and vpc_id in self._main_by_vpc:
            return self._main_by_vpc[vpc_id]
        return None

    def context(self, eni: Eni) -> _EniRouting:
        vpc_id = eni.vpc_id or self._subnet_vpc.get(eni.subnet_id or "")
        rt = self._route_table_for(eni.subnet_id, vpc_id)
        return _EniRouting(
            route_table=rt,
            vpc_cidrs=self._vpc_cidrs.get(vpc_id or "", []),
            has_public_ip=bool(eni.public_ips),
        )

    def classify(self, source_kind: str, cidr: str | None, ctx: _EniRouting) -> str:
        """Return the ``*_can_reach`` relationship for one source reaching an ENI."""
        rt = ctx.route_table
        if not self._has_data or rt is None:
            return RELATIONSHIP_UNDETERMINED

        if source_kind == "security_group":
            return RELATIONSHIP_ROUTABLE  # peer SG is VPC-local (see module docstring)

        if source_kind == "cidr" and cidr and any(_subnet_of(cidr, v) for v in ctx.vpc_cidrs):
            return RELATIONSHIP_ROUTABLE  # source is inside the VPC -> the local route covers it

        if source_kind == "cidr" and cidr and _covered_by_connective_route(rt, cidr):
            return RELATIONSHIP_ROUTABLE  # reachable over a VPN / transit gateway / peering

        # internet, or an external CIDR: needs an internet path to an addressable (public) ENI.
        if _has_default_internet_route(rt) and ctx.has_public_ip:
            return RELATIONSHIP_ROUTABLE
        return RELATIONSHIP_NOT_ROUTABLE


def _active(route) -> bool:
    return route.state in (None, "active")  # treat missing state as active; skip blackholes


def _has_default_internet_route(rt: RouteTable) -> bool:
    """Whether ``rt`` has an active default route (``0.0.0.0/0`` or ``::/0``) to an ``igw-``."""
    for r in rt.routes:
        if not _active(r):
            continue
        is_default = r.dest_cidr == "0.0.0.0/0" or r.dest_ipv6_cidr == "::/0"
        if is_default and (r.target or "").startswith("igw-"):
            return True
    return False


def _covered_by_connective_route(rt: RouteTable, cidr: str) -> bool:
    """Whether an active route to a VPN/TGW/peering gateway covers ``cidr``."""
    for r in rt.routes:
        if not _active(r) or not (r.target or "").startswith(_CONNECTIVE_PREFIXES):
            continue
        dest = r.dest_cidr or r.dest_ipv6_cidr
        if dest and _subnet_of(cidr, dest):
            return True
    return False
