"""Tests for the flow-log analysis (``mapping/flowlogs.py`` via ``build_graph``, §5.7).

Covers the §5.7 rules: IP-allocation history on ENI nodes, flow-log configuration + destination
nodes/edges, observed-connection ``connects_to`` edges (ENI->ENI when the peer is another collected
ENI, else a ``flow_peer`` node), and the IP-allocation-time clamp. Fully offline.
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
def test_ip_allocation_history_on_eni_nodes(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    inst = graph.get_node("eni-00instance0000001")
    assert inst.attributes["ip_allocations"] == [
        {"ip": "10.0.1.10", "allocated_at": "2026-06-01T00:00:00+00:00"}
    ]
    # An ENI with no CloudTrail event carries no ip_allocations attribute.
    assert "ip_allocations" not in graph.get_node("eni-00nlb00000000003").attributes


def test_ip_history_absent_without_flag(flow_bundle):
    graph = build_graph(flow_bundle)  # map_flow_logs defaults off
    assert "ip_allocations" not in graph.get_node("eni-00instance0000001").attributes


# --------------------------------------------------------------------------- #
# Flow-log configuration + destinations
# --------------------------------------------------------------------------- #
def test_flow_log_config_and_destination_nodes(flow_bundle):
    graph = build_graph(flow_bundle, map_flow_logs=True)
    cw = graph.get_node("fl-0abc00000000001")
    assert cw is not None and cw.type == "flow_log"
    assert cw.attributes["destination_type"] == "cloud-watch-logs"

    log_group = graph.get_node("/vpc/flowlogs/prod")
    assert log_group is not None and log_group.type == "log_group"
    log_bucket = graph.get_node("arn:aws:s3:::prod-flow-logs-bucket/AWSLogs/")
    assert log_bucket is not None and log_bucket.type == "log_bucket"

    # resource -> flow_log (the VPC logs to fl-...001; the subnet to fl-...002).
    logs_to = {(e.source, e.target) for e in _edges(graph, "logs_to")}
    assert ("vpc-0aaaaaaaaaaaaaaaa", "fl-0abc00000000001") in logs_to
    assert ("subnet-022222222222222", "fl-0abc00000000002") in logs_to
    # flow_log -> destination.
    delivers = {(e.source, e.target) for e in _edges(graph, "delivers_to")}
    assert ("fl-0abc00000000001", "/vpc/flowlogs/prod") in delivers
    assert ("fl-0abc00000000002", "arn:aws:s3:::prod-flow-logs-bucket/AWSLogs/") in delivers


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


def test_traffic_before_ip_allocation_is_dropped(flow_bundle):
    # The 198.51.100.9 record predates the instance ENI's 2026-06-01 IP allocation -> excluded.
    graph = build_graph(flow_bundle, map_flow_logs=True)
    assert graph.get_node("flow-peer:198.51.100.9") is None
    assert not any(e.source == "flow-peer:198.51.100.9" for e in _edges(graph, "connects_to"))


def test_flow_logs_are_a_noop_without_the_flag(flow_bundle):
    graph = build_graph(flow_bundle)  # network-only view
    types = {n.type for n in graph.nodes}
    assert not types & {"flow_log", "log_group", "log_bucket", "flow_peer"}
    rels = {e.relationship for e in graph.edges}
    assert not rels & {"connects_to", "logs_to", "delivers_to"}


def test_flow_log_mapping_is_deterministic(flow_bundle):
    assert (
        build_graph(flow_bundle, map_flow_logs=True).to_dict()
        == build_graph(flow_bundle, map_flow_logs=True).to_dict()
    )
    graph = build_graph(flow_bundle, map_flow_logs=True)
    assert graph.meta["flow_log_window_days"] == collectors.FLOW_LOG_MAX_LOOKBACK_DAYS
