"""Resource collectors, the role registry, and the collection driver.

Each ``collect_x(profile, region) -> list[dict]`` runs exactly one ``aws`` command via
:mod:`cloudbreachgraph.aws.runner` and normalizes the response into a list of plain
dicts, preserving the fields Phase 2 maps on (``docs/02_architecture.md §4``). Collectors
are **role-agnostic**: they know nothing about targets, accounts or roles — only
``(profile, region)``. The knowledge of "which account a role runs against" lives one
level up, in :func:`collect_all`.

The role registry (:data:`ROLE_COLLECTORS` / :data:`ROLE_RESULT_KEYS`, §11.6) is the
single seam future roles extend: adding a role is a new registry entry plus its
collectors — no change to the driver loop, the config grammar, or the CLI.
"""

from __future__ import annotations

from collections.abc import Callable

from . import runner

# A collector's contract: given an optional profile and region, return normalized dicts.
Collector = Callable[[str | None, str | None], list[dict]]


# --------------------------------------------------------------------------- #
# Normalization helpers — keep original AWS key names for the fields we depend on,
# so Phase 2 reads e.g. ``eni["Attachment"]["InstanceId"]`` exactly as documented.
# --------------------------------------------------------------------------- #
def _normalize_eni(raw: dict) -> dict:
    attachment = raw.get("Attachment") or {}
    return {
        "NetworkInterfaceId": raw.get("NetworkInterfaceId"),
        "SubnetId": raw.get("SubnetId"),
        "VpcId": raw.get("VpcId"),
        "InterfaceType": raw.get("InterfaceType"),
        "Description": raw.get("Description", ""),
        "Status": raw.get("Status"),
        "AvailabilityZone": raw.get("AvailabilityZone"),
        "RequesterId": raw.get("RequesterId"),
        "RequesterManaged": raw.get("RequesterManaged"),
        "Attachment": {
            "AttachmentId": attachment.get("AttachmentId"),
            "InstanceId": attachment.get("InstanceId"),
            "InstanceOwnerId": attachment.get("InstanceOwnerId"),
            "DeviceIndex": attachment.get("DeviceIndex"),
            "Status": attachment.get("Status"),
        },
        "PrivateIpAddresses": raw.get("PrivateIpAddresses", []),
        # Interface-level public IP (the primary private IP's Elastic/public IP), if any.
        "Association": {"PublicIp": (raw.get("Association") or {}).get("PublicIp")},
        "Groups": raw.get("Groups", []),
    }


def _normalize_instance(raw: dict) -> dict:
    return {
        "InstanceId": raw.get("InstanceId"),
        "State": {"Name": (raw.get("State") or {}).get("Name")},
        "InstanceType": raw.get("InstanceType"),
        "VpcId": raw.get("VpcId"),
        "SubnetId": raw.get("SubnetId"),
        "Tags": raw.get("Tags", []),
    }


def _normalize_elbv2(raw: dict) -> dict:
    return {
        "LoadBalancerArn": raw.get("LoadBalancerArn"),
        "LoadBalancerName": raw.get("LoadBalancerName"),
        "Type": raw.get("Type"),
        "Scheme": raw.get("Scheme"),
        "VpcId": raw.get("VpcId"),
        "DNSName": raw.get("DNSName"),
        "State": raw.get("State", {}),
    }


def _normalize_classic_elb(raw: dict) -> dict:
    return {
        "LoadBalancerName": raw.get("LoadBalancerName"),
        # Classic ELB spells the key "VPCId" (capital PC), unlike every other resource.
        "VPCId": raw.get("VPCId"),
        "DNSName": raw.get("DNSName"),
        "Scheme": raw.get("Scheme"),
        "Subnets": raw.get("Subnets", []),
        "SecurityGroups": raw.get("SecurityGroups", []),
    }


def _normalize_subnet(raw: dict) -> dict:
    return {
        "SubnetId": raw.get("SubnetId"),
        "VpcId": raw.get("VpcId"),
        "CidrBlock": raw.get("CidrBlock"),
        "AvailabilityZone": raw.get("AvailabilityZone"),
        "Tags": raw.get("Tags", []),
    }


def _normalize_vpc(raw: dict) -> dict:
    return {
        "VpcId": raw.get("VpcId"),
        "CidrBlock": raw.get("CidrBlock"),
        "IsDefault": raw.get("IsDefault"),
        "Tags": raw.get("Tags", []),
    }


def _normalize_ip_permission(raw: dict) -> dict:
    """Keep the fields a reachability rule depends on from one ``IpPermissions[]`` entry.

    ``IpProtocol`` is ``"-1"`` for *all traffic* (then ``FromPort``/``ToPort`` are absent);
    otherwise the port range is ``FromPort``..``ToPort``. Sources are IPv4 CIDRs
    (``IpRanges[].CidrIp``), IPv6 CIDRs (``Ipv6Ranges[].CidrIpv6``) and referencing security
    groups (``UserIdGroupPairs[].GroupId``). See ``docs/02_architecture.md §5.5``.
    """
    return {
        "IpProtocol": raw.get("IpProtocol"),
        "FromPort": raw.get("FromPort"),
        "ToPort": raw.get("ToPort"),
        "IpRanges": [{"CidrIp": r.get("CidrIp")} for r in raw.get("IpRanges", [])],
        "Ipv6Ranges": [{"CidrIpv6": r.get("CidrIpv6")} for r in raw.get("Ipv6Ranges", [])],
        "UserIdGroupPairs": [
            {"GroupId": g.get("GroupId")} for g in raw.get("UserIdGroupPairs", [])
        ],
    }


def _route_target(raw: dict) -> str | None:
    """The single target id of a route, whichever gateway/peering/eni field carries it.

    A route's next hop is spelled in one of several mutually-exclusive keys (``GatewayId`` for
    ``local`` / ``igw-`` / ``vgw-``, ``NatGatewayId``, ``TransitGatewayId``,
    ``VpcPeeringConnectionId``, ``NetworkInterfaceId``, …). We collapse them to one ``target``
    string so the routing analysis (``mapping/routing.py``) can classify the next hop by prefix.
    """
    for key in (
        "GatewayId",
        "NatGatewayId",
        "TransitGatewayId",
        "VpcPeeringConnectionId",
        "EgressOnlyInternetGatewayId",
        "NetworkInterfaceId",
        "InstanceId",
        "CarrierGatewayId",
        "LocalGatewayId",
    ):
        if raw.get(key):
            return raw[key]
    return None


def _normalize_route(raw: dict) -> dict:
    return {
        "DestinationCidrBlock": raw.get("DestinationCidrBlock"),
        "DestinationIpv6CidrBlock": raw.get("DestinationIpv6CidrBlock"),
        "Target": _route_target(raw),
        "State": raw.get("State"),
    }


def _normalize_route_table(raw: dict) -> dict:
    """Keep a route table's VPC, its subnet associations (+ whether it's the VPC main RT), and
    its routes' destination/target/state (``docs/02_architecture.md §5.6``)."""
    associations = raw.get("Associations", [])
    return {
        "RouteTableId": raw.get("RouteTableId"),
        "VpcId": raw.get("VpcId"),
        "Main": any(a.get("Main") for a in associations),
        "SubnetIds": [a.get("SubnetId") for a in associations if a.get("SubnetId")],
        "Routes": [_normalize_route(r) for r in raw.get("Routes", [])],
    }


def _normalize_security_group(raw: dict) -> dict:
    """Keep a security group's identity and its **ingress** rules (``IpPermissions``).

    Only inbound rules matter for "who can reach this ENI"; egress (``IpPermissionsEgress``)
    is intentionally dropped (``docs/02_architecture.md §5.5``)."""
    return {
        "GroupId": raw.get("GroupId"),
        "GroupName": raw.get("GroupName"),
        "VpcId": raw.get("VpcId"),
        "Description": raw.get("Description"),
        "IpPermissions": [_normalize_ip_permission(p) for p in raw.get("IpPermissions", [])],
    }


# --------------------------------------------------------------------------- #
# Collectors — one AWS command each (network role)
# --------------------------------------------------------------------------- #
def collect_network_interfaces(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-network-interfaces`` -> normalized ``.NetworkInterfaces[]``."""
    data = runner.run_aws(["ec2", "describe-network-interfaces"], profile=profile, region=region)
    return [_normalize_eni(x) for x in data.get("NetworkInterfaces", [])]


def collect_ec2_instances(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-instances`` -> normalized instances, flattened out of
    ``.Reservations[].Instances[]`` into a single flat list."""
    data = runner.run_aws(["ec2", "describe-instances"], profile=profile, region=region)
    instances: list[dict] = []
    for reservation in data.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            instances.append(_normalize_instance(inst))
    return instances


def collect_load_balancers_v2(profile: str | None, region: str | None) -> list[dict]:
    """``aws elbv2 describe-load-balancers`` -> normalized ``.LoadBalancers[]`` (ALB/NLB/GWLB)."""
    data = runner.run_aws(["elbv2", "describe-load-balancers"], profile=profile, region=region)
    return [_normalize_elbv2(x) for x in data.get("LoadBalancers", [])]


def collect_load_balancers_classic(profile: str | None, region: str | None) -> list[dict]:
    """``aws elb describe-load-balancers`` -> normalized ``.LoadBalancerDescriptions[]``.

    Accounts with no Classic ELBs return an empty list — handled gracefully (never an
    error) via the ``.get`` default."""
    data = runner.run_aws(["elb", "describe-load-balancers"], profile=profile, region=region)
    return [_normalize_classic_elb(x) for x in data.get("LoadBalancerDescriptions", [])]


def collect_subnets(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-subnets`` -> normalized ``.Subnets[]``."""
    data = runner.run_aws(["ec2", "describe-subnets"], profile=profile, region=region)
    return [_normalize_subnet(x) for x in data.get("Subnets", [])]


def collect_vpcs(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-vpcs`` -> normalized ``.Vpcs[]``."""
    data = runner.run_aws(["ec2", "describe-vpcs"], profile=profile, region=region)
    return [_normalize_vpc(x) for x in data.get("Vpcs", [])]


def collect_security_groups(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-security-groups`` -> normalized ``.SecurityGroups[]``.

    Provides the inbound rules the builder turns into ENI reachability nodes/edges
    (``docs/02_architecture.md §5.5``). Accounts with only the default SG still return it,
    and an empty response is handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-security-groups"], profile=profile, region=region)
    return [_normalize_security_group(x) for x in data.get("SecurityGroups", [])]


def collect_route_tables(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-route-tables`` -> normalized ``.RouteTables[]``.

    Feeds the routability check that splits each ENI reachability edge into
    ``routable_can_reach`` / ``not_routable_can_reach`` (``docs/02_architecture.md §5.6``).
    An empty response is handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-route-tables"], profile=profile, region=region)
    return [_normalize_route_table(x) for x in data.get("RouteTables", [])]


# --------------------------------------------------------------------------- #
# Role registry (§11.6) — the seam future roles extend
# --------------------------------------------------------------------------- #
ROLE_COLLECTORS: dict[str, list[Collector]] = {
    "network": [
        collect_network_interfaces,
        collect_ec2_instances,
        collect_load_balancers_v2,
        collect_load_balancers_classic,
        collect_subnets,
        collect_vpcs,
        collect_security_groups,
        collect_route_tables,
    ],
    # ── future (see docs/05_roadmap.md); do NOT implement in v1 ──────────────
    # "flow_logs": [
    #     collect_flow_logs,          # aws ec2  describe-flow-logs   -> .FlowLogs[]
    #     collect_log_destinations,   # aws logs describe-log-groups / s3api ...
    # ],
}

# Parallel to ROLE_COLLECTORS: the bundle key each collector's result is stored under.
# ``ROLE_RESULT_KEYS[role][i]`` names the output of ``ROLE_COLLECTORS[role][i]``.
ROLE_RESULT_KEYS: dict[str, list[str]] = {
    "network": [
        "network_interfaces",
        "ec2_instances",
        "load_balancers_v2",
        "load_balancers_classic",
        "subnets",
        "vpcs",
        "security_groups",
        "route_tables",
    ],
    # "flow_logs": ["flow_logs", "log_destinations"],  # future
}


# --------------------------------------------------------------------------- #
# Driver loop (§11.7)
# --------------------------------------------------------------------------- #
def collect_all(
    resolved,
    *,
    roles: tuple[str, ...] | list[str] = ("network",),
    cache_dir: str | None = None,
) -> dict:
    """Run every collector for each requested role and bundle the results (§11.7).

    ``resolved`` is a :class:`cloudbreachgraph.config.ResolvedTarget`; each role is run
    with its own resolved ``profile``/``region``, so a multi-account target can pull
    different roles from different accounts in one call. Per-role account provenance is
    recorded under ``meta.accounts``.

    Returns the exact Phase 1 interface contract (``docs/03_phase_plan.md``)::

        {
          "meta": {"target": str | None, "region": str | None,
                   "accounts": {role: account_id | None, ...}},
          "network_interfaces": [...], "ec2_instances": [...],
          "load_balancers_v2": [...], "load_balancers_classic": [...],
          "subnets": [...], "vpcs": [...],
        }
    """
    roles = tuple(roles)
    if cache_dir is not None:
        runner.configure_cache(cache_dir)

    # meta.region reflects the first requested role's region (network in v1).
    first_region = resolved.roles[roles[0]].region if roles else None
    bundle: dict = {
        "meta": {
            "target": resolved.target,
            "region": first_region,
            "accounts": {},
        }
    }

    for role in roles:
        acct = resolved.roles[role]
        collectors = ROLE_COLLECTORS[role]
        keys = ROLE_RESULT_KEYS[role]
        for collector, key in zip(collectors, keys, strict=True):
            bundle[key] = collector(acct.profile, acct.region)
        bundle["meta"]["accounts"][role] = acct.account_id

    return bundle
