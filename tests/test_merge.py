"""Tests for the ``cloudbreachgraph-merge`` auxiliary CLI and its core.

Fully offline and AWS-free: graphs and sources are constructed as plain JSON on disk, merged, and
the result reloaded through the model. The point of every assertion is the change request's
contract — an unrecognised (``needs_review``) ENI is upgraded from a user data file and/or a file of
older CloudTrail logs: guesses (``inferred_private_ips``, ``flow_peer`` / ``/32 cidr`` nodes) are
replaced by confirmed values, ``needs_review`` cleared, and owners + ASG membership attached.
"""

from __future__ import annotations

import json

from cloudbreachgraph import merge
from cloudbreachgraph.graph_io import load_json


# --------------------------------------------------------------------------- #
# Fixtures on disk
# --------------------------------------------------------------------------- #
def _write(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _unrecognised_graph(tmp_path, *, extra_nodes=(), extra_edges=()):
    """A graph.json with a VPC/subnet and one flagged unrecognised ENI (inferred 10.0.7.7)."""
    data = {
        "meta": {"tool_version": "test"},
        "nodes": [
            {"id": "vpc-1", "type": "vpc", "label": "vpc-1", "attributes": {"cidr": "10.0.0.0/16"}},
            {
                "id": "subnet-1",
                "type": "subnet",
                "label": "subnet-1",
                "attributes": {"vpc_id": "vpc-1"},
            },
            {
                "id": "eni-unknown00000001",
                "type": "eni",
                "label": "eni-unknown00000001",
                "attributes": {
                    "unrecognised": True,
                    "origin": "flow_log",
                    "needs_review": True,
                    "private_ips": [],
                    "inferred_private_ips": [
                        {"ip": "10.0.7.7", "method": "vpc_cidr", "confidence": "high"}
                    ],
                },
            },
            *extra_nodes,
        ],
        "edges": [
            {"source": "subnet-1", "target": "vpc-1", "relationship": "in_vpc", "attributes": {}},
            *extra_edges,
        ],
    }
    return _write(tmp_path / "graph.json", data)


def _data_file(tmp_path, enis):
    return _write(tmp_path / "data.json", {"enis": enis})


def _ct_run_instances(tmp_path, eni_id, ip, instance_id, asg):
    detail = {
        "eventTime": "2026-02-01T00:00:00+00:00",
        "eventName": "RunInstances",
        "responseElements": {
            "instancesSet": {
                "items": [
                    {
                        "instanceId": instance_id,
                        "tagSet": {
                            "items": [
                                {"key": "aws:autoscaling:groupName", "value": asg},
                                {"key": "Name", "value": "web"},
                            ]
                        },
                        "networkInterfaceSet": {
                            "items": [
                                {
                                    "networkInterfaceId": eni_id,
                                    "privateIpAddress": ip,
                                    "subnetId": "subnet-1",
                                    "vpcId": "vpc-1",
                                    "groupSet": {"items": []},
                                }
                            ]
                        },
                    }
                ]
            }
        },
    }
    event = {
        "EventName": "RunInstances",
        "EventTime": "2026-02-01T00:00:00+00:00",
        "CloudTrailEvent": json.dumps(detail),
    }
    return _write(tmp_path / "ct.json", {"Events": [event]})


# --------------------------------------------------------------------------- #
# Data-file merge — upgrade an unrecognised ENI
# --------------------------------------------------------------------------- #
def test_data_file_upgrades_unrecognised_eni(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    dp = _data_file(
        tmp_path,
        [
            {
                "id": "eni-unknown00000001",
                "private_ips": ["10.0.7.7"],
                "owner": {"id": "i-0abc", "type": "ec2_instance", "name": "web-1"},
                "asg": "web-asg",
                "subnet_id": "subnet-1",
                "vpc_id": "vpc-1",
            }
        ],
    )
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(dp), "-o", str(out)]) == 0

    g = load_json(out)
    eni = g.get_node("eni-unknown00000001")
    # Guesses replaced by confirmed values; needs_review / unrecognised cleared.
    assert eni.attributes["private_ips"] == ["10.0.7.7"]
    assert "inferred_private_ips" not in eni.attributes
    assert "unrecognised" not in eni.attributes
    assert "needs_review" not in eni.attributes
    assert eni.attributes["origin"] == "data_file"
    assert eni.attributes["asg_name"] == "web-asg"
    # Owner attached, ASG membership on the instance, subnet placement.
    owner = g.get_node("i-0abc")
    assert owner is not None and owner.type == "ec2_instance" and owner.label == "web-1"
    assert owner.attributes["asg_name"] == "web-asg"
    rels = {(e.source, e.target, e.relationship) for e in g.edges}
    assert ("eni-unknown00000001", "i-0abc", "attached_to") in rels
    assert ("eni-unknown00000001", "subnet-1", "in_subnet") in rels


def test_merge_absorbs_flow_peer_matching_confirmed_ip(tmp_path):
    # A flow_peer node whose IP the ENI actually owns is removed and its edge re-pointed to the ENI.
    gp = _unrecognised_graph(
        tmp_path,
        extra_nodes=[
            {
                "id": "flow-peer:10.0.7.7",
                "type": "flow_peer",
                "label": "10.0.7.7",
                "attributes": {"ip": "10.0.7.7"},
            },
            {
                "id": "eni-00instance0000001",
                "type": "eni",
                "label": "eni-00instance0000001",
                "attributes": {"private_ips": ["10.0.0.1"]},
            },
        ],
        extra_edges=[
            {
                "source": "flow-peer:10.0.7.7",
                "target": "eni-00instance0000001",
                "relationship": "connects_to",
                "attributes": {"ports": "tcp/443", "via": "flow_log"},
            }
        ],
    )
    dp = _data_file(tmp_path, [{"id": "eni-unknown00000001", "private_ips": ["10.0.7.7"]}])
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(dp), "-o", str(out)]) == 0

    g = load_json(out)
    assert g.get_node("flow-peer:10.0.7.7") is None  # absorbed
    rels = {(e.source, e.target, e.relationship) for e in g.edges}
    # The connects_to edge now runs ENI → ENI (re-pointed off the absorbed peer).
    assert ("eni-unknown00000001", "eni-00instance0000001", "connects_to") in rels
    assert not any(s == "flow-peer:10.0.7.7" for s, _, _ in rels)


def test_merge_absorbs_cidr_32_reachability_source(tmp_path):
    gp = _unrecognised_graph(
        tmp_path,
        extra_nodes=[
            {
                "id": "cidr:10.0.7.7/32",
                "type": "cidr",
                "label": "10.0.7.7/32",
                "attributes": {"cidr": "10.0.7.7/32"},
            },
            {
                "id": "eni-00instance0000001",
                "type": "eni",
                "label": "eni-00instance0000001",
                "attributes": {"private_ips": ["10.0.0.1"]},
            },
        ],
        extra_edges=[
            {
                "source": "cidr:10.0.7.7/32",
                "target": "eni-00instance0000001",
                "relationship": "can_reach",
                "attributes": {"ports": "tcp/22"},
            }
        ],
    )
    dp = _data_file(tmp_path, [{"id": "eni-unknown00000001", "private_ips": ["10.0.7.7"]}])
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(dp), "-o", str(out)]) == 0
    g = load_json(out)
    assert g.get_node("cidr:10.0.7.7/32") is None
    rels = {(e.source, e.target, e.relationship) for e in g.edges}
    assert ("eni-unknown00000001", "eni-00instance0000001", "can_reach") in rels


# --------------------------------------------------------------------------- #
# CloudTrail merge — reconstruct + upgrade
# --------------------------------------------------------------------------- #
def test_cloudtrail_reconstructs_and_upgrades(tmp_path):
    gp = _unrecognised_graph(
        tmp_path,
        extra_nodes=[
            {
                "id": "eni-asg000000000001",
                "type": "eni",
                "label": "eni-asg000000000001",
                "attributes": {
                    "unrecognised": True,
                    "origin": "flow_log",
                    "needs_review": True,
                    "private_ips": [],
                    "inferred_private_ips": [
                        {"ip": "10.0.9.11", "method": "recurring_side", "confidence": "low"}
                    ],
                },
            }
        ],
    )
    cp = _ct_run_instances(tmp_path, "eni-asg000000000001", "10.0.9.11", "i-asg1", "web-asg")
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--cloudtrail", str(cp), "-o", str(out)]) == 0

    g = load_json(out)
    eni = g.get_node("eni-asg000000000001")
    assert eni.attributes["private_ips"] == ["10.0.9.11"]
    assert "needs_review" not in eni.attributes
    assert eni.attributes["origin"] == "cloudtrail"
    assert eni.attributes["asg_name"] == "web-asg"
    assert g.get_node("i-asg1") is not None
    rels = {(e.source, e.target, e.relationship) for e in g.edges}
    assert ("eni-asg000000000001", "i-asg1", "attached_to") in rels
    # The other unrecognised ENI (untouched by this source) keeps its needs_review flag.
    assert g.get_node("eni-unknown00000001").attributes["needs_review"] is True


def test_both_sources_in_tandem(tmp_path):
    gp = _unrecognised_graph(
        tmp_path,
        extra_nodes=[
            {
                "id": "eni-asg000000000001",
                "type": "eni",
                "label": "eni-asg000000000001",
                "attributes": {
                    "unrecognised": True,
                    "needs_review": True,
                    "private_ips": [],
                    "inferred_private_ips": [],
                },
            }
        ],
    )
    dp = _data_file(
        tmp_path,
        [
            {
                "id": "eni-unknown00000001",
                "private_ips": ["10.0.7.7"],
                "owner": {"id": "i-data", "type": "ec2_instance"},
            }
        ],
    )
    cp = _ct_run_instances(tmp_path, "eni-asg000000000001", "10.0.9.11", "i-asg1", "web-asg")
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(dp), "--cloudtrail", str(cp), "-o", str(out)]) == 0

    g = load_json(out)
    # Both ENIs upgraded from their respective source.
    assert g.get_node("eni-unknown00000001").attributes["private_ips"] == ["10.0.7.7"]
    assert "needs_review" not in g.get_node("eni-unknown00000001").attributes
    assert g.get_node("eni-asg000000000001").attributes["private_ips"] == ["10.0.9.11"]
    assert "needs_review" not in g.get_node("eni-asg000000000001").attributes
    assert g.get_node("i-data") is not None and g.get_node("i-asg1") is not None


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #
def test_template_lists_needs_review_enis(tmp_path, capsys):
    gp = _unrecognised_graph(tmp_path)
    assert merge.main([str(gp), "--template"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in out["enis"]] == ["eni-unknown00000001"]
    row = out["enis"][0]
    # The guesses are echoed as a hint; the fill-in fields are blank.
    assert row["inferred_private_ips"] == [
        {"ip": "10.0.7.7", "method": "vpc_cidr", "confidence": "high"}
    ]
    assert row["private_ips"] == [] and row["owner"]["id"] == ""


def test_template_to_file(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    out = tmp_path / "skeleton.json"
    assert merge.main([str(gp), "--template", "-o", str(out)]) == 0
    skel = json.loads(out.read_text())
    assert [e["id"] for e in skel["enis"]] == ["eni-unknown00000001"]


def test_template_round_trips_into_a_no_op_when_left_blank(tmp_path):
    # A template filled with nothing is a no-op: the merged graph equals the input byte-for-byte.
    gp = _unrecognised_graph(tmp_path)
    skel = tmp_path / "skeleton.json"
    merge.main([str(gp), "--template", "-o", str(skel)])
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(skel), "-o", str(out)]) == 0
    assert load_json(out).to_dict() == load_json(gp).to_dict()


# --------------------------------------------------------------------------- #
# Determinism, round-trip integrity, and CLI errors
# --------------------------------------------------------------------------- #
def test_merge_is_deterministic(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    dp = _data_file(
        tmp_path,
        [
            {
                "id": "eni-unknown00000001",
                "private_ips": ["10.0.7.7"],
                "owner": {"id": "i-0abc", "type": "ec2_instance"},
                "asg": "web-asg",
            }
        ],
    )
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    merge.main([str(gp), "--data", str(dp), "-o", str(out1)])
    merge.main([str(gp), "--data", str(dp), "-o", str(out2)])
    assert out1.read_text() == out2.read_text()


def test_merge_edges_point_at_real_nodes(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    dp = _data_file(
        tmp_path,
        [
            {
                "id": "eni-unknown00000001",
                "private_ips": ["10.0.7.7"],
                "owner": {"id": "i-0abc", "type": "ec2_instance"},
                "subnet_id": "subnet-1",
                "vpc_id": "vpc-1",
            }
        ],
    )
    out = tmp_path / "merged.json"
    merge.main([str(gp), "--data", str(dp), "-o", str(out)])
    g = load_json(out)
    ids = {n.id for n in g.nodes}
    for e in g.edges:
        assert e.source in ids and e.target in ids


def test_merge_default_output_beside_input(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    dp = _data_file(tmp_path, [{"id": "eni-unknown00000001", "private_ips": ["10.0.7.7"]}])
    assert merge.main([str(gp), "--data", str(dp)]) == 0
    assert (tmp_path / "merged_graph.json").is_file()


def test_merge_nothing_to_do_returns_2(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    assert merge.main([str(gp)]) == 2


def test_merge_missing_input_returns_2(tmp_path):
    assert merge.main([str(tmp_path / "nope.json"), "--data", str(tmp_path / "d.json")]) == 2


def test_merge_missing_data_file_returns_2(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    assert merge.main([str(gp), "--data", str(tmp_path / "nope.json")]) == 2


def test_merge_non_graph_input_returns_2(tmp_path):
    notgraph = _write(tmp_path / "x.json", {"hello": "world"})
    dp = _data_file(tmp_path, [])
    assert merge.main([str(notgraph), "--data", str(dp)]) == 2


def test_merge_invalid_data_json_returns_2(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert merge.main([str(gp), "--data", str(bad)]) == 2


def test_merge_no_informative_data_leaves_graph_unchanged(tmp_path):
    gp = _unrecognised_graph(tmp_path)
    dp = _data_file(tmp_path, [{"id": "eni-unknown00000001"}])  # no confirmed info
    out = tmp_path / "merged.json"
    assert merge.main([str(gp), "--data", str(dp), "-o", str(out)]) == 0
    assert load_json(out).to_dict() == load_json(gp).to_dict()
