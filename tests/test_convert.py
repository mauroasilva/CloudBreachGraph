"""Tests for the graph loader (``graph_io``) and the ``cloudbreachgraph-to-html`` CLI.

Fully offline: a graph is built from the recorded fixtures via the real collectors/builder
(mocking only ``runner.run_aws``), written to JSON/DOT, then loaded back and converted to
HTML. The JSON round-trip is asserted lossless; the DOT round-trip is best-effort.
"""

from __future__ import annotations

import pytest
from conftest import load_fixture

from cloudbreachgraph import convert
from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget
from cloudbreachgraph.graph_io import (
    GraphLoadError,
    graph_from_dict,
    load_dot,
    load_graph,
    load_json,
)
from cloudbreachgraph.mapping.builder import build_graph
from cloudbreachgraph.output import dot_export, html_export, json_export

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
# JSON loader — lossless round-trip
# --------------------------------------------------------------------------- #
def test_graph_from_dict_round_trips_losslessly(graph):
    reloaded = graph_from_dict(graph.to_dict())
    assert reloaded.to_dict() == graph.to_dict()  # byte-for-byte identical structure


def test_load_json_from_written_file(graph, tmp_path):
    path = json_export.write_json(graph, tmp_path / "graph.json")
    reloaded = load_json(path)
    assert reloaded.to_dict() == graph.to_dict()


def test_graph_from_dict_rejects_non_graph():
    with pytest.raises(GraphLoadError):
        graph_from_dict({"not": "a graph"})


def test_load_json_rejects_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(GraphLoadError):
        load_json(bad)


# --------------------------------------------------------------------------- #
# DOT loader — best-effort recovery of this tool's own output
# --------------------------------------------------------------------------- #
def test_load_dot_recovers_nodes_edges_and_flags(graph, tmp_path):
    path = dot_export.write_dot(graph, tmp_path / "graph.dot")
    reloaded = load_dot(path)

    orig_nodes = {n.id: n for n in graph.nodes}
    got_nodes = {n.id: n for n in reloaded.nodes}
    assert set(got_nodes) == set(orig_nodes)  # every node recovered
    # Types and names survive the DOT round-trip.
    for nid, node in got_nodes.items():
        assert node.type == orig_nodes[nid].type
        assert node.label == orig_nodes[nid].label
    # Every edge (source, target, relationship) recovered.
    orig_edges = {(e.source, e.target, e.relationship) for e in graph.edges}
    got_edges = {(e.source, e.target, e.relationship) for e in reloaded.edges}
    assert got_edges == orig_edges


def test_load_dot_recovers_public_exposure(graph, tmp_path):
    path = dot_export.write_dot(graph, tmp_path / "graph.dot")
    reloaded = load_dot(path)
    exposed = reloaded.get_node("eni-00instance0000001")
    assert exposed is not None and exposed.attributes.get("public_ips")
    # An ENI without a public IP stays unexposed.
    assert not reloaded.get_node("eni-00nlb00000000003").attributes.get("public_ips")


def test_load_dot_marks_synthetic(tmp_path):
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
    path = dot_export.write_dot(g, tmp_path / "g.dot")
    reloaded = load_dot(path)
    assert reloaded.get_node("subnet-missing").attributes.get("synthetic") is True


def test_load_dot_rejects_empty(tmp_path):
    empty = tmp_path / "empty.dot"
    empty.write_text("digraph x {\n}\n", encoding="utf-8")
    with pytest.raises(GraphLoadError):
        load_dot(empty)


# --------------------------------------------------------------------------- #
# Dispatch (load_graph) — format inference and overrides
# --------------------------------------------------------------------------- #
def test_load_graph_infers_format_from_extension(graph, tmp_path):
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    dp = dot_export.write_dot(graph, tmp_path / "graph.dot")
    assert load_graph(jp).to_dict() == graph.to_dict()
    assert {n.id for n in load_graph(dp).nodes} == {n.id for n in graph.nodes}


def test_load_graph_unknown_extension_errors(tmp_path):
    p = tmp_path / "graph.txt"
    p.write_text("whatever", encoding="utf-8")
    with pytest.raises(GraphLoadError):
        load_graph(p)


def test_load_graph_format_override(graph, tmp_path):
    # A .json file forced to be read as JSON regardless of a misleading name.
    p = tmp_path / "graph.data"
    json_export.write_json(graph, p)
    assert load_graph(p, fmt="json").to_dict() == graph.to_dict()


# --------------------------------------------------------------------------- #
# CLI: cloudbreachgraph-to-html
# --------------------------------------------------------------------------- #
def test_convert_json_to_html(graph, tmp_path):
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    rc = convert.main([str(jp)])
    assert rc == 0
    html = tmp_path / "graph.html"
    assert html.is_file()
    assert html.read_text().startswith("<!DOCTYPE html>")


def test_convert_json_matches_direct_pipeline(graph, tmp_path):
    # Converting the JSON must reproduce exactly what write_html produces directly.
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    convert.main([str(jp), "-o", str(tmp_path / "converted.html")])
    direct = html_export.build_html(graph)
    assert (tmp_path / "converted.html").read_text() == direct


def test_convert_dot_to_html(graph, tmp_path):
    dp = dot_export.write_dot(graph, tmp_path / "graph.dot")
    rc = convert.main([str(dp), "-o", str(tmp_path / "out.html")])
    assert rc == 0
    text = (tmp_path / "out.html").read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert "eni-00instance0000001" in text


def test_convert_missing_file_returns_2(tmp_path):
    rc = convert.main([str(tmp_path / "nope.json")])
    assert rc == 2


def test_convert_falls_back_to_dot_when_too_large(graph, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(html_export, "MAX_NODES", 0)
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    out = tmp_path / "big.html"
    rc = convert.main([str(jp), "-o", str(out)])
    assert rc == 0
    assert not out.exists()  # HTML skipped
    assert (tmp_path / "big.dot").is_file()  # fallback .dot written
    err = capsys.readouterr().err
    assert "too large" in err and "big.dot" in err


def test_convert_too_large_does_not_clobber_input_dot(graph, tmp_path, monkeypatch, capsys):
    # When the input already IS the fallback .dot path, don't overwrite it — just warn.
    monkeypatch.setattr(html_export, "MAX_NODES", 0)
    dp = dot_export.write_dot(graph, tmp_path / "graph.dot")
    before = dp.read_text()
    rc = convert.main([str(dp)])  # output defaults to graph.html; fallback would be graph.dot
    assert rc == 0
    assert dp.read_text() == before  # input untouched
    assert "too large" in capsys.readouterr().err
