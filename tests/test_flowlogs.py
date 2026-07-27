"""Tests for the flow-log analysis (``mapping/flowlogs.py`` via ``build_graph``, §5.7).

Covers the §5.7 rules: IP-allocation history on ENI nodes, flow-log configuration as a VPC
attribute, observed-connection ``connects_to`` edges (ENI->ENI when the peer is another collected
ENI that already held the IP at record time, else a ``flow_peer`` node), and the allocation-time
clamps. Fully offline.
"""

from __future__ import annotations

import pytest
from conftest import load_fixture

from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget
from cloudbreachgraph.mapping.builder import build_graph

_COMMAND_FIXTURES = {
    ("ec2", "describe-network-interfaces"): "ec2_describe-network-interfaces.json",
    ("ec2", "describe-instances"): "ec2_describe-instances.json",
    ("elbv2", "describe-load-balancers"): "elbv2_describe-load-balancers.json",
    ("elb", "describe-load-balancers"): "elb_describe-load-balancers.json",
    ("ec2", "describe-subnets"): "ec2_describe-subnets.json",
    ("ec2", "describe-vpcs"): "ec2_describe-vpcs.json",
    ("ec2", "describe-security-groups"): "ec2_describe-security-groups.json",
    ("ec2", "describe-route-tables"): "ec2_describe-route-tables.json",
    ("ec2", "describe-nat-gateways"): "ec2_describe-nat-gateways.json",
    ("ec2", "describe-vpc-endpoints"): "ec2_describe-vpc-endpoints.json",
    ("ec2", "describe-flow-logs"): "ec2_describe-flow-logs.json",
    ("cloudtrail", "lookup-events"): "cloudtrail_lookup-events.json",
    ("logs", "filter-log-events"): "logs_filter-log-events.json",
    ("s3api", "list-objects-v2"): "s3api_list-objects-v2.json",
}


@pytest.fixture
def flow_bundle(monkeypatch):
    """A ``collect_all`` bundle with both the network and flow_logs roles, served from fixtures."""

    def _run(args, *, profile=None, region=None, cache_dir=None):
        return load_fixture(_COMMAND_FIXTURES[tuple(args[:2])])

    monkeypatch.setattr(runner, "run_aws", _run)
    resolved = ResolvedTarget(
        target="prod",
        roles={
            "network": ResolvedAccount(
                profile="prod-audit", account_id="111111111111", region="us-east-1"
            ),
            "flow_logs": ResolvedAccount(
                profile="prod-audit", account_id="111111111111", region="us-east-1"
            ),
        },
    )
    return collectors.collect_all(resolved, roles=("network", "flow_logs"))


def _edges(graph, rel):
    return [e for e in graph.edges if e.relationship == rel]


# --------------------------------------------------------------------------- #
# IP history
# --------------------------------------------------------------------------- #
def test_ip_history_on_eni_nodes(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    # ip_history: {ip: {start, end}} — start from CloudTrail, end None while the IP is current.
    inst = graph.get_node("eni-00instance0000001")
    assert inst.attributes["ip_history"] == {
        "10.0.1.10": {"start": "2026-06-01T00:00:00+00:00", "end": None}
    }
    # Every ENI gets the field, even one with no CloudTrail event: its current IP, start unknown.
    assert graph.get_node("eni-00nlb00000000003").attributes["ip_history"] == {
        "10.0.2.30": {"start": None, "end": None}
    }


def test_ip_history_absent_without_flag(flow_bundle):
    graph = build_graph(flow_bundle)  # map_flow_logs defaults off
    assert "ip_history" not in graph.get_node("eni-00instance0000001").attributes


def _eni_dict(eni_id, ips):
    return {
        "NetworkInterfaceId": eni_id,
        "SubnetId": "subnet-1",
        "VpcId": "vpc-1",
        "InterfaceType": "interface",
        "Description": "",
        "Status": "in-use",
        "AvailabilityZone": "us-east-1a",
        "RequesterId": None,
        "RequesterManaged": False,
        "Attachment": {"InstanceId": None},
        "Association": {"PublicIp": None},
        "PrivateIpAddresses": [{"PrivateIpAddress": ip} for ip in ips],
        "Groups": [],
    }


def _alloc(ip, at):
    return {"NetworkInterfaceId": "eni-h", "PrivateIpAddress": ip, "AllocatedAt": at}


def test_ip_history_marks_a_superseded_ip_with_start_and_end():
    # An ENI whose IP changed: 10.0.0.5 (2026-05-01) was replaced by current 10.0.0.9 (2026-06-01).
    bundle = {
        "meta": {"target": None, "region": "us-east-1", "accounts": {}},
        "network_interfaces": [_eni_dict("eni-h", ["10.0.0.9"])],
        "ec2_instances": [],
        "load_balancers_v2": [],
        "load_balancers_classic": [],
        "subnets": [],
        "vpcs": [],
        "flow_logs": [],
        "ip_allocations": [
            _alloc("10.0.0.5", "2026-05-01T00:00:00+00:00"),
            _alloc("10.0.0.9", "2026-06-01T00:00:00+00:00"),
        ],
        "flow_log_records": [],
    }
    history = build_graph(bundle, map_flow_logs=True).get_node("eni-h").attributes["ip_history"]
    assert history == {
        # The released IP: start = its allocation, end = when the successor was allocated.
        "10.0.0.5": {"start": "2026-05-01T00:00:00+00:00", "end": "2026-06-01T00:00:00+00:00"},
        # The current IP: still held, so end is open.
        "10.0.0.9": {"start": "2026-06-01T00:00:00+00:00", "end": None},
    }


# --------------------------------------------------------------------------- #
# Flow-log configuration — a VPC attribute, not separate nodes
# --------------------------------------------------------------------------- #
def test_flow_log_config_is_a_vpc_attribute_not_nodes(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    # No standalone flow-log / destination nodes or plumbing edges any more.
    assert not any(n.type in ("flow_log", "log_group", "log_bucket") for n in graph.nodes)
    assert not any(e.relationship in ("logs_to", "delivers_to") for e in graph.edges)

    # The config lives on the VPC that owns the logged resource. Both flow logs (one VPC-scoped,
    # one subnet-scoped) resolve up to the same VPC and its destination is recorded there.
    vpc = graph.get_node("vpc-0aaaaaaaaaaaaaaaa")
    flow_logs = vpc.attributes["flow_logs"]
    assert [fl["flow_log_id"] for fl in flow_logs] == [
        "fl-0abc00000000001",
        "fl-0abc00000000002",
    ]
    by_id = {fl["flow_log_id"]: fl for fl in flow_logs}
    assert by_id["fl-0abc00000000001"]["destination"] == "/vpc/flowlogs/prod"
    assert by_id["fl-0abc00000000001"]["destination_type"] == "cloud-watch-logs"
    # A subnet-scoped flow log still attaches to its VPC (resolved via the subnet).
    assert by_id["fl-0abc00000000002"]["resource_id"] == "subnet-022222222222222"
    assert by_id["fl-0abc00000000002"]["destination"].startswith("arn:aws:s3:::")


# --------------------------------------------------------------------------- #
# Observed connections
# --------------------------------------------------------------------------- #
def test_eni_to_eni_edge_when_peer_ip_is_another_eni(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    connects = {(e.source, e.target): e for e in _edges(graph, "connects_to")}
    # instance -> nlb (10.0.1.10 -> 10.0.2.30, dstport 443): a direct ENI->ENI edge.
    edge = connects[("eni-00instance0000001", "eni-00nlb00000000003")]
    assert edge.attributes["ports"] == "tcp/443"
    assert edge.attributes["via"] == "flow_log"
    # The reverse direction (nlb -> instance) is captured from the nlb's own flow record.
    assert ("eni-00nlb00000000003", "eni-00instance0000001") in connects


def test_external_peer_becomes_flow_peer_node(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    peer = graph.get_node("flow-peer:203.0.113.5")
    assert peer is not None and peer.type == "flow_peer" and peer.label == "203.0.113.5"
    # It connected *to* the instance ENI on tcp/22.
    edge = next(e for e in _edges(graph, "connects_to") if e.source == "flow-peer:203.0.113.5")
    assert edge.target == "eni-00instance0000001"
    assert edge.attributes["ports"] == "tcp/22"


def test_traffic_before_home_ip_allocation_is_dropped(flow_bundle):
    # The 198.51.100.9 record predates the instance ENI's 2026-06-01 IP allocation -> excluded.
    graph = build_graph(flow_bundle, map_flow_logs=True)
    assert graph.get_node("flow-peer:198.51.100.9") is None
    assert not any(e.source == "flow-peer:198.51.100.9" for e in _edges(graph, "connects_to"))


def test_eni_to_eni_edge_requires_peer_held_the_ip_at_record_time(flow_bundle):
    # A valid instance->alb flow (2026-06-15, after the alb's 2026-05-20 IP allocation) links them.
    graph = build_graph(flow_bundle, map_flow_logs=True)
    connects = {(e.source, e.target) for e in _edges(graph, "connects_to")}
    assert ("eni-00instance0000001", "eni-00alb00000000002") in connects

    # But the nlb->10.0.1.20 flow at 2026-05-01 predates when the alb ENI got 10.0.1.20
    # (2026-05-20): the IP was a different interface's then -> historic reuse, dropped.
    assert ("eni-00nlb00000000003", "eni-00alb00000000002") not in connects
    # ...and no flow_peer is invented for an IP that currently belongs to an ENI.
    assert graph.get_node("flow-peer:10.0.1.20") is None


# --------------------------------------------------------------------------- #
# Historical ENIs + time-aware IP→ENI resolution (§5.7 Part 3)
# --------------------------------------------------------------------------- #
def _epoch(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso).timestamp())


def _hist(eni_id, ips, *, created=None, deleted=None, subnet="subnet-1", vpc="vpc-1", **kw):
    return {
        "NetworkInterfaceId": eni_id,
        "PrivateIpAddresses": ips,
        "SubnetId": subnet,
        "VpcId": vpc,
        "Groups": kw.get("groups", []),
        "Description": kw.get("description"),
        "InterfaceType": "interface",
        "RequesterId": None,
        "InstanceId": kw.get("instance_id"),
        "AsgName": kw.get("asg_name"),
        "Name": kw.get("name"),
        "CreatedAt": created,
        "DeletedAt": deleted,
    }


def _record(home, src, dst, *, start, dstport=443, proto="6"):
    return {
        "InterfaceId": home,
        "SrcAddr": src,
        "DstAddr": dst,
        "SrcPort": 50000,
        "DstPort": dstport,
        "Protocol": proto,
        "Action": "ACCEPT",
        "Start": start,
        "LogGroup": "/g",
    }


def _vpc(vpc_id, cidr):
    return {"VpcId": vpc_id, "CidrBlock": cidr, "IsDefault": False, "Tags": []}


def _flow_bundle_with(*, network=(), historical=(), records=(), allocations=(), vpcs=()):
    return {
        "meta": {"target": None, "region": "us-east-1", "accounts": {}},
        "network_interfaces": list(network),
        "ec2_instances": [],
        "load_balancers_v2": [],
        "load_balancers_classic": [],
        "subnets": [],
        "vpcs": list(vpcs),
        "flow_logs": [],
        "ip_allocations": list(allocations),
        "historical_enis": list(historical),
        "flow_log_records": list(records),
    }


def test_time_aware_resolution_attributes_reused_ip_to_the_right_eni():
    # Two historical ENIs reused 10.0.0.50 at different times; a flow at each time attributes to the
    # ENI whose lifetime covers it — an ENI↔ENI edge to the right one, never a flow_peer.
    home = _eni_dict("eni-home00000000001", ["10.0.0.1"])
    historical = [
        _hist(
            "eni-old00000000001",
            ["10.0.0.50"],
            created="2026-02-01T00:00:00+00:00",
            deleted="2026-03-01T00:00:00+00:00",
        ),
        _hist(
            "eni-new00000000001",
            ["10.0.0.50"],
            created="2026-04-01T00:00:00+00:00",
        ),
    ]
    records = [
        _record(
            "eni-home00000000001",
            "10.0.0.1",
            "10.0.0.50",
            start=_epoch("2026-02-15T00:00:00+00:00"),
        ),
        _record(
            "eni-home00000000001",
            "10.0.0.1",
            "10.0.0.50",
            start=_epoch("2026-04-15T00:00:00+00:00"),
        ),
    ]
    bundle = _flow_bundle_with(network=[home], historical=historical, records=records)
    graph = build_graph(bundle, map_flow_logs=True)
    connects = {(e.source, e.target) for e in _edges(graph, "connects_to")}
    assert ("eni-home00000000001", "eni-old00000000001") in connects  # the Feb flow → the old ENI
    assert ("eni-home00000000001", "eni-new00000000001") in connects  # the Apr flow → the new ENI
    assert graph.get_node("flow-peer:10.0.0.50") is None  # never an external peer for an ENI IP


def test_historical_eni_becomes_a_flagged_terminated_node():
    home = _eni_dict("eni-home00000000001", ["10.0.0.1"])
    historical = [
        _hist(
            "eni-term00000000001",
            ["10.0.0.50"],
            created="2026-02-01T00:00:00+00:00",
            deleted="2026-03-01T00:00:00+00:00",
        )
    ]
    records = [
        _record(
            "eni-home00000000001",
            "10.0.0.1",
            "10.0.0.50",
            start=_epoch("2026-02-15T00:00:00+00:00"),
        )
    ]
    graph = build_graph(
        _flow_bundle_with(network=[home], historical=historical, records=records),
        map_flow_logs=True,
    )
    node = graph.get_node("eni-term00000000001")
    assert node is not None and node.type == "eni"
    assert node.attributes["historical"] is True
    assert node.attributes["status"] == "terminated"
    assert node.attributes["terminated_at"] == "2026-03-01T00:00:00+00:00"
    # It is placed in its subnet (so it clusters in the VPC).
    assert any(
        e.source == "eni-term00000000001" and e.relationship == "in_subnet" for e in graph.edges
    )


def test_flow_on_a_now_terminated_home_eni_is_analysed_not_dropped():
    # A flow captured on an ENI since terminated is still analysed (its peer still resolves).
    historical = [
        _hist(
            "eni-gone00000000001",
            ["10.0.5.5"],
            created="2026-02-01T00:00:00+00:00",
            deleted="2026-05-01T00:00:00+00:00",
        )
    ]
    records = [
        _record(
            "eni-gone00000000001",
            "10.0.5.5",
            "203.0.113.9",
            start=_epoch("2026-03-01T00:00:00+00:00"),
            dstport=22,
        )
    ]
    graph = build_graph(
        _flow_bundle_with(historical=historical, records=records), map_flow_logs=True
    )
    edge = next(
        (e for e in _edges(graph, "connects_to") if e.source == "eni-gone00000000001"), None
    )
    assert edge is not None  # not dropped despite the home ENI being terminated
    assert edge.target == "flow-peer:203.0.113.9"
    assert edge.attributes["ports"] == "tcp/22"


# --------------------------------------------------------------------------- #
# Unrecognised ENIs — surface (never drop) an unknown flow-record home id (§5.7)
# --------------------------------------------------------------------------- #
def test_unrecognised_eni_becomes_flagged_node_with_inferred_ip():
    # A flow-record home id in neither the current nor historical inventory: instead of being
    # dropped it becomes a flagged `unrecognised` ENI, its own IP inferred from a known VPC CIDR.
    records = [
        _record(
            "eni-unknown00000001",
            "10.0.7.7",
            "203.0.113.10",
            start=_epoch("2026-06-15T00:00:00+00:00"),
        ),
        _record(
            "eni-unknown00000001",
            "203.0.113.11",
            "10.0.7.7",
            start=_epoch("2026-06-16T00:00:00+00:00"),
            dstport=22,
        ),
    ]
    bundle = _flow_bundle_with(records=records, vpcs=[_vpc("vpc-1", "10.0.0.0/16")])
    graph = build_graph(bundle, map_flow_logs=True)

    node = graph.get_node("eni-unknown00000001")
    assert node is not None and node.type == "eni"
    assert node.attributes["unrecognised"] is True
    assert node.attributes["origin"] == "flow_log"
    assert node.attributes["needs_review"] is True
    # The guess is NEVER in private_ips (confirmed only) — it is flagged under inferred_private_ips.
    assert node.attributes["private_ips"] == []
    assert node.attributes["inferred_private_ips"] == [
        {"ip": "10.0.7.7", "method": "vpc_cidr", "confidence": "high"}
    ]
    # Its flows are still mapped: the external peers become flow_peer nodes.
    connects = {(e.source, e.target) for e in _edges(graph, "connects_to")}
    assert ("eni-unknown00000001", "flow-peer:203.0.113.10") in connects
    assert ("flow-peer:203.0.113.11", "eni-unknown00000001") in connects


def test_unrecognised_eni_inferred_ip_forms_eni_to_eni_edge_with_a_peer():
    # A known ENI whose flow peer matches the unrecognised ENI's inferred IP links the two directly
    # (ENI↔ENI); a peer matching no ENI stays an external flow_peer.
    known = _eni_dict("eni-known0000000001", ["10.0.0.1"])
    records = [
        # establish the unrecognised ENI's own (recurring, VPC-internal) IP 10.0.7.7
        _record(
            "eni-unknown00000001",
            "10.0.7.7",
            "203.0.113.10",
            start=_epoch("2026-06-15T00:00:00+00:00"),
        ),
        _record(
            "eni-unknown00000001",
            "10.0.7.7",
            "203.0.113.11",
            start=_epoch("2026-06-16T00:00:00+00:00"),
        ),
        # a known ENI's flow whose peer IS the unrecognised ENI's inferred IP
        _record(
            "eni-known0000000001", "10.0.0.1", "10.0.7.7", start=_epoch("2026-06-17T00:00:00+00:00")
        ),
    ]
    bundle = _flow_bundle_with(
        network=[known], records=records, vpcs=[_vpc("vpc-1", "10.0.0.0/16")]
    )
    graph = build_graph(bundle, map_flow_logs=True)
    connects = {(e.source, e.target) for e in _edges(graph, "connects_to")}
    # ENI↔ENI: the known ENI connects to the unrecognised ENI (not to a flow_peer for 10.0.7.7).
    assert ("eni-known0000000001", "eni-unknown00000001") in connects
    assert graph.get_node("flow-peer:10.0.7.7") is None
    # A peer matching no ENI is still an external flow_peer.
    assert graph.get_node("flow-peer:203.0.113.10") is not None


def test_unrecognised_eni_falls_back_to_recurring_side_without_vpc_cidr():
    # With no known VPC CIDR to pin the internal side, the own IP is the most-recurring address and
    # the guess is flagged low confidence.
    records = [
        _record(
            "eni-unknown00000001",
            "10.9.9.9",
            "203.0.113.10",
            start=_epoch("2026-06-15T00:00:00+00:00"),
        ),
        _record(
            "eni-unknown00000001",
            "203.0.113.11",
            "10.9.9.9",
            start=_epoch("2026-06-16T00:00:00+00:00"),
        ),
    ]
    graph = build_graph(_flow_bundle_with(records=records), map_flow_logs=True)
    node = graph.get_node("eni-unknown00000001")
    assert node.attributes["inferred_private_ips"] == [
        {"ip": "10.9.9.9", "method": "recurring_side", "confidence": "low"}
    ]


def test_known_eni_home_is_not_flagged_unrecognised(flow_bundle):
    # Sanity: the fixture's records are all captured on *known* ENIs, so no unrecognised node
    # appears (the change is byte-inert on a graph whose homes are all resolvable).
    graph = build_graph(flow_bundle, map_flow_logs=True)
    assert not any(n.attributes.get("unrecognised") for n in graph.nodes)


def test_flow_logs_are_a_noop_without_the_flag(flow_bundle):
    graph = build_graph(flow_bundle)  # network-only view
    types = {n.type for n in graph.nodes}
    assert not types & {"flow_log", "log_group", "log_bucket", "flow_peer"}
    rels = {e.relationship for e in graph.edges}
    assert not rels & {"connects_to", "logs_to", "delivers_to"}
    # And the VPC carries no flow-log attribute in the network-only view.
    assert "flow_logs" not in graph.get_node("vpc-0aaaaaaaaaaaaaaaa").attributes


def test_flow_log_mapping_is_deterministic(flow_bundle):
    assert (
        build_graph(flow_bundle, map_flow_logs=True).to_dict()
        == build_graph(flow_bundle, map_flow_logs=True).to_dict()
    )
    graph = build_graph(flow_bundle, map_flow_logs=True)
    assert graph.meta["flow_log_window_days"] == collectors.FLOW_LOG_MAX_LOOKBACK_DAYS
