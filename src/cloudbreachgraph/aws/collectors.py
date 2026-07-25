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

import json as _json
import time
from collections.abc import Callable

from . import runner

# A collector's contract: given an optional profile and region, return normalized dicts.
Collector = Callable[[str | None, str | None], list[dict]]

# How far back the flow-log analysis ever reaches (``docs/02_architecture.md §5.7``). The
# per-ENI window starts when the ENI's IP was allocated (from CloudTrail) but is clamped to at
# most this many days in the past — both by this collection-time query bound and, per ENI, in the
# mapping layer. Kept as a module constant so the CLI/docs and the mapping layer agree.
FLOW_LOG_MAX_LOOKBACK_DAYS = 60


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


def _normalize_nat_gateway(raw: dict) -> dict:
    """Keep a NAT gateway's identity, placement, and the ENIs it owns.

    ``NatGatewayAddresses[].NetworkInterfaceId`` is the authoritative ENI-ownership signal the
    builder attributes on (``docs/02_architecture.md §5.4``); ``PublicIp`` is surfaced so the
    map shows the gateway's internet-facing address."""
    addresses = raw.get("NatGatewayAddresses", [])
    return {
        "NatGatewayId": raw.get("NatGatewayId"),
        "VpcId": raw.get("VpcId"),
        "SubnetId": raw.get("SubnetId"),
        "State": raw.get("State"),
        "ConnectivityType": raw.get("ConnectivityType"),
        "NatGatewayAddresses": [
            {
                "NetworkInterfaceId": a.get("NetworkInterfaceId"),
                "PublicIp": a.get("PublicIp"),
                "PrivateIp": a.get("PrivateIp"),
            }
            for a in addresses
        ],
        "Tags": raw.get("Tags", []),
    }


def _normalize_vpc_endpoint(raw: dict) -> dict:
    """Keep a VPC endpoint's identity, type, service, and the ENIs it owns.

    ``NetworkInterfaceIds[]`` lists the ENIs an **Interface**/**GatewayLoadBalancer** endpoint
    owns (empty for a **Gateway** endpoint, which owns no ENI) — the builder attributes ENIs on
    it (``docs/02_architecture.md §5.4``)."""
    return {
        "VpcEndpointId": raw.get("VpcEndpointId"),
        "VpcEndpointType": raw.get("VpcEndpointType"),
        "VpcId": raw.get("VpcId"),
        "ServiceName": raw.get("ServiceName"),
        "State": raw.get("State"),
        "NetworkInterfaceIds": list(raw.get("NetworkInterfaceIds", [])),
        "SubnetIds": list(raw.get("SubnetIds", [])),
        "Tags": raw.get("Tags", []),
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


def _normalize_flow_log(raw: dict) -> dict:
    """Keep a VPC Flow Log's *configuration*: which resource logs, and **where to**.

    A flow log is attached to a ``ResourceId`` (a ``vpc-``/``subnet-``/``eni-`` id) and delivers
    to either CloudWatch Logs (``LogDestinationType == "cloud-watch-logs"``, ``LogGroupName`` set)
    or an S3 bucket (``LogDestinationType == "s3"``, ``LogDestination`` an S3 ARN). This is the
    "where each VPC stores its logs" configuration (``docs/02_architecture.md §5.7``)."""
    return {
        "FlowLogId": raw.get("FlowLogId"),
        "ResourceId": raw.get("ResourceId"),
        "LogDestinationType": raw.get("LogDestinationType"),
        "LogGroupName": raw.get("LogGroupName"),
        "LogDestination": raw.get("LogDestination"),
        "DeliverLogsStatus": raw.get("DeliverLogsStatus"),
        "FlowLogStatus": raw.get("FlowLogStatus"),
        "TrafficType": raw.get("TrafficType"),
    }


def _normalize_allocation_event(raw: dict) -> dict | None:
    """Parse one CloudTrail ``CreateNetworkInterface`` event into an IP-allocation record.

    The interesting fields live inside the ``CloudTrailEvent`` JSON *string*:
    ``responseElements.networkInterface.{networkInterfaceId,privateIpAddress}`` and the
    ``eventTime``. Returns ``None`` for an event we can't parse into an (eni, ip, time) triple, so
    the collector simply drops it (``docs/02_architecture.md §5.7``)."""
    detail = raw.get("CloudTrailEvent")
    parsed: dict = {}
    if isinstance(detail, str):
        try:
            parsed = _json.loads(detail)
        except ValueError:
            parsed = {}
    elif isinstance(detail, dict):
        parsed = detail

    iface = ((parsed.get("responseElements") or {}).get("networkInterface")) or {}
    eni_id = iface.get("networkInterfaceId")
    if not eni_id:
        return None
    allocated_at = parsed.get("eventTime") or raw.get("EventTime")
    return {
        "NetworkInterfaceId": eni_id,
        "PrivateIpAddress": iface.get("privateIpAddress"),
        "AllocatedAt": allocated_at,
    }


# VPC Flow Log default (version 2) record fields, space-separated. We only depend on a handful.
_FLOW_FIELD_IDX = {
    "interface_id": 2,
    "srcaddr": 3,
    "dstaddr": 4,
    "srcport": 5,
    "dstport": 6,
    "protocol": 7,
    "start": 10,
    "action": 12,
}


def _parse_flow_log_message(message: str, log_group: str | None) -> dict | None:
    """Parse one default-format VPC flow-log record line into a normalized dict.

    Fields we keep (``docs/02_architecture.md §5.7``): ``interface_id`` (the ENI the flow was
    captured on), ``srcaddr``/``dstaddr`` (the two ends), ``srcport``/``dstport``, ``protocol``,
    the capture-window ``start`` (epoch seconds, used to clamp to the IP-allocation window) and the
    ``action`` (ACCEPT/REJECT). A missing address (``-``, common for skipped/NODATA records) makes
    the line unusable, so we drop it — never guess."""
    parts = message.split()
    if len(parts) <= _FLOW_FIELD_IDX["action"]:
        return None

    def _field(name: str) -> str:
        return parts[_FLOW_FIELD_IDX[name]]

    srcaddr, dstaddr = _field("srcaddr"), _field("dstaddr")
    if srcaddr in ("", "-") or dstaddr in ("", "-"):
        return None

    def _int(value: str) -> int | None:
        try:
            return int(value)
        except ValueError:
            return None

    return {
        "InterfaceId": _field("interface_id"),
        "SrcAddr": srcaddr,
        "DstAddr": dstaddr,
        "SrcPort": _int(_field("srcport")),
        "DstPort": _int(_field("dstport")),
        "Protocol": _field("protocol"),
        "Start": _int(_field("start")),
        "Action": _field("action"),
        "LogGroup": log_group,
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


def collect_nat_gateways(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-nat-gateways`` -> normalized ``.NatGateways[]``.

    Supplies the ENI -> NAT-gateway ownership the builder uses to attribute otherwise-ownerless
    NAT-gateway ENIs (``docs/02_architecture.md §5.4``). Accounts with no NAT gateways return an
    empty list — handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-nat-gateways"], profile=profile, region=region)
    return [_normalize_nat_gateway(x) for x in data.get("NatGateways", [])]


def collect_vpc_endpoints(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-vpc-endpoints`` -> normalized ``.VpcEndpoints[]``.

    Supplies the ENI -> VPC-endpoint ownership the builder uses to attribute interface-endpoint
    ENIs (``docs/02_architecture.md §5.4``). Accounts with no endpoints return an empty list —
    handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-vpc-endpoints"], profile=profile, region=region)
    return [_normalize_vpc_endpoint(x) for x in data.get("VpcEndpoints", [])]


def collect_route_tables(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-route-tables`` -> normalized ``.RouteTables[]``.

    Feeds the routability check that splits each ENI reachability edge into
    ``routable_can_reach`` / ``not_routable_can_reach`` (``docs/02_architecture.md §5.6``).
    An empty response is handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-route-tables"], profile=profile, region=region)
    return [_normalize_route_table(x) for x in data.get("RouteTables", [])]


# --------------------------------------------------------------------------- #
# Collectors — flow_logs role (§5.7). These gather the material the flow-log analysis
# (``mapping/flowlogs.py``) turns into IP-history + connection nodes/edges. They are read-only:
# ``ec2 describe-flow-logs``, ``cloudtrail lookup-events`` and ``logs filter-log-events`` all only
# *retrieve* data. Value-carrying flags are passed as ``--flag=value`` so both the runner cache key
# and the ``--from-cache`` reader (which key on the positional sub-command) stay stable.
# --------------------------------------------------------------------------- #
def collect_flow_logs(profile: str | None, region: str | None) -> list[dict]:
    """``aws ec2 describe-flow-logs`` -> normalized ``.FlowLogs[]`` (the log *configuration*).

    Where each VPC/subnet/ENI publishes its flow logs. Accounts with no flow logs return an empty
    list — handled gracefully via the ``.get`` default."""
    data = runner.run_aws(["ec2", "describe-flow-logs"], profile=profile, region=region)
    return [_normalize_flow_log(x) for x in data.get("FlowLogs", [])]


def collect_ip_allocation_events(profile: str | None, region: str | None) -> list[dict]:
    """``aws cloudtrail lookup-events`` for ``CreateNetworkInterface`` -> IP-allocation records.

    Each record is ``{NetworkInterfaceId, PrivateIpAddress, AllocatedAt}`` — *when* an ENI's IP was
    allocated (``docs/02_architecture.md §5.7``), which bounds how far back that ENI's flow logs are
    analysed. CloudTrail retention is 90 days; accounts/events we can't parse simply yield fewer
    records (never an error)."""
    data = runner.run_aws(
        [
            "cloudtrail",
            "lookup-events",
            "--lookup-attributes=AttributeKey=EventName,AttributeValue=CreateNetworkInterface",
        ],
        profile=profile,
        region=region,
    )
    events = data.get("Events", [])
    out: list[dict] = []
    for ev in events:
        rec = _normalize_allocation_event(ev)
        if rec is not None:
            out.append(rec)
    return out


def collect_flow_log_records(profile: str | None, region: str | None) -> list[dict]:
    """Fetch and parse the CloudWatch-Logs flow-log *records* for the account's flow logs.

    Discovers which CloudWatch log groups receive flow logs (``describe-flow-logs``), then reads up
    to :data:`FLOW_LOG_MAX_LOOKBACK_DAYS` days of records from each with ``aws logs
    filter-log-events`` and parses every line (``docs/02_architecture.md §5.7``). S3-destined flow
    logs are shown as a destination node but their object contents are **not** read here (that would
    need per-object S3 ``get-object`` calls); this collector covers the CloudWatch path. Read-only:
    ``filter-log-events`` only retrieves. Returns a flat list of normalized flow records."""
    config = runner.run_aws(["ec2", "describe-flow-logs"], profile=profile, region=region)
    log_groups = sorted(
        {
            fl.get("LogGroupName")
            for fl in config.get("FlowLogs", [])
            if fl.get("LogDestinationType") == "cloud-watch-logs" and fl.get("LogGroupName")
        }
    )

    start_ms = int((time.time() - FLOW_LOG_MAX_LOOKBACK_DAYS * 86400) * 1000)
    records: list[dict] = []
    for group in log_groups:
        data = runner.run_aws(
            [
                "logs",
                "filter-log-events",
                f"--log-group-name={group}",
                f"--start-time={start_ms}",
            ],
            profile=profile,
            region=region,
        )
        for event in data.get("events", []):
            rec = _parse_flow_log_message(event.get("message", ""), group)
            if rec is not None:
                records.append(rec)
    return records


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
        collect_nat_gateways,
        collect_vpc_endpoints,
    ],
    # flow_logs (§5.7): IP-allocation history + VPC flow-log config/records. Opt-in via
    # ``--flow-logs`` (the CLI adds the role to the active set); needs extra read-only IAM
    # (ec2:DescribeFlowLogs, cloudtrail:LookupEvents, logs:FilterLogEvents).
    "flow_logs": [
        collect_flow_logs,  # aws ec2        describe-flow-logs   -> .FlowLogs[]
        collect_ip_allocation_events,  # aws cloudtrail lookup-events        -> allocation records
        collect_flow_log_records,  # aws logs       filter-log-events    -> parsed flow records
    ],
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
        "nat_gateways",
        "vpc_endpoints",
    ],
    "flow_logs": ["flow_logs", "ip_allocations", "flow_log_records"],
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
          "subnets": [...], "vpcs": [...], "security_groups": [...],
          "route_tables": [...], "nat_gateways": [...], "vpc_endpoints": [...],
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
