"""Tests for the Phase 3 serializers: JSON and Graphviz DOT export.

Fully offline: a graph is built from the recorded fixtures via the real
collectors/builder (mocking only ``runner.run_aws``), then serialized.
"""

from __future__ import annotations

import json

import pytest
from conftest import load_fixture

from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget
from cloudbreachgraph.mapping.builder import build_graph
from cloudbreachgraph.output import dot_export, json_export

_COMMAND_FIXTURES = {
    ("ec2", "describe-network-interfaces"): "ec2_describe-network-interfaces.json",
    ("ec2", "describe-instances"): "ec2_describe-instances.json",
    ("elbv2", "describe-load-balancers"): "elbv2_describe-load-balancers.json",
    ("elb", "describe-load-balancers"): "elb_describe-load-balancers.json",
    ("ec2", "describe-subnets"): "ec2_describe-subnets.json",
    ("ec2", "describe-vpcs"): "ec2_describe-vpcs.json",
}


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(
        runner, "run_aws", lambda args, **k: load_fixture(_COMMAND_FIXTURES[tuple(args[:2])])
    )
    resolved = ResolvedTarget(
        target="prod",
        roles={"network": ResolvedAccount("prod-audit", "111111111111", "us-east-1")},
    )
    return build_graph(collectors.collect_all(resolved))


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def test_write_json_wellformed(graph, tmp_path):
    path = json_export.write_json(graph, tmp_path / "graph.json")
    assert path.is_file()
    data = json.loads(path.read_text())
    assert set(data) == {"meta", "nodes", "edges"}
    assert data["meta"]["tool_version"] == "0.1.0"
    assert data["meta"]["accounts"] == {"network": "111111111111"}
    ids = {n["id"] for n in data["nodes"]}
    assert "eni-00instance0000001" in ids
    assert any(n["type"] == "vpc" for n in data["nodes"])


def test_write_json_is_deterministic(graph, tmp_path):
    a = json_export.write_json(graph, tmp_path / "a.json").read_text()
    b = json_export.write_json(graph, tmp_path / "b.json").read_text()
    assert a == b  # no timestamp / stable ordering


def test_write_json_creates_parent_dirs(graph, tmp_path):
    path = json_export.write_json(graph, tmp_path / "nested" / "deep" / "graph.json")
    assert path.is_file()


# --------------------------------------------------------------------------- #
# DOT
# --------------------------------------------------------------------------- #
def test_write_dot_wellformed(graph, tmp_path):
    path = dot_export.write_dot(graph, tmp_path / "graph.dot")
    text = path.read_text()
    assert text.startswith("digraph cloudbreachgraph {")
    assert text.rstrip().endswith("}")
    # VPC clustering groups subnets/ENIs (the VPC's *contents*).
    assert "subgraph cluster_vpc_0aaaaaaaaaaaaaaaa {" in text
    assert 'label="VPC primary-vpc";' in text
    # The VPC is its own top-level node (2-space indent), NOT nested in its own cluster
    # (which would be 4-space indent), and subnets connect up to it via in_vpc edges.
    assert '\n  "vpc-0aaaaaaaaaaaaaaaa" [' in text
    assert '\n    "vpc-0aaaaaaaaaaaaaaaa" [' not in text
    assert '"subnet-011111111111111" -> "vpc-0aaaaaaaaaaaaaaaa" [label="in_vpc"' in text
    # Labels show "<aws-id> [<name>]" when named, and just "<aws-id>" when not.
    assert "i-0abc0000000000001 [web-server-1]" in text  # named instance
    assert "subnet-011111111111111 [public-1a]" in text  # named subnet
    assert "vpc-0aaaaaaaaaaaaaaaa [primary-vpc]" in text  # named vpc
    # An ENI has no Name tag -> id only, no bracketed name.
    assert "eni-00instance0000001\\ninterface" in text
    assert "eni-00instance0000001 [" not in text
    # ENI labels carry Private IP / Public IP sections.
    assert "Private IP: 10.0.1.10" in text
    assert "Public IP: 54.10.20.30" in text
    # ENIs with a public IP are connected to a generic "Internet" node.
    assert '"Internet" [label="Internet"' in text
    assert '"eni-00instance0000001" -> "Internet" [label="public_ip"];' in text
    # ENIs without a public IP are not connected to it.
    assert '"eni-00nlb00000000003" -> "Internet"' not in text
    # Nodes colored by type (a couple of representative fills).
    assert 'fillcolor="#E8F5E9"' in text  # eni
    assert 'fillcolor="#E3F2FD"' in text  # subnet
    # Edges labeled by relationship, with match_rule on the LB edge.
    assert 'label="in_subnet"' in text
    assert 'label="in_vpc"' in text
    assert "attached_to\\n(elbv2_description)" in text


def test_write_dot_marks_synthetic_dashed(tmp_path):
    # An ENI referencing a subnet that isn't in the collected set -> synthetic subnet node.
    collected = {
        "meta": {},
        "network_interfaces": [
            {
                "NetworkInterfaceId": "eni-orphan",
                "SubnetId": "subnet-missing",
                "VpcId": "vpc-missing",
                "InterfaceType": "interface",
                "Description": "",
                "Attachment": {"InstanceId": None},
                "PrivateIpAddresses": [],
                "Groups": [],
            }
        ],
    }
    g = build_graph(collected)
    text = dot_export.write_dot(g, tmp_path / "g.dot").read_text()
    assert 'style="filled,dashed"' in text  # synthetic subnet / vpc rendered dashed


def test_render_without_dot_returns_none(graph, tmp_path, monkeypatch):
    dot_path = dot_export.write_dot(graph, tmp_path / "graph.dot")
    monkeypatch.setattr(dot_export.shutil, "which", lambda _: None)
    assert dot_export.render(dot_path, "png") is None
    assert dot_path.is_file()  # .dot still there


def test_render_with_dot_invokes_binary(graph, tmp_path, monkeypatch):
    dot_path = dot_export.write_dot(graph, tmp_path / "graph.dot")
    calls = {}

    monkeypatch.setattr(dot_export.shutil, "which", lambda _: "/usr/bin/dot")

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(dot_export.subprocess, "run", _fake_run)
    out = dot_export.render(dot_path, "svg")
    assert out == dot_path.with_suffix(".svg")
    assert calls["cmd"][:2] == ["dot", "-Tsvg"]
