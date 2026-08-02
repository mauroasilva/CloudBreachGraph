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

import email.utils
import gzip
import json as _json
import os
import re
import socket
import struct
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

from . import cloudtrail_enis, runner
from .cloudtrail_enis import enis_from_events

if TYPE_CHECKING:
    from ..config import ResolvedAccount

# A collector's contract: given an optional profile and region, return normalized dicts.
Collector = Callable[[str | None, str | None], list[dict]]


@dataclass(frozen=True)
class ArchiveAccess:
    """How the S3 flow-log object I/O resolves an account (``docs/02_architecture.md §5.7``).

    VPC flow-log *objects* live in the log-archive account, which is often **not** the account the
    VPCs live in (the ``network`` account). This carries the resolution strategy for the S3 read:

    * ``primary`` — the account attempted **first** (the network account); in centralized-logging
      setups the source account is frequently granted cross-account read on the bucket, so this
      usually just works with no config.
    * ``explicit`` — when the ``flow_logs`` role is **explicitly bound** (a ``[targets.<t>.roles]``
      entry, or ``--profile``), the S3 read uses this account **directly** — the auto-resolution
      trial is skipped.
    * ``candidates`` — the ordered fallback accounts (distinct configured ``account_id → profile``
      entries) tried, first success wins, only when ``explicit`` is unset and ``primary`` is denied.
    """

    primary: ResolvedAccount
    explicit: ResolvedAccount | None = None
    candidates: tuple[ResolvedAccount, ...] = ()


# How far back the flow-log *record* analysis reaches by default (``docs/02_architecture.md §5.7``).
# This is the **default** for the configurable ``--flow-log-days N`` window; the effective window is
# the module-level :data:`_flow_log_window_days`, read by :func:`collect_flow_log_records` (the
# record window) and threaded from the CLI via :func:`set_flow_log_window`. Kept as a constant so
# the CLI/docs and the mapping layer agree on the default.
FLOW_LOG_MAX_LOOKBACK_DAYS = 60

# How far back the **CloudTrail** history collectors reach. CloudTrail Event history retains ~90
# days, so the historical-ENI reconstruction (:func:`collect_historical_enis`) and the IP-allocation
# history (:func:`collect_ip_allocation_events`) always query the full 90 days — independent of the
# (possibly shorter) flow-log-record window — so a flow captured on a now-terminated ENI can still
# be resolved to the ENI that held its IP at the time (``docs/02_architecture.md §5.7``).
CLOUDTRAIL_MAX_LOOKBACK_DAYS = 90

# The configured flow-log-record window in days (``--flow-log-days N``, default
# :data:`FLOW_LOG_MAX_LOOKBACK_DAYS`). A module global set once by the CLI via
# :func:`set_flow_log_window`, mirroring the ``configure_cache``/``set_verbose`` pattern so the
# ``collect_x(profile, region)`` collector contract is preserved (the window isn't a parameter).
_flow_log_window_days: int = FLOW_LOG_MAX_LOOKBACK_DAYS

# Whether the 90-day CloudTrail historical-ENI reconstruction runs (on with ``--flow-logs``, off
# under ``--no-historical-enis``). When off, :func:`collect_historical_enis` returns no records, so
# no extra CloudTrail calls are made and the mapping sees no historical ENIs.
_historical_enabled: bool = True

# An explicit flow-log-record window ``[start, end]`` in epoch seconds (``--flow-log-start`` /
# ``--flow-log-end``). When ``_flow_log_start_epoch`` is set it **overrides** the days-based window
# (:data:`_flow_log_window_days`); ``_flow_log_end_epoch`` ``None`` means "up to now". Set once by
# the CLI via :func:`set_flow_log_range`, mirroring the ``set_flow_log_window`` knob.
_flow_log_start_epoch: float | None = None
_flow_log_end_epoch: float | None = None


def set_flow_log_window(days: int) -> None:
    """Set the flow-log-record window in days (``--flow-log-days N``); read by the collectors.

    Mirrors :func:`~cloudbreachgraph.aws.runner.configure_cache`: a module-level knob toggled once
    by the CLI so the ``collect_x(profile, region)`` contract is untouched. Only the flow-log
    **record** window follows this; the CloudTrail history always reaches its 90-day cap (see
    :func:`_cloudtrail_lookback_days`)."""
    global _flow_log_window_days
    _flow_log_window_days = days


def get_flow_log_window() -> int:
    """The configured flow-log-record window in days (default ``FLOW_LOG_MAX_LOOKBACK_DAYS``)."""
    return _flow_log_window_days


def set_historical_enis(enabled: bool) -> None:
    """Enable/disable the 90-day CloudTrail historical-ENI reconstruction (``--no-historical-enis``
    turns it off). Off ⇒ :func:`collect_historical_enis` short-circuits to an empty list."""
    global _historical_enabled
    _historical_enabled = enabled


def set_flow_log_range(start_epoch: float | None, end_epoch: float | None = None) -> None:
    """Set an explicit flow-log-record window ``[start, end]`` in epoch seconds
    (``--flow-log-start`` / ``--flow-log-end``), overriding the days-based window.
    ``end_epoch=None`` means "up to now". ``start_epoch=None`` clears the range (back to the
    ``--flow-log-days`` window)."""
    global _flow_log_start_epoch, _flow_log_end_epoch
    _flow_log_start_epoch = start_epoch
    _flow_log_end_epoch = end_epoch if start_epoch is not None else None


def _flow_log_window_bounds() -> tuple[float, float | None]:
    """The flow-log-record window as ``(start_epoch, end_epoch|None)``.

    An explicit ``--flow-log-start`` / ``--flow-log-end`` range wins; otherwise ``start = now −
    <window> days`` and ``end = None`` (up to now). Only the *record* readers use these bounds — the
    CloudTrail history always reaches its 90-day cap (:func:`_cloudtrail_lookback_days`)."""
    if _flow_log_start_epoch is not None:
        return _flow_log_start_epoch, _flow_log_end_epoch
    return time.time() - _flow_log_window_days * 86400, None


def _cloudtrail_lookback_days() -> int:
    """CloudTrail lookback in days: always the full retention (90), never shorter than the flow-log
    window — ``min(CLOUDTRAIL_MAX_LOOKBACK_DAYS, max(days, CLOUDTRAIL_MAX_LOOKBACK_DAYS))``. History
    reconstruction must reach the 90-day max regardless of the (possibly shorter) record window."""
    return min(
        CLOUDTRAIL_MAX_LOOKBACK_DAYS, max(_flow_log_window_days, CLOUDTRAIL_MAX_LOOKBACK_DAYS)
    )


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
        # ``LogFormat`` is kept so the *record* fetch can reuse this one config source (queried in
        # the network account) — the CloudWatch reader derives each group's field positions from it,
        # rather than re-running ``describe-flow-logs`` under a second profile (§5.7 cross-account).
        "LogFormat": raw.get("LogFormat"),
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


# VPC Flow Log **default** (version 2) record field positions, space-separated. Used when a flow log
# has no explicit ``LogFormat`` (the standard layout).
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

# Map a ``LogFormat`` token name (as it appears inside ``${...}``) to our internal field key, so a
# **custom** flow-log format is parsed by *position derived from its own format string* rather than
# assuming the default order. Only the fields the analysis needs are mapped.
_FLOW_TOKEN_TO_KEY = {
    "interface-id": "interface_id",
    "srcaddr": "srcaddr",
    "dstaddr": "dstaddr",
    "srcport": "srcport",
    "dstport": "dstport",
    "protocol": "protocol",
    "start": "start",
    "action": "action",
}

# The fields we must be able to locate to use a record at all (the ENI + the two ends).
_FLOW_REQUIRED = ("interface_id", "srcaddr", "dstaddr")


def _field_index_from_format(log_format: str | None) -> dict[str, int] | None:
    """Build a field-name -> position map from a flow log's ``LogFormat`` string.

    ``LogFormat`` looks like ``"${version} ${account-id} ${interface-id} ${srcaddr} ..."``; each
    ``${token}`` occupies one space-separated position. An empty/absent format means the **default**
    layout (:data:`_FLOW_FIELD_IDX`). Returns ``None`` if the format omits a required field
    (:data:`_FLOW_REQUIRED`), so the caller can skip that group instead of misreading every line."""
    if not log_format or not log_format.strip():
        return dict(_FLOW_FIELD_IDX)
    idx: dict[str, int] = {}
    for pos, token in enumerate(log_format.split()):
        name = token.strip()
        if name.startswith("${") and name.endswith("}"):
            name = name[2:-1]
        key = _FLOW_TOKEN_TO_KEY.get(name)
        if key is not None:
            idx[key] = pos
    if any(k not in idx for k in _FLOW_REQUIRED):
        return None
    return idx


def _parse_flow_log_message(
    message: str, log_group: str | None, field_idx: dict[str, int] | None = None
) -> dict | None:
    """Parse one VPC flow-log record line into a normalized dict, per ``field_idx``.

    ``field_idx`` maps field name -> position (from the flow log's ``LogFormat``, or the default
    layout). Fields we keep (``docs/02_architecture.md §5.7``): ``interface_id`` (the ENI the flow
    was captured on), ``srcaddr``/``dstaddr`` (the two ends), ``srcport``/``dstport``, ``protocol``,
    the capture-window ``start`` (epoch seconds, used to clamp to the IP-allocation window) and the
    ``action`` (ACCEPT/REJECT). A missing address (``-``, common for skipped/NODATA records) makes
    the line unusable, so we drop it — never guess."""
    idx = field_idx if field_idx is not None else _FLOW_FIELD_IDX
    parts = message.split()
    needed = max((idx[k] for k in _FLOW_REQUIRED), default=0)
    if len(parts) <= needed:
        return None

    def _field(name: str) -> str | None:
        pos = idx.get(name)
        return parts[pos] if pos is not None and pos < len(parts) else None

    srcaddr, dstaddr = _field("srcaddr"), _field("dstaddr")
    if srcaddr in (None, "", "-") or dstaddr in (None, "", "-"):
        return None

    def _int(value: str | None) -> int | None:
        try:
            return int(value)  # type: ignore[arg-type]
        except (ValueError, TypeError):
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
    analysed. The lookback reaches the full CloudTrail retention (:func:`_cloudtrail_lookback_days`,
    90 days) — independent of the (possibly shorter) flow-log-record window — so IP history is as
    complete as CloudTrail allows. An ENI created before the window has no event here, so its
    ``ip_history`` start is unknown — treated as "held throughout"; accounts/events we can't parse
    simply yield fewer records (never an error)."""
    start = datetime.now(UTC) - timedelta(days=_cloudtrail_lookback_days())
    data = runner.run_aws(
        [
            "cloudtrail",
            "lookup-events",
            "--lookup-attributes=AttributeKey=EventName,AttributeValue=CreateNetworkInterface",
            f"--start-time={start.strftime('%Y-%m-%dT%H:%M:%SZ')}",
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


# --------------------------------------------------------------------------- #
# Historical-ENI reconstruction (§5.7) — rebuild ENIs that existed in the window from CloudTrail,
# so a flow captured on a now-terminated ASG ENI can still be resolved. One ``lookup-events`` query
# per EventName (the ``--flag=value`` form keeps the cache key stable), merged by ENI id across
# event sources. Read-only.
# --------------------------------------------------------------------------- #
def collect_historical_enis(profile: str | None, region: str | None) -> list[dict]:
    """Reconstruct the ENIs that existed in the CloudTrail window (§5.7 Part 2).

    Runs ``aws cloudtrail lookup-events`` **once per EventName** (``CreateNetworkInterface``,
    ``RunInstances``, ``DeleteNetworkInterface``, ``TerminateInstances``) over the full 90-day
    CloudTrail retention (:func:`_cloudtrail_lookback_days`), then hands the merged event stream to
    the shared pure parser :func:`cloudbreachgraph.aws.cloudtrail_enis.enis_from_events` (reused by
    the ``cloudbreachgraph-merge`` tool) to reconstruct one record per ENI::

        {NetworkInterfaceId, PrivateIpAddresses[], SubnetId, VpcId, Groups[], Description,
         InterfaceType, RequesterId, InstanceId, AsgName, Name, CreatedAt, DeletedAt}

    ``RunInstances`` is what most instance ENIs come from (they have no standalone
    ``CreateNetworkInterface`` event) and carries the ``aws:autoscaling:groupName`` tag used for ASG
    collapse (§Part 4). ``DeleteNetworkInterface``/``TerminateInstances`` set ``DeletedAt`` (a
    terminated instance's deletion cascades to its ENIs). Returns an empty list when historical
    reconstruction is disabled (``--no-historical-enis``) so no extra CloudTrail calls run. Events
    we can't parse simply yield fewer records (never an error); each event is checked against the
    EventName it was queried under so a shared response can't be misread."""
    if not _historical_enabled:
        return []
    start = datetime.now(UTC) - timedelta(days=_cloudtrail_lookback_days())
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    events: list[dict] = []
    counts: dict[str, int] = {}
    for event_name in cloudtrail_enis.EVENT_NAMES:
        data = runner.run_aws(
            [
                "cloudtrail",
                "lookup-events",
                f"--lookup-attributes=AttributeKey=EventName,AttributeValue={event_name}",
                f"--start-time={start_str}",
            ],
            profile=profile,
            region=region,
        )
        # Only interpret an event as the type it was queried under (robust to a shared mock/cache
        # response); the pure parser then dispatches each on its own ``eventName``.
        matched = [
            ev
            for ev in data.get("Events", [])
            if cloudtrail_enis.cloudtrail_detail(ev).get("eventName") in (event_name, None)
        ]
        counts[event_name] = len(matched)
        events.extend(matched)

    out = enis_from_events(events)
    _report_historical_enis(counts, out)
    return out


def _report_historical_enis(counts: dict[str, int], reconstructed: list[dict]) -> None:
    """One-line stderr diagnostic for the historical-ENI reconstruction (§5.7): per-event counts and
    how many distinct ENIs were rebuilt, so its volume/coverage is visible next to the flow-log
    diagnostic. Skipped entirely when nothing was queried (reconstruction disabled)."""
    if not counts:
        return
    terminated = sum(1 for r in reconstructed if r.get("DeletedAt"))
    by_event = ", ".join(f"{counts[n]} {n}" for n in cloudtrail_enis.EVENT_NAMES if n in counts)
    print(
        f"cloudbreachgraph: historical ENIs: CloudTrail events [{by_event}]; reconstructed "
        f"{len(reconstructed)} ENI(s) ({terminated} terminated).",
        file=sys.stderr,
    )


class FlowLogDestinationError(RuntimeError):
    """A flow log delivers to a destination type we have **no record collector** for.

    VPC Flow Logs can deliver to ``cloud-watch-logs``, ``s3`` or ``kinesis-data-firehose``. We read
    records from the first two; any other (or a missing) type raises this so the run fails loudly
    rather than silently omitting those flows (``docs/02_architecture.md §5.7``)."""

    def __init__(self, dest_type: str | None, flow_log_id: str | None = None) -> None:
        self.dest_type = dest_type
        self.flow_log_id = flow_log_id
        fid = f" (flow log {flow_log_id})" if flow_log_id else ""
        super().__init__(
            f"unsupported VPC flow-log destination type {dest_type!r}{fid}: no record collector is "
            f"implemented for it. Implemented: {sorted(FLOW_LOG_READERS)}."
        )


# --------------------------------------------------------------------------- #
# Resilient flow-log fetch (§5.7, §9) — a shared classifier + retry wrapper + trusted-time
# clock check + failure-rate safeguard, applied MANDATORILY to BOTH record readers so a single
# failed AWS call (a corrupt S3 object, a missing CloudWatch group, a transient network stall)
# degrades to a best-effort partial graph instead of aborting the whole run.
# --------------------------------------------------------------------------- #

# Retry backoff for transient failures (tier 1's network case + tier 3): up to 3 retries after the
# initial attempt, sleeping this many seconds before each. Kept as data so tests assert the order.
_RETRY_BACKOFF: tuple[int, ...] = (30, 60, 120)

# SigV4 rejects a request whose timestamp is more than ~15 min from AWS's clock
# (``RequestTimeTooSkewed``). Beyond this offset from a *trusted* external clock we call it a real
# local-clock problem and abort; within it (or if the trusted time can't be fetched) we treat the
# skew error as a transient network stall and retry.
_CLOCK_SKEW_TOLERANCE_S: float = 900.0
_TRUSTED_TIME_TIMEOUT_S: float = 5.0
_TRUSTED_TIME_URL = "https://www.google.com/"
_NTP_HOST = "pool.ntp.org"
_NTP_PORT = 123
_NTP_UNIX_EPOCH_DELTA = 2208988800  # seconds between the NTP (1900) and Unix (1970) epochs

# Failure-rate safeguard thresholds (both sources): abort rather than return a silent near-empty
# graph. Trips on the first N units failing in a row, or on > threshold of a large-enough sample.
_FAILURE_STREAK_ABORT = 5
_FAILURE_MIN_SAMPLE = 4
_FAILURE_RATE_THRESHOLD = 0.5

# S3 flow-log objects are read **largest first** (see :func:`_size_descending_keys`): an all-NODATA
# object compresses tiny (repetitive ``- - -`` lines), so descending size front-loads the
# data-bearing objects and pushes NODATA to the tail. Sizes come free from ``list-objects-v2`` (no
# extra call). Two consecutive-**zero-record** streak thresholds then decide when to stop reading a
# source (``docs/02_architecture.md §5.7``):
#
# * ``_FLOW_LOG_PROBE_OBJECTS`` — cold-start probe. If a source yields **zero** records across this
#   many of its largest objects, it's all-NODATA/unrecognised: skip the rest. Kept conservative (a
#   representative sample) because we have no positive evidence yet. If *every* source ends empty
#   the run fails loudly rather than build an empty graph.
# * ``_FLOW_LOG_TAIL_STREAK`` — tail trim. **After** a source has yielded records, this many
#   consecutive NODATA objects (now in the small-size tail) is strong evidence the rest of the tail
#   is dead too — skip the remaining, smaller objects. Short because we already have positive
#   evidence the source is real; the cost of over-skipping is at most a few tiny data objects, and
#   NODATA is dropped by design anyway.
_FLOW_LOG_PROBE_OBJECTS = 25
_FLOW_LOG_TAIL_STREAK = 3


class CredentialsExpiredError(RuntimeError):
    """Credentials / session token expired mid-fetch (``ExpiredToken``/``InvalidToken``/…).

    Propagates to :func:`cloudbreachgraph.cli.main`, which re-runs ``aws sso login`` for **every**
    configured profile and retries the whole run once (``docs/02_architecture.md §9``). Raising a
    dedicated type (rather than a bare :class:`~cloudbreachgraph.aws.runner.AwsCliError`) keeps the
    error-gated ``aws sso login`` reaction strictly tied to an expired-token cause."""


class FlowLogFetchError(RuntimeError):
    """A flow-log fetch aborts the run: a **systemic** AWS error (auth / clock skew / bad signature)
    or **too many** per-unit failures (the failure-rate safeguard). Caught in
    :func:`cloudbreachgraph.cli.main` → non-zero exit. Distinct from
    :class:`FlowLogDestinationError` (an unsupported destination *type*): this is a fetch that
    *should* have worked failing in a way that makes a partial graph misleading, not incomplete."""


class FlowLogCoverageError(RuntimeError):
    """The discovered flow-log config doesn't cover the discovered VPCs (``§5.7`` coverage check).

    Raised **before** any S3 download when the ``describe-flow-logs`` result references **none** of
    the discovered ``vpc-``/``subnet-``/``eni-`` ids — the exact symptom of querying the flow-log
    configuration in the **wrong account** (the archive account instead of the VPC/network account),
    which returns that account's unrelated flow logs with no error. Also raised on the fetch side
    when **every** covered VPC yields zero in-window objects. Caught in
    :func:`cloudbreachgraph.cli.main` → non-zero exit. Distinct from :class:`FlowLogFetchError`
    (a fetch that should have worked failing): this is a *config/account mismatch*, not a fetch
    failure — an auth error can't catch it because the wrong-account query *succeeds*."""


class _S3AccessDeniedError(RuntimeError):
    """S3 ``list``/``get`` was denied (``AccessDenied``/``Forbidden``) for one profile.

    Internal signal that drives the archive-account auto-resolution fallback
    (``docs/02_architecture.md §5.7`` design guidance B) rather than a hard abort: the S3 read is
    retried under the next configured profile. Only surfaced by :func:`_run_unit` when
    ``access_denied_signals=True`` (the auto-resolution list probe); otherwise an ``AccessDenied``
    still aborts via :class:`FlowLogFetchError` as before (e.g. an explicit binding, or a per-object
    ``get`` after the account is already resolved)."""


class _SkippableUnitError(RuntimeError):
    """A single unit is unreadable in a way that is safe to skip (corrupt gzip, decode error).

    Raised by the per-object reader so :func:`_run_unit` warns + skips it and counts it toward the
    failure-rate safeguard — the same best-effort path as a skippable AWS error (tier 5)."""


class _ErrorTier(Enum):
    """How a failed AWS unit is routed (``docs/02_architecture.md §5.7`` design guidance A)."""

    CLOCK_SKEW = "clock_skew"  # RequestTimeTooSkewed — decide clock-vs-network, maybe retry
    EXPIRED = "expired"  # ExpiredToken/InvalidToken/… — re-login + retry the run once
    TRANSIENT = "transient"  # timeout/throttle/5xx/reset — retry with backoff, then skip
    SYSTEMIC = "systemic"  # AccessDenied/SignatureDoesNotMatch/… — abort with an actionable message
    SKIPPABLE = "skippable"  # NoSuchKey/ResourceNotFoundException/unclassified — warn + skip


_EXPIRED_TOKENS = ("ExpiredToken", "ExpiredTokenException", "InvalidToken", "TokenRefreshRequired")
# Permission-denied tokens (a subset of the systemic tier). For the S3 auto-resolution list probe
# these drive the archive-account fallback (:class:`_S3AccessDeniedError`) instead of aborting; a
# real signing/clock systemic error (``SignatureDoesNotMatch``) always aborts and is excluded here.
_ACCESS_DENIED_TOKENS = ("AccessDenied", "Forbidden", "AuthorizationHeaderMalformed")
_SYSTEMIC_TOKENS = (*_ACCESS_DENIED_TOKENS, "SignatureDoesNotMatch")
_TRANSIENT_TOKENS = (
    "RequestTimeout",
    "SlowDown",
    "Throttling",  # also matches ThrottlingException
    "InternalError",
    "InternalFailure",
    "ServiceUnavailable",
    "Connection reset",
    "connection reset",
    "Could not connect",
    "ConnectionError",
    "EndpointConnectionError",
    "Read timeout",
    "timed out",
)


def _classify_aws_error(stderr: str | None) -> _ErrorTier:
    """Route one AwsCliError stderr into an :class:`_ErrorTier`.

    Order matters: skew and expiry are decided before the systemic/transient buckets so an
    ``ExpiredToken`` never falls through to "unclassified → skip"."""
    s = stderr or ""
    if "RequestTimeTooSkewed" in s:
        return _ErrorTier.CLOCK_SKEW
    if any(tok in s for tok in _EXPIRED_TOKENS):
        return _ErrorTier.EXPIRED
    if any(tok in s for tok in _SYSTEMIC_TOKENS):
        return _ErrorTier.SYSTEMIC
    if any(tok in s for tok in _TRANSIENT_TOKENS):
        return _ErrorTier.TRANSIENT
    return _ErrorTier.SKIPPABLE


def is_expired_error(exc: BaseException) -> bool:
    """Whether ``exc`` means "credentials expired" — either the dedicated
    :class:`CredentialsExpiredError` or an :class:`~cloudbreachgraph.aws.runner.AwsCliError` whose
    stderr classifies as :attr:`_ErrorTier.EXPIRED`. Used by ``cli.main`` to gate the SSO re-login
    for expired tokens raised **anywhere** in the run, not only from the flow-log readers."""
    if isinstance(exc, CredentialsExpiredError):
        return True
    if isinstance(exc, runner.AwsCliError):
        return _classify_aws_error(exc.stderr) is _ErrorTier.EXPIRED
    return False


# --- Trusted external time (stdlib only), for the clock-vs-network decision -------------------- #
def _http_date_epoch() -> float | None:
    """Trusted epoch seconds from an HTTPS ``Date`` response header, or ``None`` on any failure."""
    req = urllib.request.Request(_TRUSTED_TIME_URL, method="HEAD")
    with urllib.request.urlopen(req, timeout=_TRUSTED_TIME_TIMEOUT_S) as resp:  # noqa: S310 (https)
        date_hdr = resp.headers.get("Date")
    if not date_hdr:
        return None
    return email.utils.parsedate_to_datetime(date_hdr).timestamp()


def _sntp_epoch() -> float | None:
    """Trusted epoch seconds from a minimal SNTP (NTP) UDP query, or ``None`` on any failure."""
    packet = b"\x1b" + 47 * b"\x00"  # LI=0, VN=3, Mode=3 (client); rest zero
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(_TRUSTED_TIME_TIMEOUT_S)
        sock.sendto(packet, (_NTP_HOST, _NTP_PORT))
        data, _ = sock.recvfrom(48)
    finally:
        sock.close()
    if len(data) < 44:
        return None
    seconds = struct.unpack("!I", data[40:44])[0]
    return float(seconds - _NTP_UNIX_EPOCH_DELTA)


def _trusted_time_offset() -> float | None:
    """Offset in seconds between a **trusted external clock** and the local UTC clock, or ``None``.

    Positive ⇒ the local clock is *behind* true time; negative ⇒ *ahead*. Tries an HTTPS ``Date``
    header first, then an SNTP query; returns ``None`` if neither can be reached (so the caller
    treats an unverifiable skew as transient, not a clock problem). Uses ``time.time()`` for the
    local reference — mockable, like ``time.sleep``, so tests stay offline and fast."""
    for fetch in (_http_date_epoch, _sntp_epoch):
        try:
            remote = fetch()
        except Exception:  # noqa: BLE001 — any network/parse failure ⇒ "can't fetch trusted time"
            remote = None
        if remote is not None:
            return remote - time.time()
    return None


def _clock_skew_message(offset: float) -> str:
    direction = "ahead of" if offset < 0 else "behind"
    return (
        f"aborting flow-log fetch: the system clock is ~{abs(offset) / 60:.1f} min {direction} the "
        f"true time (AWS rejected the request as RequestTimeTooSkewed and a trusted external time "
        f"source confirms the skew). Sync your clock and re-run (macOS: System Settings → "
        f"General → Date & Time, 'Set time automatically')."
    )


# --- Per-unit outcome, the retry wrapper, and the failure-rate tracker ------------------------- #
@dataclass
class _FetchOutcome:
    """The result of running one unit through :func:`_run_unit`."""

    value: Any  # the fetch's return on success, else None
    ok: bool  # succeeded (possibly after retries)?
    error: str = ""  # short cause when skipped, for the diagnostic's "last error"


def _short_cause(exc: BaseException) -> str:
    """A one-line cause for a warning/diagnostic, from an AwsCliError stderr or an exception str."""
    if isinstance(exc, runner.AwsCliError):
        line = (exc.stderr or "").strip().splitlines()
        return line[0] if line else f"exit {exc.returncode}"
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


def _is_access_denied(stderr: str | None) -> bool:
    """Whether a systemic error is a *permission* denial (drives the S3 archive fallback), as
    opposed to a signing/clock systemic error (``SignatureDoesNotMatch``, which always aborts)."""
    s = stderr or ""
    return any(tok in s for tok in _ACCESS_DENIED_TOKENS)


def _run_unit(
    fetch: Callable[[], Any],
    *,
    source: str,
    unit: str,
    iam_hint: str,
    access_denied_signals: bool = False,
) -> _FetchOutcome:
    """Run one unit fetch (an S3 object, or a CloudWatch group) under the shared resilience rules.

    ``fetch`` performs the AWS call(s) and returns the unit's value; it is re-invoked from scratch
    on each retry so a retried request re-signs with a current timestamp. ``source`` is
    ``"s3"``/``"cloud-watch-logs"`` and ``iam_hint`` names the IAM a systemic auth failure would
    need — both only for messages. Returns a :class:`_FetchOutcome`; raises
    :class:`CredentialsExpiredError` (tier 2) or :class:`FlowLogFetchError` (tier 1 real-clock /
    tier 4 systemic) to abort/propagate.

    When ``access_denied_signals`` is set (the S3 auto-resolution *list* probe), a permission
    denial (``AccessDenied``/``Forbidden``) raises :class:`_S3AccessDeniedError` instead of
    aborting,
    so the caller can retry under the next configured profile; ``SignatureDoesNotMatch`` still
    aborts."""
    attempt = 0
    while True:
        try:
            return _FetchOutcome(fetch(), True)
        except _SkippableUnitError as exc:
            _warn_skip(source, unit, _short_cause(exc))
            return _FetchOutcome(None, False, _short_cause(exc))
        except runner.AwsCliError as exc:
            tier = _classify_aws_error(exc.stderr)
            if tier is _ErrorTier.EXPIRED:
                raise CredentialsExpiredError(
                    f"credentials expired while fetching {source} unit {unit}: {_short_cause(exc)}"
                ) from exc
            if tier is _ErrorTier.SYSTEMIC:
                if access_denied_signals and _is_access_denied(exc.stderr):
                    raise _S3AccessDeniedError(_short_cause(exc)) from exc
                raise FlowLogFetchError(_systemic_message(source, unit, iam_hint, exc)) from exc
            if tier is _ErrorTier.CLOCK_SKEW:
                offset = _trusted_time_offset()
                if offset is not None and abs(offset) > _CLOCK_SKEW_TOLERANCE_S:
                    raise FlowLogFetchError(_clock_skew_message(offset)) from exc
                # within tolerance, or trusted time unfetchable ⇒ treat as transient ⇒ back off.
            elif tier is _ErrorTier.SKIPPABLE:
                _warn_skip(source, unit, _short_cause(exc))
                return _FetchOutcome(None, False, _short_cause(exc))

            # TRANSIENT, or a CLOCK_SKEW judged to be a network stall: retry with backoff.
            if attempt >= len(_RETRY_BACKOFF):
                _warn_skip(source, unit, _short_cause(exc), exhausted=True)
                return _FetchOutcome(None, False, _short_cause(exc))
            delay = _RETRY_BACKOFF[attempt]
            if runner.is_verbose():
                print(
                    f"cloudbreachgraph: {source} unit {unit}: transient error "
                    f"({_short_cause(exc)}); retry {attempt + 1}/{len(_RETRY_BACKOFF)} in {delay}s",
                    file=sys.stderr,
                )
            time.sleep(delay)
            attempt += 1


def _systemic_message(source: str, unit: str, iam_hint: str, exc: runner.AwsCliError) -> str:
    stderr = exc.stderr or ""
    if "SignatureDoesNotMatch" in stderr:
        detail = (
            "SignatureDoesNotMatch — the request signature did not validate, usually a corrupted "
            "secret key or a badly-skewed clock. Check the profile's credentials and system clock."
        )
    else:
        detail = f"access denied — the profile needs {iam_hint}."
    return f"aborting flow-log fetch: {source} unit {unit}: {detail}\n{exc}"


def _warn_skip(source: str, unit: str, cause: str, *, exhausted: bool = False) -> None:
    """Warn (stderr) that one best-effort unit was skipped, naming the object/group and cause."""
    why = "retries exhausted" if exhausted else "skipped"
    print(
        f"cloudbreachgraph: warning: flow-log {source} unit {unit} {why}: {cause}",
        file=sys.stderr,
    )


@dataclass
class _FailureTracker:
    """Track attempted vs failed units for one source; abort (raise) if too many fail (§5.7 A.6)."""

    source: str
    attempted: int = 0
    failed: int = 0
    streak: int = 0
    last_error: str = ""

    def record(self, outcome: _FetchOutcome) -> None:
        self.attempted += 1
        if outcome.ok:
            self.streak = 0
        else:
            self.failed += 1
            self.streak += 1
            if outcome.error:
                self.last_error = outcome.error
            self._check()

    def _check(self) -> None:
        streak_trip = self.streak >= _FAILURE_STREAK_ABORT
        rate_trip = (
            self.attempted >= _FAILURE_MIN_SAMPLE
            and self.failed / self.attempted > _FAILURE_RATE_THRESHOLD
        )
        if streak_trip or rate_trip:
            raise FlowLogFetchError(
                f"aborting flow-log fetch: too many {self.source} fetches failed "
                f"({self.failed}/{self.attempted}); last error: {self.last_error or 'unknown'}. "
                f"Refusing to build a graph from a near-empty flow-log set."
            )


# --------------------------------------------------------------------------- #
# VPC coverage reconciliation (§5.7 design guidance C) — validate that the discovered flow-log
# config actually references the discovered VPCs *before* downloading a single object, so a
# config-in-wrong-account mistake fails fast instead of after thousands of unrelated gets.
# --------------------------------------------------------------------------- #
# A VPC flow-log id as it appears in an S3 object key (``..._vpcflowlogs_<region>_fl-<hex>_...``)
# and as ``describe-flow-logs`` returns it. Used by the precision filter + per-VPC accounting.
_FLOW_LOG_ID_RE = re.compile(r"fl-[0-9a-f]+")
# The source account id in an S3 flow-log key path ``AWSLogs/<account-id>/...`` (a 12-digit id). It
# is the account the VPCs live in (the network account), used as a secondary precision guard.
_AWS_LOGS_ACCOUNT_RE = re.compile(r"AWSLogs/(\d{12})/")


@dataclass
class FlowLogCoverage:
    """Reconciliation of discovered flow logs against the discovered VPC/subnet/ENI universe.

    Built by :func:`check_vpc_coverage` from the network account's ``vpcs``/``subnets``/
    ``network_interfaces`` and the ``describe-flow-logs`` result. Drives the coverage hard-fail
    (config queried in the wrong account), the uncovered-VPC warning, the S3 precision filter
    (``in_scope_fl_ids``) and the per-VPC completeness accounting (``fl_to_vpc``)."""

    vpcs_total: int
    covered_vpcs: list[str]  # discovered VPCs with ≥1 flow log (sorted)
    uncovered_vpcs: list[str]  # discovered VPCs with no flow log (sorted)
    in_scope_fl_ids: set[str]  # flow-log ids referencing a discovered vpc/subnet/eni
    fl_to_vpc: dict[str, str]  # in-scope flow-log id -> the discovered VPC it maps to

    def meta(self, per_vpc: dict[str, dict[str, int]] | None = None) -> dict:
        """A deterministic ``meta.flow_log_coverage`` payload (sorted; no wall-clock)."""
        out: dict = {
            "vpcs_total": self.vpcs_total,
            "vpcs_covered": len(self.covered_vpcs),
            "covered_vpcs": sorted(self.covered_vpcs),
            "uncovered_vpcs": sorted(self.uncovered_vpcs),
        }
        if per_vpc is not None:
            out["per_vpc"] = {
                vpc: {
                    "objects": per_vpc.get(vpc, {}).get("objects", 0),
                    "records": per_vpc.get(vpc, {}).get("records", 0),
                }
                for vpc in sorted(self.covered_vpcs)
            }
        return out


def check_vpc_coverage(
    flow_logs: list[dict],
    vpcs: list[dict],
    subnets: list[dict],
    enis: list[dict],
) -> FlowLogCoverage:
    """Reconcile the discovered flow logs against the discovered VPCs (``§5.7`` guidance C).

    A flow log "covers" a discovered resource when its ``ResourceId`` (a ``vpc-``/``subnet-``/
    ``eni-`` id) maps up to a discovered VPC. Raises :class:`FlowLogCoverageError` — **before** any
    S3 I/O — when flow logs exist but reference **none** of the discovered VPCs (the cross-account
    symptom). VPCs with no flow log are reported (not fatal) via the returned ``uncovered_vpcs``."""
    vpc_ids = {v.get("VpcId") for v in vpcs if v.get("VpcId")}
    subnet_to_vpc = {s.get("SubnetId"): s.get("VpcId") for s in subnets if s.get("SubnetId")}
    eni_to_vpc = {
        e.get("NetworkInterfaceId"): e.get("VpcId") for e in enis if e.get("NetworkInterfaceId")
    }

    def _vpc_of(resource_id: str | None) -> str | None:
        if not resource_id:
            return None
        if resource_id.startswith("vpc-"):
            return resource_id if resource_id in vpc_ids else None
        if resource_id.startswith("subnet-"):
            vpc = subnet_to_vpc.get(resource_id)
            return vpc if vpc in vpc_ids else None
        if resource_id.startswith("eni-"):
            vpc = eni_to_vpc.get(resource_id)
            return vpc if vpc in vpc_ids else None
        return None

    in_scope_fl_ids: set[str] = set()
    fl_to_vpc: dict[str, str] = {}
    covered: set[str] = set()
    for fl in flow_logs:
        vpc = _vpc_of(fl.get("ResourceId"))
        if vpc is None:
            continue
        covered.add(vpc)
        fid = fl.get("FlowLogId")
        if fid:
            in_scope_fl_ids.add(fid)
            fl_to_vpc[fid] = vpc

    uncovered = sorted(vpc_ids - covered)  # type: ignore[operator]
    coverage = FlowLogCoverage(
        vpcs_total=len(vpc_ids),
        covered_vpcs=sorted(covered),
        uncovered_vpcs=uncovered,
        in_scope_fl_ids=in_scope_fl_ids,
        fl_to_vpc=fl_to_vpc,
    )

    # Hard fail (the cross-account symptom): flow logs exist and VPCs were discovered, but no flow
    # log references any of them — describe-flow-logs was almost certainly queried in the wrong
    # account. Fail here, before downloading anything.
    if flow_logs and vpc_ids and not covered:
        raise FlowLogCoverageError(
            f"aborting flow-log analysis: discovered {len(vpc_ids)} VPC(s) but "
            f"describe-flow-logs returned {len(flow_logs)} flow log(s), none referencing any of "
            f"them (their ResourceIds are all foreign). The flow-log configuration was almost "
            f"certainly queried in the wrong account — it must run in the VPC/network account, not "
            f"the log-archive account. Check the `network` vs `flow_logs` target binding."
        )
    return coverage


def _flow_log_id_from_key(key: str) -> str | None:
    """The ``fl-…`` id embedded in an S3 flow-log object filename, or ``None`` (§5.7 precision)."""
    m = _FLOW_LOG_ID_RE.search(key)
    return m.group(0) if m else None


def _aws_logs_account_from_key(key: str) -> str | None:
    """The source account id from an ``AWSLogs/<account-id>/`` S3 key segment, or ``None``."""
    m = _AWS_LOGS_ACCOUNT_RE.search(key)
    return m.group(1) if m else None


def _warn_uncovered_vpcs(coverage: FlowLogCoverage) -> None:
    """Warn (stderr, non-fatal per §9) about discovered VPCs with **no** flow log configured."""
    if coverage.uncovered_vpcs:
        print(
            f"cloudbreachgraph: warning: {len(coverage.uncovered_vpcs)} discovered VPC(s) have no "
            f"flow log configured (no flow-log analysis for them): "
            f"{', '.join(coverage.uncovered_vpcs)}.",
            file=sys.stderr,
        )


def collect_flow_log_records(profile: str | None, region: str | None) -> list[dict]:
    """Fetch and parse the flow-log *records* for the account's flow logs, per destination type.

    ``describe-flow-logs`` says *where* each flow log delivers (``LogDestinationType``); this
    dispatches to the reader for that type (:data:`FLOW_LOG_READERS`) so it always pulls from the
    right source — CloudWatch Logs (``logs filter-log-events``) or S3 (``s3api list-objects-v2`` +
    ``get-object`` on the gzipped objects). A flow log whose destination type has **no** implemented
    reader raises :class:`FlowLogDestinationError` (``docs/02_architecture.md §5.7``). Each reader
    reads up to the configured :func:`get_flow_log_window` days back (``--flow-log-days N``, default
    :data:`FLOW_LOG_MAX_LOOKBACK_DAYS`) and is read-only. Returns a flat list
    of normalized flow records; emits a one-line stderr diagnostic so an empty result is
    explainable."""
    config = runner.run_aws(["ec2", "describe-flow-logs"], profile=profile, region=region)
    flow_logs = config.get("FlowLogs", [])

    by_type, dest_counts = _group_flow_logs_by_dest(flow_logs)

    since_epoch, until_epoch = _flow_log_window_bounds()
    records: list[dict] = []
    fetched_by_type: dict[str, int] = {}
    skipped_by_type: dict[str, int] = {}
    for dest, fls in by_type.items():
        recs, fetched, skipped = FLOW_LOG_READERS[dest](
            fls, profile, region, since_epoch, until_epoch
        )
        records.extend(recs)
        fetched_by_type[dest] = fetched
        skipped_by_type[dest] = skipped

    _report_flow_log_records(flow_logs, dest_counts, fetched_by_type, skipped_by_type, len(records))
    return records


def _group_flow_logs_by_dest(
    flow_logs: list[dict],
) -> tuple[dict[str | None, list[dict]], dict[str, int]]:
    """Group flow logs by ``LogDestinationType`` and validate each has an implemented reader.

    Returns ``(by_type, dest_counts)``; raises :class:`FlowLogDestinationError` for a destination
    type with no reader — **before** any partial fetch work (``docs/02_architecture.md §5.7``)."""
    by_type: dict[str | None, list[dict]] = {}
    dest_counts: dict[str, int] = {}
    for fl in flow_logs:
        dest = fl.get("LogDestinationType")
        dest_counts[dest or "unknown"] = dest_counts.get(dest or "unknown", 0) + 1
        by_type.setdefault(dest, []).append(fl)
    for dest, fls in by_type.items():
        if dest not in FLOW_LOG_READERS:
            raise FlowLogDestinationError(dest, fls[0].get("FlowLogId"))
    return by_type, dest_counts


def fetch_flow_log_records(
    flow_logs: list[dict],
    network: ResolvedAccount,
    archive: ArchiveAccess,
    *,
    coverage: FlowLogCoverage | None = None,
    network_account_id: str | None = None,
) -> tuple[list[dict], dict[str, dict[str, int]], ResolvedAccount | None]:
    """Fetch flow-log *records* across two accounts (``§5.7`` cross-account split), given the config
    already discovered (in the **network** account — queried once, not re-run here).

    CloudWatch reads (``logs filter-log-events``) run under ``network`` (the log group lives with
    the VPCs); S3 reads (``list-objects-v2`` + ``get-object``) run under the **archive** account
    resolved by :class:`_ArchiveResolver` (explicit binding, else primary→AccessDenied fallback).
    ``coverage``
    scopes the S3 download to the in-scope ``fl-…`` objects (precision) and drives per-VPC
    object/record accounting (completeness). Returns ``(records, per_vpc, resolved_archive)`` — the
    per-VPC counts and the account the S3 objects were actually read under (for provenance)."""
    by_type, dest_counts = _group_flow_logs_by_dest(flow_logs)
    since_epoch, until_epoch = _flow_log_window_bounds()
    fl_to_vpc = coverage.fl_to_vpc if coverage else None
    # Only scope by fl-id when a discovered VPC universe exists; with no VPCs (e.g. a thin cache) an
    # empty in-scope set would wrongly drop every object, so fall back to no precision filter.
    allowed = coverage.in_scope_fl_ids if (coverage and coverage.vpcs_total) else None

    resolver = _ArchiveResolver(archive)
    per_vpc: dict[str, dict[str, int]] = {}
    records: list[dict] = []
    fetched_by_type: dict[str, int] = {}
    skipped_by_type: dict[str, int] = {}
    for dest, fls in by_type.items():
        if dest == "s3":
            recs, fetched, skipped = _read_s3_records(
                fls,
                network.profile,
                network.region,
                since_epoch,
                until_epoch,
                resolver=resolver,
                allowed_fl_ids=allowed,
                fl_to_vpc=fl_to_vpc,
                network_account_id=network_account_id,
                per_vpc=per_vpc,
            )
        else:  # cloud-watch-logs — validated by _group_flow_logs_by_dest
            recs, fetched, skipped = _read_cloudwatch_records(
                fls,
                network.profile,
                network.region,
                since_epoch,
                until_epoch,
                fl_to_vpc=fl_to_vpc,
                per_vpc=per_vpc,
            )
        records.extend(recs)
        fetched_by_type[dest] = fetched
        skipped_by_type[dest] = skipped

    _report_flow_log_records(flow_logs, dest_counts, fetched_by_type, skipped_by_type, len(records))
    return records, per_vpc, resolver.resolved


def _read_cloudwatch_records(
    flow_logs: list[dict],
    profile: str | None,
    region: str | None,
    since_epoch: float,
    until_epoch: float | None = None,
    *,
    fl_to_vpc: dict[str, str] | None = None,
    per_vpc: dict[str, dict[str, int]] | None = None,
) -> tuple[list[dict], int, int]:
    """Read records from each CloudWatch log group (``logs filter-log-events``). Returns
    ``(records, events_fetched, groups_skipped)``. Each group's fields come from its own
    ``LogFormat``. ``until_epoch`` (from ``--flow-log-end``) bounds the query with ``--end-time``
    when set. Each group is one **unit** run through :func:`_run_unit`, so a missing group
    (``ResourceNotFoundException``) or a transient stall on one group is warned + skipped (with
    backoff/retry) while the others are read — a systemic error (``AccessDenied``) still aborts.

    The CloudWatch reader runs under the **network** account (the log group lives with the VPCs).
    When ``per_vpc`` is provided, each group's events/records are attributed to the VPC(s) the group
    belongs to (via ``fl_to_vpc``) for the completeness accounting (§5.7 guidance D)."""
    group_fields: dict[str, dict[str, int]] = {}
    group_to_vpcs: dict[str, set[str]] = {}
    for fl in flow_logs:
        group = fl.get("LogGroupName")
        if group and group not in group_fields:
            fields = _field_index_from_format(fl.get("LogFormat"))
            if fields is not None:
                group_fields[group] = fields
        if group and fl_to_vpc is not None:
            vpc = fl_to_vpc.get(fl.get("FlowLogId"))
            if vpc:
                group_to_vpcs.setdefault(group, set()).add(vpc)

    start_ms = int(since_epoch * 1000)
    end_ms = int(until_epoch * 1000) if until_epoch is not None else None
    records: list[dict] = []
    fetched = 0
    tracker = _FailureTracker("cloud-watch-logs")
    for group in sorted(group_fields):

        def _fetch(g: str = group) -> list[dict]:
            args = [
                "logs",
                "filter-log-events",
                f"--log-group-name={g}",
                f"--start-time={start_ms}",
            ]
            if end_ms is not None:
                args.append(f"--end-time={end_ms}")
            data = runner.run_aws(args, profile=profile, region=region)
            return data.get("events", [])

        outcome = _run_unit(
            _fetch,
            source="cloud-watch-logs",
            unit=group,
            iam_hint=f"logs:FilterLogEvents on log group '{group}'",
        )
        tracker.record(outcome)
        if not outcome.ok:
            continue
        events = outcome.value
        fetched += len(events)
        parsed_here = 0
        for event in events:
            rec = _parse_flow_log_message(event.get("message", ""), group, group_fields[group])
            if rec is not None:
                records.append(rec)
                parsed_here += 1
        if per_vpc is not None:
            for vpc in group_to_vpcs.get(group, set()):
                acc = per_vpc.setdefault(vpc, {"objects": 0, "records": 0})
                acc["objects"] += len(events)
                acc["records"] += parsed_here
    return records, fetched, tracker.failed


class _ArchiveResolver:
    """Resolve which account the S3 flow-log objects are read under (``§5.7`` design guidance B).

    An explicit ``flow_logs`` binding is used directly (no trial). Otherwise the **primary**
    (network) account is tried first — the ``list-objects-v2`` we already make *is* the access probe
    (no separate probe, no wasted ``get-object``) — and on an ``AccessDenied``/``Forbidden`` the
    read
    falls back through the configured candidate profiles, first success wins. The working account is
    remembered so later sources try it first, and the per-object ``get-object`` calls run under it.
    """

    def __init__(self, archive: ArchiveAccess) -> None:
        self.archive = archive
        self.resolved: ResolvedAccount | None = archive.explicit

    def _ordered_accounts(self) -> list[ResolvedAccount]:
        if self.archive.explicit is not None:
            return [self.archive.explicit]
        ordered: list[ResolvedAccount] = []
        for acct in (self.resolved, self.archive.primary, *self.archive.candidates):
            if acct is not None and all(acct.profile != s.profile for s in ordered):
                ordered.append(acct)
        return ordered

    def list_source(
        self, bucket: str, prefix: str, since: float, until: float | None, iam: str
    ) -> tuple[_FetchOutcome, ResolvedAccount]:
        """List one source, resolving the account. Returns ``(list_outcome, account)``; the account
        is what the caller reads objects under. Raises :class:`FlowLogFetchError` when every
        candidate is denied (auto-resolution exhausted) — naming the bucket and profiles tried."""
        accounts = self._ordered_accounts()
        auto = self.archive.explicit is None
        denied: list[ResolvedAccount] = []
        for acct in accounts:
            try:
                outcome = _run_unit(
                    lambda a=acct: _list_s3_flow_log_objects(
                        bucket, prefix, since, until, a.profile, a.region
                    ),
                    source="s3",
                    unit=f"s3://{bucket}/{prefix} (list)",
                    iam_hint=iam,
                    access_denied_signals=auto,
                )
            except _S3AccessDeniedError:
                denied.append(acct)
                continue
            if outcome.ok:
                self.resolved = acct
            return outcome, acct
        raise FlowLogFetchError(_archive_unresolved_message(bucket, denied))


def _archive_unresolved_message(bucket: str, tried: list[ResolvedAccount]) -> str:
    profiles = ", ".join(a.profile or "<default>" for a in tried) or "<none>"
    return (
        f"aborting flow-log fetch: none of the tried profiles could read the flow-log archive "
        f"bucket '{bucket}' (AccessDenied under [{profiles}]). Bind the archive account explicitly "
        f'with a [targets.<name>.roles] entry (e.g. `flow_logs = "<archive-account-alias>"`), or '
        f"grant the primary profile s3:ListBucket + s3:GetObject on the bucket."
    )


def _filter_objects_in_scope(
    objects: list[tuple[str, int]],
    allowed_fl_ids: set[str] | None,
    network_account_id: str | None,
) -> list[tuple[str, int]]:
    """Keep only the in-scope flow-log objects (``§5.7`` guidance D precision).

    Drops an object whose embedded ``fl-…`` id is **not** in ``allowed_fl_ids`` (the flow logs whose
    ``ResourceId`` maps to a discovered VPC), and — as a secondary guard — one whose
    ``AWSLogs/<acct>/`` segment names a **different** account than ``network_account_id``. Objects
    with no extractable id/account are kept (a non-standard key can't be proven out-of-scope). With
    both filters unset this is a no-op (the legacy single-account path)."""
    if allowed_fl_ids is None and network_account_id is None:
        return objects
    kept: list[tuple[str, int]] = []
    for key, size in objects:
        if allowed_fl_ids is not None:
            fid = _flow_log_id_from_key(key)
            if fid is not None and fid not in allowed_fl_ids:
                continue
        if network_account_id is not None:
            acct = _aws_logs_account_from_key(key)
            if acct is not None and acct != network_account_id:
                continue
        kept.append((key, size))
    return kept


def _parse_s3_arn(arn: str | None) -> tuple[str, str] | None:
    """Split an S3 ``LogDestination`` ARN (``arn:aws:s3:::bucket/prefix``) into ``bucket, prefix``.

    ``prefix`` may be empty (the bucket root). Returns ``None`` for a malformed ARN.
    """
    if not arn or ":::" not in arn:
        return None
    bucket, _, prefix = arn.split(":::", 1)[1].partition("/")
    return (bucket, prefix) if bucket else None


def _read_s3_records(
    flow_logs: list[dict],
    profile: str | None,
    region: str | None,
    since_epoch: float,
    until_epoch: float | None = None,
    *,
    resolver: _ArchiveResolver | None = None,
    allowed_fl_ids: set[str] | None = None,
    fl_to_vpc: dict[str, str] | None = None,
    network_account_id: str | None = None,
    per_vpc: dict[str, dict[str, int]] | None = None,
) -> tuple[list[dict], int, int]:
    """Read records from each S3 destination: list the gzipped objects modified within the window
    (``s3api list-objects-v2``), download and parse each (``s3api get-object`` + gunzip). Returns
    ``(records, objects_read, objects_skipped)``. ``until_epoch`` (from ``--flow-log-end``) upper-
    bounds the objects' ``LastModified`` when set. Distinct ``(bucket, prefix)`` sources are read
    once. The per-source *list* and each per-object *get* are units run through :func:`_run_unit`,
    so a corrupt/missing object (``NoSuchKey``/bad gzip) or a transient stall is warned + skipped
    (with backoff) while the rest are read — a systemic error (``AccessDenied``) still aborts.

    When ``resolver`` is given (the cross-account path) the list + gets run under the **archive**
    account it resolves (primary → AccessDenied fallback), not the passed ``profile``/``region``.
    ``allowed_fl_ids``/``network_account_id`` scope the download to the in-scope objects (precision,
    §5.7 D) and ``per_vpc``/``fl_to_vpc`` accumulate per-VPC object/record counts (completeness).

    Each source is read **largest object first** (:func:`_size_descending_keys`) and stops early on
    a run of NODATA (empty parses): a cold source with no records is abandoned after
    ``_FLOW_LOG_PROBE_OBJECTS`` consecutive empties (all-NODATA/unrecognised); a source that has
    already yielded records has its smaller NODATA tail trimmed after ``_FLOW_LOG_TAIL_STREAK``
    consecutive empties. One empty VPC never aborts a run whose other VPCs have data; the run raises
    :class:`FlowLogFetchError` only if **every** source came back empty (and enough objects were
    read to be sure)."""
    sources: dict[tuple[str, str], None] = {}
    for fl in flow_logs:
        bp = _parse_s3_arn(fl.get("LogDestination"))
        if bp is not None:
            sources.setdefault(bp, None)

    records: list[dict] = []
    objects_read = 0
    tracker = _FailureTracker("s3")
    empty_sources: list[str] = []  # sources abandoned as all-NODATA (largest objects parse nothing)
    for bucket, prefix in sorted(sources):
        iam = f"s3:ListBucket and s3:GetObject on bucket '{bucket}'"
        if resolver is not None:
            objects_outcome, acct = resolver.list_source(
                bucket, prefix, since_epoch, until_epoch, iam
            )
            get_profile, get_region = acct.profile, acct.region
        else:
            objects_outcome = _run_unit(
                lambda b=bucket, p=prefix: _list_s3_flow_log_objects(
                    b, p, since_epoch, until_epoch, profile, region
                ),
                source="s3",
                unit=f"s3://{bucket}/{prefix} (list)",
                iam_hint=iam,
            )
            get_profile, get_region = profile, region
        if not objects_outcome.ok:
            continue  # couldn't list this source (skippable/transient-exhausted) — move on
        objects = _filter_objects_in_scope(
            objects_outcome.value, allowed_fl_ids, network_account_id
        )
        total = len(objects)
        largest = max((size for _, size in objects), default=0)
        order = _size_descending_keys(objects)
        src_read = 0  # objects successfully read from THIS source
        src_parsed = 0  # records parsed from THIS source
        zero_streak = 0  # consecutive successfully-read objects (in size order) that parsed nothing
        for i, key in enumerate(order):
            outcome = _run_unit(
                lambda b=bucket, k=key, gp=get_profile, gr=get_region: _read_s3_object_records(
                    b, k, gp, gr
                ),
                source="s3",
                unit=f"s3://{bucket}/{key}",
                iam_hint=iam,
            )
            tracker.record(outcome)
            if not outcome.ok:
                continue  # download failure (skippable/transient-exhausted) — streak untouched
            objects_read += 1
            src_read += 1
            parsed = len(outcome.value)
            records.extend(outcome.value)
            src_parsed += parsed
            _account_object_to_vpc(per_vpc, fl_to_vpc, key, parsed)
            zero_streak = zero_streak + 1 if parsed == 0 else 0
            # Read largest first, so once a run of NODATA builds up the remaining objects are all
            # smaller and almost certainly NODATA too — stop reading this source. A cold source (no
            # records yet) needs a conservative probe (_FLOW_LOG_PROBE_OBJECTS) before we give up; a
            # source that has already yielded records only needs a short streak (TAIL_STREAK)
            # to trim its dead tail. One all-NODATA VPC never aborts a run with data elsewhere; the
            # run fails hard (below) only if EVERY source is empty.
            threshold = _FLOW_LOG_TAIL_STREAK if src_parsed else _FLOW_LOG_PROBE_OBJECTS
            if zero_streak >= threshold:
                remaining = len(order) - (i + 1)
                if remaining:
                    _warn_nodata_skip(
                        bucket,
                        prefix,
                        read=src_read,
                        total=total,
                        largest=largest,
                        parsed=src_parsed,
                        remaining=remaining,
                    )
                break
        if src_parsed == 0:
            empty_sources.append(f"s3://{bucket}/{prefix}")

    if not records and objects_read >= _FLOW_LOG_PROBE_OBJECTS:
        # Every S3 source we probed came back empty (all-NODATA / unrecognised) — fail loudly rather
        # than hand back an empty graph, preserving the original single-VPC fast-fail intent.
        where = ", ".join(empty_sources) or "the configured S3 destination(s)"
        raise FlowLogFetchError(
            f"aborting flow-log fetch: probed {objects_read} S3 object(s) across {where} but "
            f"parsed 0 usable flow records — the flow logs appear to be all NODATA/SKIPDATA or an "
            f"unrecognised format. Check the flow log's TrafficType/format, or narrow the window."
        )
    return records, objects_read, tracker.failed


def _account_object_to_vpc(
    per_vpc: dict[str, dict[str, int]] | None,
    fl_to_vpc: dict[str, str] | None,
    key: str,
    parsed: int,
) -> None:
    """Attribute one read S3 object (+ its parsed records) to the VPC of the flow-log id in its key,
    for the per-VPC completeness accounting (§5.7 D). No-op when accounting isn't requested."""
    if per_vpc is None or not fl_to_vpc:
        return
    fid = _flow_log_id_from_key(key)
    vpc = fl_to_vpc.get(fid) if fid else None
    if vpc is None:
        return
    acc = per_vpc.setdefault(vpc, {"objects": 0, "records": 0})
    acc["objects"] += 1
    acc["records"] += parsed


def _list_s3_flow_log_objects(
    bucket: str,
    prefix: str,
    since_epoch: float,
    until_epoch: float | None,
    profile: str | None,
    region: str | None,
) -> list[tuple[str, int]]:
    """The ``.gz`` objects under ``bucket``/``prefix`` last modified within the window, as
    ``(key, size_bytes)`` pairs sorted by key.

    ``since_epoch`` lower-bounds ``LastModified``; ``until_epoch`` (from ``--flow-log-end``) upper-
    bounds it when set. Objects with an unknown timestamp are kept. ``Size`` comes back in the same
    ``list-objects-v2`` response (no extra call) and feeds the size-aware fast-fail probe; a missing
    or non-numeric size is treated as ``0``."""
    args = ["s3api", "list-objects-v2", f"--bucket={bucket}"]
    if prefix:
        args.append(f"--prefix={prefix}")
    data = runner.run_aws(args, profile=profile, region=region)
    objects: list[tuple[str, int]] = []
    for obj in data.get("Contents", []):
        key = obj.get("Key")
        if not key or not key.endswith(".gz"):
            continue
        modified = _epoch_from_iso(obj.get("LastModified"))
        if modified is not None and modified < since_epoch:
            continue  # older than the window start — skip (keep it if the timestamp is unknown)
        if modified is not None and until_epoch is not None and modified > until_epoch:
            continue  # newer than the window end (--flow-log-end)
        raw_size = obj.get("Size")
        size = int(raw_size) if isinstance(raw_size, (int, float)) else 0
        objects.append((key, size))
    return sorted(objects)


def _size_descending_keys(objects: list[tuple[str, int]]) -> list[str]:
    """A source's object keys ordered **largest first** (ties broken by key for determinism).

    NODATA objects compress tiny, so descending size front-loads the data-bearing objects and pushes
    NODATA to the tail — letting :func:`_read_s3_records` consume real data first and then skip a
    trailing run of NODATA. Output is unaffected by download order (records are sorted downstream);
    this only changes the order in which objects are read."""
    return [k for k, _ in sorted(objects, key=lambda o: (-o[1], o[0]))]


def _warn_nodata_skip(
    bucket: str, prefix: str, *, read: int, total: int, largest: int, parsed: int, remaining: int
) -> None:
    """Warn (stderr) that a source's remaining smaller objects are skipped as a NODATA tail."""
    src = f"s3://{bucket}/{prefix}"
    if parsed == 0:
        reason = (
            f"probed the {read} largest of {total} object(s) (largest ~{largest:,} B) "
            f"but parsed 0 usable records"
        )
    else:
        reason = (
            f"parsed records from the largest objects, then hit {_FLOW_LOG_TAIL_STREAK} "
            f"consecutive NODATA object(s) in the small-size tail"
        )
    print(
        f"cloudbreachgraph: warning: flow-log s3 source {src}: {reason} — skipping the "
        f"remaining {remaining} smaller object(s) (all NODATA/SKIPDATA or an unrecognised format).",
        file=sys.stderr,
    )


def _read_s3_object_records(
    bucket: str, key: str, profile: str | None, region: str | None
) -> list[dict]:
    """Download one gzipped flow-log object and parse its records. The object's **first line is the
    field-name header** (VPC flow-log S3 files always carry one), so the field index is read from it
    (falling back to the default layout if it isn't a header). A corrupt/unreadable object is
    skipped, never fatal."""
    lines = _download_gz_lines(bucket, key, profile, region)
    if not lines:
        return []
    header_idx = _field_index_from_format(lines[0])
    if header_idx is not None:
        field_idx, data_lines = header_idx, lines[1:]
    else:
        field_idx, data_lines = dict(_FLOW_FIELD_IDX), lines
    source = f"s3://{bucket}/{key}"
    out: list[dict] = []
    for line in data_lines:
        rec = _parse_flow_log_message(line, source, field_idx)
        if rec is not None:
            out.append(rec)
    return out


def _download_gz_lines(bucket: str, key: str, profile: str | None, region: str | None) -> list[str]:
    """``s3api get-object`` the key to a temp file, gunzip it, return its text lines.

    A failed ``get-object`` raises :class:`~cloudbreachgraph.aws.runner.AwsCliError` (classified and
    handled by :func:`_run_unit`); a corrupt/undecodable body raises :class:`_SkippableUnitError`
    (warned + skipped + counted toward the failure-rate safeguard) rather than silently vanish."""
    fd, dest = tempfile.mkstemp(suffix=".gz")
    os.close(fd)
    try:
        runner.download_object(
            ["s3api", "get-object", f"--bucket={bucket}", f"--key={key}"],
            dest,
            profile=profile,
            region=region,
        )
        with gzip.open(dest, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise _SkippableUnitError(f"corrupt/unreadable gzip object: {exc}") from exc
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass


def _epoch_from_iso(value: str | None) -> float | None:
    """Epoch seconds from an ISO-8601 timestamp (S3 ``LastModified``), or ``None``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Registry: LogDestinationType -> the reader that pulls its records. Adding a new destination type
# (e.g. kinesis-data-firehose) is one entry here + its reader; until then such a type raises
# FlowLogDestinationError, so the tool always pulls from the right source or fails loudly (§5.7).
FLOW_LOG_READERS: dict[
    str,
    Callable[
        [list[dict], str | None, str | None, float, float | None], tuple[list[dict], int, int]
    ],
] = {
    "cloud-watch-logs": _read_cloudwatch_records,
    "s3": _read_s3_records,
}


def _report_flow_log_records(
    flow_logs: list[dict],
    dest_counts: dict[str, int],
    fetched_by_type: dict[str, int],
    skipped_by_type: dict[str, int],
    parsed: int,
) -> None:
    """Emit a concise stderr diagnostic for the flow-log record fetch (``docs/02_architecture.md
    §5.7``) so an empty result points at its cause rather than failing silently. ``fetched`` counts
    are CloudWatch **events** and S3 **objects**; skipped counts are best-effort **units** that were
    warned + skipped — enough to localise where the pipeline drops."""
    dest = ", ".join(f"{n} {d}" for d, n in sorted(dest_counts.items())) or "none"
    unit = {"cloud-watch-logs": "event(s)", "s3": "object(s)"}
    fetched = (
        ", ".join(
            f"{fetched_by_type[d]} {unit.get(d, 'item(s)')} from {d}"
            for d in sorted(fetched_by_type)
        )
        or "nothing"
    )
    total_skipped = sum(skipped_by_type.values())
    skipped_note = ""
    if total_skipped:
        by_src = ", ".join(
            f"{skipped_by_type[d]} from {d}" for d in sorted(skipped_by_type) if skipped_by_type[d]
        )
        skipped_note = f" skipped {total_skipped} unit(s) [{by_src}];"
    print(
        f"cloudbreachgraph: flow logs: {len(flow_logs)} config(s) [{dest}]; "
        f"fetched {fetched};{skipped_note} parsed {parsed} flow record(s).",
        file=sys.stderr,
    )
    if sum(fetched_by_type.values()) and not parsed:
        print(
            "cloudbreachgraph: note: fetched log data but parsed no flow records — the source may "
            "hold a non-flow-log or unrecognised format.",
            file=sys.stderr,
        )


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
    # (ec2:DescribeFlowLogs, cloudtrail:LookupEvents, logs:FilterLogEvents in the *network* account;
    # s3:ListBucket + s3:GetObject in the *archive* account). NOTE: unlike ``network``, this role is
    # **not** run by the generic driver loop — :func:`collect_all` special-cases it via
    # :func:`_collect_flow_logs_role`, because the four collectors span **two** accounts (config +
    # CloudTrail + CloudWatch in the network account; S3 object I/O in the archive account). The
    # registry entry documents the collectors + result keys; the account split lives in the driver.
    "flow_logs": [
        collect_flow_logs,  # aws ec2        describe-flow-logs   -> .FlowLogs[]   (network acct)
        collect_ip_allocation_events,  # aws cloudtrail lookup-events -> allocations (network acct)
        collect_historical_enis,  # aws cloudtrail lookup-events (x4)   -> historical (network acct)
        collect_flow_log_records,  # readers: CloudWatch (network) / S3 (archive) -> flow records
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
    "flow_logs": ["flow_logs", "ip_allocations", "historical_enis", "flow_log_records"],
}


# --------------------------------------------------------------------------- #
# flow_logs role driver (§5.7 cross-account split) — config/CloudTrail/CloudWatch in the network
# account, S3 object I/O in the archive account, with the coverage/precision/completeness checks.
# --------------------------------------------------------------------------- #
def _collect_flow_logs_role(resolved, bundle: dict, archive_access: ArchiveAccess | None) -> None:
    """Collect the ``flow_logs`` role across the network + archive accounts and gate on coverage.

    Runs ``describe-flow-logs`` **once** (network account) and reuses that config for both the graph
    nodes and the record fetch. The coverage check runs **before** any S3 download so a
    config-in-wrong-account mistake fails fast; the fetch is scoped to the in-scope ``fl-…`` objects
    and reconciled per VPC for completeness. Writes the four ``flow_logs`` result keys plus
    ``meta.flow_log_coverage`` into ``bundle`` and records both accounts under ``meta.accounts``."""
    net = resolved.roles.get("network") or resolved.roles["flow_logs"]
    fl_role_acct = resolved.roles["flow_logs"]

    # Config, IP-allocation history and historical-ENI reconstruction are EC2/CloudTrail resources
    # of the VPC account — run them under the network account (§5.7 A).
    flow_logs = collect_flow_logs(net.profile, net.region)
    bundle["flow_logs"] = flow_logs
    bundle["ip_allocations"] = collect_ip_allocation_events(net.profile, net.region)
    bundle["historical_enis"] = collect_historical_enis(net.profile, net.region)

    # Validate destination types up front (raises FlowLogDestinationError for an unreadable type
    # like kinesis-data-firehose) — a distinct fail-loud that precedes the coverage check.
    _group_flow_logs_by_dest(flow_logs)

    # Coverage reconciliation BEFORE any S3 I/O (§5.7 C): a wrong-account config fails here, not
    # after thousands of unrelated gets. Uncovered VPCs are a soft warning (§9), not fatal.
    coverage = check_vpc_coverage(
        flow_logs,
        bundle.get("vpcs", []),
        bundle.get("subnets", []),
        bundle.get("network_interfaces", []),
    )
    _warn_uncovered_vpcs(coverage)

    # Records: CloudWatch under the network account, S3 under the resolved archive account. When the
    # CLI didn't supply an ArchiveAccess (--from-cache / simple callers), use the flow_logs role's
    # own account directly as an explicit binding — so single-account runs are unchanged.
    archive = archive_access or ArchiveAccess(primary=net, explicit=fl_role_acct)
    records, per_vpc, resolved_archive = fetch_flow_log_records(
        flow_logs,
        net,
        archive,
        coverage=coverage,
        network_account_id=net.account_id,
    )
    bundle["flow_log_records"] = records

    _check_flow_log_completeness(coverage, per_vpc)
    bundle["meta"]["flow_log_coverage"] = coverage.meta(per_vpc)
    bundle["meta"]["accounts"]["network"] = net.account_id
    # Provenance: the account the S3 objects were actually read under (the resolved archive, which
    # may differ from the bound flow_logs account after an auto-resolution fallback).
    archive_acct = resolved_archive or fl_role_acct
    bundle["meta"]["accounts"]["flow_logs"] = archive_acct.account_id


def _check_flow_log_completeness(
    coverage: FlowLogCoverage, per_vpc: dict[str, dict[str, int]]
) -> None:
    """Reconcile the fetch against coverage (``§5.7`` D completeness).

    A covered VPC (one with an in-scope flow log) that yielded **zero** in-window objects/events is
    suspicious (wrong prefix/region/window, or the archive account resolved wrong) — warn per VPC.
    If **every** covered VPC came back empty, that's the "downloading logs unrelated to my VPCs"
    caught on the fetch side: raise :class:`FlowLogCoverageError`. A covered VPC whose objects exist
    but are all NODATA is *not* empty here (it has objects) — that case is the reader's own NODATA
    handling."""
    covered = coverage.covered_vpcs
    if not covered:
        return
    empty = [v for v in covered if per_vpc.get(v, {}).get("objects", 0) == 0]
    if len(empty) == len(covered):
        raise FlowLogCoverageError(
            f"aborting flow-log fetch: all {len(covered)} covered VPC(s) "
            f"({', '.join(sorted(covered))}) yielded zero in-window flow-log objects. The flow-log "
            f"configuration references these VPCs, but no delivered log data was found for them — "
            f"check the record window (--flow-log-days/--flow-log-start), the destination "
            f"bucket/region, or whether the archive account resolved to the right bucket."
        )
    for vpc in sorted(empty):
        print(
            f"cloudbreachgraph: warning: VPC {vpc} has a flow log configured but no in-window "
            f"flow-log data was fetched for it (empty window, wrong prefix/region, or an archive "
            f"account without that VPC's objects).",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #
# Driver loop (§11.7)
# --------------------------------------------------------------------------- #
def collect_all(
    resolved,
    *,
    roles: tuple[str, ...] | list[str] = ("network",),
    cache_dir: str | None = None,
    archive_access: ArchiveAccess | None = None,
) -> dict:
    """Run every collector for each requested role and bundle the results (§11.7).

    ``resolved`` is a :class:`cloudbreachgraph.config.ResolvedTarget`; each role is run
    with its own resolved ``profile``/``region``, so a multi-account target can pull
    different roles from different accounts in one call. Per-role account provenance is
    recorded under ``meta.accounts``.

    The ``flow_logs`` role is special-cased (``§5.7`` cross-account split): its config, CloudTrail
    history and CloudWatch reads run in the **network** account, while only the S3 object I/O runs
    the **archive** account resolved from ``archive_access`` (explicit binding, else primary→
    AccessDenied fallback across the configured candidates). ``archive_access`` is built by the CLI
    where both accounts are in hand; when omitted (``--from-cache`` and simple callers) the S3 read
    uses the ``flow_logs`` role's own resolved account directly, so single-account runs are
    unchanged.

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
        if role == "flow_logs":
            _collect_flow_logs_role(resolved, bundle, archive_access)
            continue
        acct = resolved.roles[role]
        collectors = ROLE_COLLECTORS[role]
        keys = ROLE_RESULT_KEYS[role]
        for collector, key in zip(collectors, keys, strict=True):
            bundle[key] = collector(acct.profile, acct.region)
        bundle["meta"]["accounts"][role] = acct.account_id

    # Record the flow-log record window so the graph carries it (§5.7): either the configured
    # ``--flow-log-days`` count, or an explicit ``--flow-log-start``/``--flow-log-end`` range (ISO,
    # deterministic — user-supplied, not wall-clock). Plus the (always-90) CloudTrail window.
    # Only when the flow_logs role ran.
    if "flow_logs" in roles:
        if _flow_log_start_epoch is not None:
            bundle["meta"]["flow_log_start"] = _iso_utc(_flow_log_start_epoch)
            bundle["meta"]["flow_log_end"] = (
                _iso_utc(_flow_log_end_epoch) if _flow_log_end_epoch is not None else None
            )
        else:
            bundle["meta"]["flow_log_window_days"] = _flow_log_window_days
        bundle["meta"]["cloudtrail_window_days"] = _cloudtrail_lookback_days()

    return bundle


def _iso_utc(epoch: float) -> str:
    """Format epoch seconds as a UTC ISO-8601 string (``2026-05-01T00:00:00Z``)."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
