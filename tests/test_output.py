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
from cloudbreachgraph.model.graph import Edge, Graph, Node
from cloudbreachgraph.output import dot_export, html_export, json_export

_COMMAND_FIXTURES = {
    ("ec2", "describe-network-interfaces"): "ec2_describe-network-interfaces.json",
    ("ec2", "describe-instances"): "ec2_describe-instances.json",
    ("elbv2", "describe-load-balancers"): "elbv2_describe-load-balancers.json",
    ("elb", "describe-load-balancers"): "elb_describe-load-balancers.json",
    ("ec2", "describe-subnets"): "ec2_describe-subnets.json",
    ("ec2", "describe-vpcs"): "ec2_describe-vpcs.json",
    ("ec2", "describe-security-groups"): "ec2_describe-security-groups.json",
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
    # An ENI has no Name tag -> its own node line is id only, no bracketed name.
    assert "eni-00instance0000001\\ninterface" in text
    assert '"eni-00instance0000001" [label="[eni]\\neni-00instance0000001' in text
    # ENI labels carry Private IP / Public IP sections.
    assert "Private IP: 10.0.1.10" in text
    assert "Public IP: 54.10.20.30" in text
    # Reachability (§5.5): a per-ENI Internet node for the 0.0.0.0/0 inbound rule, plus a shared
    # CIDR source node and a referencing-security-group source node, each with a can_reach edge.
    assert '"internet:eni-00instance0000001" [label="[internet]' in text
    assert '"internet:eni-00instance0000001" -> "eni-00instance0000001" [label="can_reach' in text
    assert '"cidr:203.0.113.0/24" -> "eni-00instance0000001" [label="can_reach\\ntcp/22"];' in text
    assert '"sg-source:sg-0aaa0002" -> "eni-00instance0000001"' in text
    # The ALB ENI is exposed via its own per-ENI Internet node (not a shared one).
    assert '"internet:eni-00alb00000000002" -> "eni-00alb00000000002"' in text
    # An ENI with no security groups (the NAT gateway / NLB) has no reachability edge.
    assert '-> "eni-00natgw000000004" [label="can_reach' not in text
    assert '-> "eni-00nlb00000000003" [label="can_reach' not in text
    # Nodes colored by type (a couple of representative fills).
    assert 'fillcolor="#E8F5E9"' in text  # eni
    assert 'fillcolor="#E3F2FD"' in text  # subnet
    assert 'fillcolor="#FFEBEE"' in text  # internet source
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


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def test_write_html_is_self_contained(graph, tmp_path):
    path = html_export.write_html(graph, tmp_path / "graph.html")
    assert path is not None and path.is_file()
    text = path.read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert text.rstrip().endswith("</html>")
    # Fully self-contained: no external assets / network references at all.
    assert "http://" not in text and "https://" not in text
    assert "<script src" not in text and 'link rel="stylesheet"' not in text
    # The graph data is inlined and the placeholder was substituted.
    assert "__GRAPH_DATA__" not in text
    assert "const GRAPH =" in text
    # The force layout that self-distributes nodes is present.
    assert "REPULSION" in text and "requestAnimationFrame" in text
    # A representative node id is embedded in the inlined data.
    assert "eni-00instance0000001" in text


def test_write_html_has_recompute_button_and_cluster_separation(graph, tmp_path):
    text = html_export.write_html(graph, tmp_path / "graph.html").read_text()
    # A HUD button re-runs the layout on demand, wired to a recompute() handler.
    assert 'id="recompute"' in text
    assert "Recompute layout" in text
    assert "function recompute()" in text
    assert 'getElementById("recompute").addEventListener("click", recompute)' in text
    # recompute refines from CURRENT positions, not a full re-solve: it releases pins, anchors
    # each node to where it is, drops the centering gravity, and applies only a gentle reheat.
    assert "n.fixed = false" in text
    assert "n.ax = n.x" in text and "n.ay = n.y" in text  # per-node position anchors
    assert "anchored = true" in text
    assert "gravityScale = 0" in text
    assert "alpha = RECOMPUTE_ALPHA" in text
    # It must NOT do a full high-energy reheat (that was the re-tangling bug).
    assert "alpha = 1.0;        // reheat" not in text
    # The anchor tether is applied during integration and follows a dragged node.
    assert "(n.ax - n.x) * ANCHOR" in text
    # Connected components are computed and disconnected clusters repel harder, so
    # segregated clusters sit away from each other.
    assert "CROSS_COMPONENT" in text
    assert "a.comp !== b.comp" in text
    assert "componentCount" in text


def test_write_html_embeds_public_exposure(graph, tmp_path):
    text = html_export.write_html(graph, tmp_path / "graph.html").read_text()
    # An ENI with a public IP is flagged so the page can highlight internet exposure.
    assert '"public": true' in text


def test_write_html_has_zoom_controls_and_scroll_lock(graph, tmp_path):
    text = html_export.write_html(graph, tmp_path / "graph.html").read_text()
    # Zoom In / Zoom Out buttons wired to a shared zoomAround() helper (about the viewport center).
    assert 'id="zoomIn"' in text and 'id="zoomOut"' in text
    assert "function zoomAround(" in text
    assert 'getElementById("zoomIn")' in text and "zoomAround(W / 2, H / 2, 1.2)" in text
    assert 'getElementById("zoomOut")' in text and "zoomAround(W / 2, H / 2, 1 / 1.2)" in text
    # A checkbox toggle disables wheel zoom so only the buttons work; the wheel handler honors it.
    assert 'id="noscroll"' in text
    assert "let scrollZoomEnabled = true" in text
    assert "if (!scrollZoomEnabled) return;" in text


def test_write_html_is_deterministic(graph, tmp_path):
    a = html_export.write_html(graph, tmp_path / "a.html").read_text()
    b = html_export.write_html(graph, tmp_path / "b.html").read_text()
    assert a == b  # stable ordering, seeded layout, no timestamps


def test_write_html_falls_back_when_too_many_nodes(graph, tmp_path):
    # With a node cap of 0 the graph is "too big": nothing is written, None returned.
    result = html_export.write_html(graph, tmp_path / "graph.html", max_nodes=0)
    assert result is None
    assert not (tmp_path / "graph.html").exists()


def test_write_html_falls_back_when_too_many_bytes(graph, tmp_path):
    result = html_export.write_html(graph, tmp_path / "graph.html", max_bytes=10)
    assert result is None
    assert not (tmp_path / "graph.html").exists()


def test_write_html_size_guard_defaults_allow_small_graph(tmp_path):
    g = Graph(meta={})
    g.add_node(Node(id="vpc-1", type="vpc", label="vpc-1"))
    g.add_node(Node(id="subnet-1", type="subnet", label="subnet-1"))
    g.add_edge(Edge(source="subnet-1", target="vpc-1", relationship="in_vpc"))
    path = html_export.write_html(g, tmp_path / "graph.html")
    assert path is not None and path.is_file()


# --------------------------------------------------------------------------- #
# HTML export — ringed layout
# --------------------------------------------------------------------------- #
def test_write_ringed_html_is_self_contained(graph, tmp_path):
    path = html_export.write_ringed_html(graph, tmp_path / "ringed.html")
    assert path is not None and path.is_file()
    text = path.read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert text.rstrip().endswith("</html>")
    # Fully self-contained: no external assets / network references at all.
    assert "http://" not in text and "https://" not in text
    assert "<script src" not in text and 'link rel="stylesheet"' not in text
    assert "__GRAPH_DATA__" not in text and "const GRAPH =" in text
    # Ringed page: precomputed positions + per-cluster metadata, NOT a force simulation.
    assert "GRAPH.clusters" in text
    assert "REPULSION" not in text and "requestAnimationFrame" not in text
    # Rings are conveyed by node position only — no guide circles are stroked.
    assert "ctx.arc(p.x, p.y, rr" not in text
    assert "eni-00instance0000001" in text


def test_write_ringed_html_is_deterministic(graph, tmp_path):
    a = html_export.write_ringed_html(graph, tmp_path / "a.html").read_text()
    b = html_export.write_ringed_html(graph, tmp_path / "b.html").read_text()
    assert a == b  # positions computed from sorted nodes/edges; no timestamps


def test_write_ringed_html_has_zoom_controls_and_scroll_lock(graph, tmp_path):
    text = html_export.write_ringed_html(graph, tmp_path / "ringed.html").read_text()
    assert 'id="zoomIn"' in text and 'id="zoomOut"' in text
    assert "function zoomAround(" in text
    assert 'getElementById("zoomIn")' in text and "zoomAround(W / 2, H / 2, 1.2)" in text
    assert 'getElementById("zoomOut")' in text and "zoomAround(W / 2, H / 2, 1 / 1.2)" in text
    assert 'id="noscroll"' in text
    assert "let scrollZoomEnabled = true" in text
    assert "if (!scrollZoomEnabled) return;" in text


def test_write_ringed_html_embeds_public_exposure(graph, tmp_path):
    text = html_export.write_ringed_html(graph, tmp_path / "ringed.html").read_text()
    assert '"public": true' in text


def test_write_ringed_html_falls_back_when_too_many_nodes(graph, tmp_path):
    result = html_export.write_ringed_html(graph, tmp_path / "ringed.html", max_nodes=0)
    assert result is None
    assert not (tmp_path / "ringed.html").exists()


def test_write_ringed_html_falls_back_when_too_many_bytes(graph, tmp_path):
    result = html_export.write_ringed_html(graph, tmp_path / "ringed.html", max_bytes=10)
    assert result is None
    assert not (tmp_path / "ringed.html").exists()


# HTML export — overlap-free layout


def test_write_optimized_html_is_self_contained(graph, tmp_path):
    path = html_export.write_optimized_html(graph, tmp_path / "opt.html", max_passes=500)
    assert path is not None
    text = path.read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert "overlap-free" in text
    assert "http://" not in text and "https://" not in text


def test_write_optimized_html_is_deterministic(graph, tmp_path):
    a = html_export.write_optimized_html(graph, tmp_path / "a.html", max_passes=500).read_text()
    b = html_export.write_optimized_html(graph, tmp_path / "b.html", max_passes=500).read_text()
    assert a == b


def test_write_optimized_html_falls_back_when_too_many_nodes(graph, tmp_path):
    result = html_export.write_optimized_html(graph, tmp_path / "opt.html", max_nodes=0)
    assert result is None
    assert not (tmp_path / "opt.html").exists()


def test_write_optimized_html_falls_back_when_too_many_bytes(graph, tmp_path):
    result = html_export.write_optimized_html(graph, tmp_path / "opt.html", max_bytes=10)
    assert result is None
    assert not (tmp_path / "opt.html").exists()


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
