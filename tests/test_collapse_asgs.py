"""Tests for the Auto Scaling group collapse view transform (``mapping/collapse.py``, §5.7 Part 4).

Fully offline: graphs are hand-built and passed through :func:`collapse_autoscaling_groups`.
"""

from __future__ import annotations

from cloudbreachgraph.mapping.collapse import collapse_autoscaling_groups
from cloudbreachgraph.model.graph import Edge, Graph, Node


def _fleet_graph() -> Graph:
    """A graph with a two-instance ``web-asg`` (one instance/ENI historical), a single-instance
    ``db-asg``, plus non-member neighbours (a subnet layer, an external ENI, a flow_peer, an SG)."""
    g = Graph(meta={"region": "us-east-1"})
    # Structure.
    g.add_node(Node("vpc-1", "vpc", "vpc-1", {"cidr": "10.0.0.0/16"}))
    g.add_node(Node("subnet-1", "subnet", "subnet-1", {"vpc_id": "vpc-1"}))
    g.add_node(Node("subnet-2", "subnet", "subnet-2", {"vpc_id": "vpc-1"}))
    g.add_edge(Edge("subnet-1", "vpc-1", "in_vpc"))
    g.add_edge(Edge("subnet-2", "vpc-1", "in_vpc"))

    # web-asg: i-1/eni-1 current, i-2/eni-2 historical (terminated), spanning two subnets.
    g.add_node(Node("i-1", "ec2_instance", "i-1", {"vpc_id": "vpc-1", "asg_name": "web-asg"}))
    g.add_node(
        Node(
            "i-2",
            "ec2_instance",
            "i-2",
            {"vpc_id": "vpc-1", "asg_name": "web-asg", "historical": True},
        )
    )
    g.add_node(Node("eni-1", "eni", "eni-1", {"private_ips": ["10.0.0.1"]}))
    g.add_node(
        Node(
            "eni-2",
            "eni",
            "eni-2",
            {"private_ips": ["10.0.0.2"], "historical": True, "asg_name": "web-asg"},
        )
    )
    g.add_edge(Edge("eni-1", "i-1", "attached_to"))
    g.add_edge(Edge("eni-2", "i-2", "attached_to"))
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet"))
    g.add_edge(Edge("eni-2", "subnet-2", "in_subnet"))

    # db-asg: one instance + ENI.
    g.add_node(Node("i-3", "ec2_instance", "i-3", {"vpc_id": "vpc-1", "asg_name": "db-asg"}))
    g.add_node(Node("eni-3", "eni", "eni-3", {"private_ips": ["10.0.0.3"]}))
    g.add_edge(Edge("eni-3", "i-3", "attached_to"))
    g.add_edge(Edge("eni-3", "subnet-1", "in_subnet"))

    # Non-members: an external ENI, a flow_peer, and a security group.
    g.add_node(Node("eni-ext", "eni", "eni-ext", {"private_ips": ["10.0.0.9"]}))
    g.add_edge(Edge("eni-ext", "subnet-1", "in_subnet"))
    g.add_node(Node("flow-peer:203.0.113.5", "flow_peer", "203.0.113.5", {"ip": "203.0.113.5"}))
    g.add_node(Node("sg-1", "security_group", "sg-1", {"vpc_id": "vpc-1"}))

    # Edges that must re-point/merge/drop:
    g.add_edge(Edge("eni-1", "eni-ext", "connects_to", {"ports": "tcp/443", "via": "flow_log"}))
    g.add_edge(Edge("eni-2", "eni-ext", "connects_to", {"ports": "tcp/80", "via": "flow_log"}))
    g.add_edge(Edge("flow-peer:203.0.113.5", "eni-1", "connects_to", {"ports": "tcp/22"}))
    g.add_edge(Edge("eni-1", "eni-3", "connects_to", {"ports": "tcp/5432", "via": "flow_log"}))
    g.add_edge(Edge("eni-1", "sg-1", "secured_by"))
    return g


def _rels(graph, source=None, target=None):
    return {
        (e.source, e.target, e.relationship)
        for e in graph.edges
        if (source is None or e.source == source) and (target is None or e.target == target)
    }


def test_members_collapse_into_one_node_per_asg():
    out = collapse_autoscaling_groups(_fleet_graph())
    ids = {n.id for n in out.nodes}
    assert "asg:web-asg" in ids and "asg:db-asg" in ids
    # Every member instance AND ENI (current + historical) is gone.
    assert not ({"i-1", "i-2", "i-3", "eni-1", "eni-2", "eni-3"} & ids)
    # Non-members are untouched.
    assert {"eni-ext", "flow-peer:203.0.113.5", "sg-1", "subnet-1", "subnet-2", "vpc-1"} <= ids


def test_asg_node_carries_member_counts_and_subnets():
    out = collapse_autoscaling_groups(_fleet_graph())
    web = out.get_node("asg:web-asg")
    a = web.attributes
    assert a["instance_count"] == 2 and a["eni_count"] == 2
    assert a["historical_instance_count"] == 1 and a["current_instance_count"] == 1
    assert a["historical_eni_count"] == 1 and a["current_eni_count"] == 1
    assert a["subnets"] == ["subnet-1", "subnet-2"]  # the fleet spans two AZ subnets
    assert a["vpc_id"] == "vpc-1"
    assert a["private_ips"] == ["10.0.0.1", "10.0.0.2"]
    # It keeps an in_subnet edge to each distinct member subnet, so it nests in the VPC cluster.
    assert _rels(out, source="asg:web-asg") >= {
        ("asg:web-asg", "subnet-1", "in_subnet"),
        ("asg:web-asg", "subnet-2", "in_subnet"),
    }


def test_external_edges_repoint_and_merge_ports():
    out = collapse_autoscaling_groups(_fleet_graph())
    # eni-1 (tcp/443) and eni-2 (tcp/80) both talked to eni-ext -> one merged ASG->ext edge.
    merged = next(e for e in out.edges if e.source == "asg:web-asg" and e.target == "eni-ext")
    assert merged.relationship == "connects_to"
    assert merged.attributes["ports"] == "tcp/443, tcp/80"
    assert merged.attributes["via"] == "flow_log"
    # An inbound flow_peer re-points onto the ASG.
    assert ("flow-peer:203.0.113.5", "asg:web-asg", "connects_to") in _rels(out)
    # A member's security group membership re-points too.
    assert ("asg:web-asg", "sg-1", "secured_by") in _rels(out)


def test_intra_fleet_edges_are_dropped():
    out = collapse_autoscaling_groups(_fleet_graph())
    # No ENI->instance attached_to survives (both endpoints are the same ASG -> self-loop).
    assert not any(e.relationship == "attached_to" for e in out.edges)
    # No self-loop edge anywhere.
    assert not any(e.source == e.target for e in out.edges)


def test_two_asg_flow_becomes_one_asg_to_asg_edge():
    out = collapse_autoscaling_groups(_fleet_graph())
    asg_to_asg = [e for e in out.edges if e.source == "asg:web-asg" and e.target == "asg:db-asg"]
    assert len(asg_to_asg) == 1
    assert asg_to_asg[0].relationship == "connects_to"
    assert asg_to_asg[0].attributes["ports"] == "tcp/5432"


def test_no_asg_members_returns_graph_unchanged():
    g = Graph(meta={})
    g.add_node(Node("eni-1", "eni", "eni-1", {"private_ips": ["10.0.0.1"]}))
    g.add_node(Node("subnet-1", "subnet", "subnet-1"))
    g.add_edge(Edge("eni-1", "subnet-1", "in_subnet"))
    out = collapse_autoscaling_groups(g)
    assert out is g  # nothing to collapse -> the same object, byte-for-byte identical


def test_collapse_is_deterministic_and_idempotent():
    once = collapse_autoscaling_groups(_fleet_graph())
    again = collapse_autoscaling_groups(_fleet_graph())
    assert once.to_dict() == again.to_dict()  # deterministic
    twice = collapse_autoscaling_groups(once)
    assert twice.to_dict() == once.to_dict()  # idempotent
