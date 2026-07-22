"""Tests for the graph loader (``graph_io``) and the ``cloudbreachgraph-to-html`` CLI.

Fully offline: a graph is built from the recorded fixtures via the real collectors/builder
(mocking only ``runner.run_aws``), written to JSON/DOT, then loaded back and converted to
HTML. The JSON round-trip is asserted lossless; the DOT round-trip is best-effort.
"""

from __future__ import annotations

from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Ringed layout (--ringed)
# --------------------------------------------------------------------------- #
def test_ringed_view_data_groups_by_vpc_and_ring(graph):
    # Every node in the fixture resolves under the one VPC; rings split by type.
    group = html_export._vpc_group_of(graph)
    vpc = next(n for n in graph.nodes if n.type == "vpc")
    assert all(g == vpc.id for g in group.values())  # single-VPC fixture -> one cluster

    data = html_export._ringed_view_data(graph)
    assert len(data["clusters"]) == 1
    cluster = data["clusters"][0]
    assert cluster["label"] == vpc.label
    rings = cluster["rings"]
    assert len(rings) == 3  # subnets, then ENIs, then everything else
    assert 0 < rings[0] < rings[1] < rings[2]  # each successive ring is further out

    pos = {n["id"]: n for n in data["nodes"]}
    # VPC at the center; subnets on ring 1; ENIs on their own ring 2; everything else on ring 3.
    assert (pos[vpc.id]["x"], pos[vpc.id]["y"]) == (cluster["cx"], cluster["cy"])
    for n in graph.nodes:
        d = round(
            ((pos[n.id]["x"] - cluster["cx"]) ** 2 + (pos[n.id]["y"] - cluster["cy"]) ** 2) ** 0.5,
            1,
        )
        if n.type == "vpc":
            assert d == 0.0
        elif n.type == "subnet":
            assert d == round(rings[0], 1)
        elif n.type == "eni":
            assert d == round(rings[1], 1)
        else:
            assert d == round(rings[2], 1)


def _angle_of(node, cx, cy):
    import math

    return math.atan2(node["y"] - cy, node["x"] - cx)


def _angles_close(a, b, tol=1e-6):
    import math

    # Compare on the circle: the difference's unit vector must be ~ (1, 0).
    return abs(math.cos(a - b) - 1.0) < tol


def test_ringed_outer_node_aligns_with_its_single_eni(graph):
    # Each EC2/LB in the fixture is fronted by exactly one ENI, so it must sit on that spoke.
    data = html_export._ringed_view_data(graph)
    pos = {n["id"]: n for n in data["nodes"]}
    cx, cy = data["clusters"][0]["cx"], data["clusters"][0]["cy"]
    enis_of = {}
    for e in graph.edges:
        if e.relationship == "attached_to":
            enis_of.setdefault(e.target, []).append(e.source)
    assert enis_of  # sanity: the fixture has attachments
    for target, enis in enis_of.items():
        assert len(enis) == 1
        assert _angles_close(_angle_of(pos[target], cx, cy), _angle_of(pos[enis[0]], cx, cy))


def test_ringed_outer_node_uses_average_angle_of_multiple_enis():
    # An EC2 fronted by two ENIs must land at the circular mean of *their* two angles. Two
    # further ENIs attach elsewhere so ring 2 is non-degenerate (the two spokes aren't
    # antipodal) and the mean is a genuine intermediate angle.
    import math

    def eni(name, instance):
        return {
            "NetworkInterfaceId": name,
            "SubnetId": "subnet-1",
            "VpcId": "vpc-1",
            "InterfaceType": "interface",
            "Description": "",
            "Attachment": {"InstanceId": instance},
            "PrivateIpAddresses": [],
            "Groups": [],
        }

    collected = {
        "meta": {},
        "network_interfaces": [
            eni("eni-a", "i-shared"),
            eni("eni-b", "i-shared"),
            eni("eni-c", "i-other"),
            eni("eni-d", "i-other"),
        ],
    }
    g = build_graph(collected)
    data = html_export._ringed_view_data(g)
    pos = {n["id"]: n for n in data["nodes"]}
    cx, cy = data["clusters"][0]["cx"], data["clusters"][0]["cy"]
    a, b = _angle_of(pos["eni-a"], cx, cy), _angle_of(pos["eni-b"], cx, cy)
    expected = math.atan2(math.sin(a) + math.sin(b), math.cos(a) + math.cos(b))
    got = _angle_of(pos["i-shared"], cx, cy)
    assert _angles_close(got, expected)
    # It is genuinely averaged — between its two ENIs, on neither's exact spoke.
    assert not _angles_close(got, a) and not _angles_close(got, b)


def test_ringed_subnet_aligns_with_mean_angle_of_its_enis(graph):
    # In the fixture each subnet sits at the circular mean of the ENIs it contains.
    import math

    data = html_export._ringed_view_data(graph)
    pos = {n["id"]: n for n in data["nodes"]}
    cx, cy = data["clusters"][0]["cx"], data["clusters"][0]["cy"]
    enis_of_subnet = {}
    for e in graph.edges:
        if e.relationship == "in_subnet":
            enis_of_subnet.setdefault(e.target, []).append(e.source)
    assert enis_of_subnet  # sanity: the fixture has subnets with ENIs
    for subnet, enis in enis_of_subnet.items():
        angs = [_angle_of(pos[e], cx, cy) for e in enis]
        expected = math.atan2(sum(map(math.sin, angs)), sum(map(math.cos, angs)))
        assert _angles_close(_angle_of(pos[subnet], cx, cy), expected)


def test_ringed_enis_stay_grouped_near_their_subnet():
    # Two subnets with several ENIs each: every ENI must be angularly closest to its own
    # subnet, i.e. a subnet's interfaces cluster near it rather than scattering around the ring.
    import math

    def eni(name, subnet):
        return {
            "NetworkInterfaceId": name,
            "SubnetId": subnet,
            "VpcId": "vpc-1",
            "InterfaceType": "interface",
            "Description": "",
            "Attachment": {"InstanceId": None},
            "PrivateIpAddresses": [],
            "Groups": [],
        }

    enis_of_subnet = {"subnet-1": ["eni-a", "eni-b"], "subnet-2": ["eni-c", "eni-d", "eni-e"]}
    collected = {
        "meta": {},
        "network_interfaces": [eni(n, s) for s, names in enis_of_subnet.items() for n in names],
    }
    g = build_graph(collected)
    data = html_export._ringed_view_data(g)
    pos = {n["id"]: n for n in data["nodes"]}
    cx, cy = data["clusters"][0]["cx"], data["clusters"][0]["cy"]

    def circ_dist(a, b):
        return math.acos(max(-1.0, min(1.0, math.cos(a - b))))

    subnets = list(enis_of_subnet)
    for subnet, enis in enis_of_subnet.items():
        for e in enis:
            ea = _angle_of(pos[e], cx, cy)
            nearest = min(subnets, key=lambda s: circ_dist(ea, _angle_of(pos[s], cx, cy)))
            assert nearest == subnet


def test_ringed_unassigned_nodes_form_their_own_cluster():
    # An ENI in no subnet (thus no VPC) collects into the trailing "unassigned" cluster.
    collected = {
        "meta": {},
        "network_interfaces": [
            {
                "NetworkInterfaceId": "eni-lonely",
                "SubnetId": None,
                "VpcId": None,
                "InterfaceType": "interface",
                "Description": "",
                "Attachment": {"InstanceId": None},
                "PrivateIpAddresses": [],
                "Groups": [],
            }
        ],
    }
    g = build_graph(collected)
    group = html_export._vpc_group_of(g)
    assert group["eni-lonely"] == html_export._UNASSIGNED
    data = html_export._ringed_view_data(g)
    assert data["clusters"][-1]["label"] == "unassigned"


def test_ringed_build_is_deterministic(graph):
    assert html_export.build_ringed_html(graph) == html_export.build_ringed_html(graph)


def test_convert_ringed_json_to_html(graph, tmp_path):
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    rc = convert.main([str(jp), "--ringed", "-o", str(tmp_path / "ringed.html")])
    assert rc == 0
    text = (tmp_path / "ringed.html").read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert "ringed" in text  # ringed title/HUD
    # Reproduces exactly what the ringed writer produces directly.
    assert text == html_export.build_ringed_html(graph)


def test_convert_ringed_falls_back_to_dot_when_too_large(graph, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(html_export, "MAX_NODES", 0)
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    out = tmp_path / "big.html"
    rc = convert.main([str(jp), "--ringed", "-o", str(out)])
    assert rc == 0
    assert not out.exists()  # HTML skipped
    assert (tmp_path / "big.dot").is_file()  # fallback .dot written
    assert "too large" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Ringed layout — crossing-reduction / overlap-nudge optimizer (--optimize-passes)
# --------------------------------------------------------------------------- #
def _crossing_graph():
    # 4 subnets, each with one ENI; instance i-1 spans subnets 1 & 3, i-2 spans 2 & 4. In the
    # id-ordered baseline the connected subnets land on opposite sides, so their edges cross the
    # circle — exactly the case the optimizer should untangle.
    def eni(name, subnet, instance):
        return {
            "NetworkInterfaceId": name,
            "SubnetId": subnet,
            "VpcId": "vpc-1",
            "InterfaceType": "interface",
            "Description": "",
            "Attachment": {"InstanceId": instance},
            "PrivateIpAddresses": [],
            "Groups": [],
        }

    return build_graph(
        {
            "meta": {},
            "network_interfaces": [
                eni("eni-1", "subnet-1", "i-1"),
                eni("eni-2", "subnet-2", "i-2"),
                eni("eni-3", "subnet-3", "i-1"),
                eni("eni-4", "subnet-4", "i-2"),
            ],
        }
    )


def _total_edge_length(data):
    import math

    pos = {n["id"]: n for n in data["nodes"]}
    return sum(
        math.hypot(
            pos[e["source"]]["x"] - pos[e["target"]]["x"],
            pos[e["source"]]["y"] - pos[e["target"]]["y"],
        )
        for e in data["edges"]
    )


def test_ringed_optimize_reduces_edge_length():
    # Fewer/shorter crossing edges: pulling connected nodes together shrinks total edge length.
    g = _crossing_graph()
    base = _total_edge_length(html_export._ringed_view_data(g, 0))
    opt = _total_edge_length(html_export._ringed_view_data(g, 20))
    assert opt < base


def test_ringed_optimize_places_lb_sharing_subnets_adjacent():
    # The reported case: two subnets that share an instance/LB (subnet-1 & subnet-3 both host an
    # ENI of i-1) start on opposite sides of the ring and must be pulled angularly close — not
    # merely reordered into distant even slots.
    import math

    g = _crossing_graph()

    def separation_deg(data):
        pos = {n["id"]: n for n in data["nodes"]}
        c = data["clusters"][0]
        a1, a3 = (
            math.atan2(pos[s]["y"] - c["cy"], pos[s]["x"] - c["cx"])
            for s in ("subnet-1", "subnet-3")
        )
        return abs(math.degrees(math.atan2(math.sin(a1 - a3), math.cos(a1 - a3))))

    assert separation_deg(html_export._ringed_view_data(g, 0)) > 150  # baseline: opposite sides
    assert separation_deg(html_export._ringed_view_data(g, 20)) < 45  # optimized: adjacent


def test_ringed_optimize_leaves_no_overlaps():
    import math

    g = _crossing_graph()
    data = html_export._ringed_view_data(g, 20)
    ns = data["nodes"]
    for i in range(len(ns)):
        for j in range(i + 1, len(ns)):
            dist = math.hypot(ns[i]["x"] - ns[j]["x"], ns[i]["y"] - ns[j]["y"])
            # Disks must not overlap (their drawn radii must fit).
            assert dist >= html_export._node_radius(ns[i]) + html_export._node_radius(ns[j])


def test_ringed_optimize_is_deterministic():
    g = _crossing_graph()
    assert html_export.build_ringed_html(g, 20) == html_export.build_ringed_html(g, 20)


def test_ringed_passes_zero_is_unchanged(graph):
    # The default (0 passes) must be byte-identical to the pre-optimizer placement.
    assert html_export.build_ringed_html(graph, 0) == html_export.build_ringed_html(graph)
    assert html_export._ringed_view_data(graph, 0) == html_export._ringed_view_data(graph)


def test_ringed_optimize_converges_early():
    # Asking for many passes must not change the result once it has converged (no drift).
    g = _crossing_graph()
    assert html_export.build_ringed_html(g, 20) == html_export.build_ringed_html(g, 200)


def test_ringed_optimize_freezes_on_tangled_graph():
    # A densely cross-linked graph (instances spanning several subnets) makes the barycenter
    # iteration limit-cycle rather than settle; the cooling schedule must freeze it so a large
    # pass count is byte-stable. Without cooling build(120) != build(600) here.
    def eni(name, subnet, instance):
        return {
            "NetworkInterfaceId": name,
            "SubnetId": subnet,
            "VpcId": "vpc-1",
            "InterfaceType": "interface",
            "Description": "",
            "Attachment": {"InstanceId": instance},
            "PrivateIpAddresses": [],
            "Groups": [],
        }

    spans = {  # instance -> the subnets it spans (cross-links that tangle the rings)
        "i-a": ["subnet-0", "subnet-3"],
        "i-b": ["subnet-1", "subnet-4"],
        "i-c": ["subnet-2", "subnet-5"],
        "i-d": ["subnet-0", "subnet-2", "subnet-4"],
    }
    nis = [eni(f"eni-{inst}-{s}", s, inst) for inst, subs in spans.items() for s in subs]
    g = build_graph({"meta": {}, "network_interfaces": nis})
    assert html_export.build_ringed_html(g, 120) == html_export.build_ringed_html(g, 600)


def _count_crossings(data):
    pos = {n["id"]: (n["x"], n["y"]) for n in data["nodes"]}
    e = [(pos[x["source"]], pos[x["target"]]) for x in data["edges"]]

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return (v > 1e-9) - (v < -1e-9)

    def crosses(p, q, r, s):
        if p in (r, s) or q in (r, s):  # shared endpoint
            return False
        return orient(p, q, r) != orient(p, q, s) and orient(r, s, p) != orient(r, s, q)

    return sum(1 for i in range(len(e)) for j in range(i + 1, len(e)) if crosses(*e[i], *e[j]))


def test_ringed_crossing_reduction_beats_barycenter_only(monkeypatch):
    # Load balancers whose ENIs are interleaved across subnets leave whole spokes crossing after
    # the barycenter passes; the greedy relocation local search removes several of them.
    def eni(name, subnet, instance):
        return {
            "NetworkInterfaceId": name,
            "SubnetId": subnet,
            "VpcId": "vpc-1",
            "InterfaceType": "interface",
            "Description": "",
            "Attachment": {"InstanceId": instance},
            "PrivateIpAddresses": [],
            "Groups": [],
        }

    spans = {
        "i-a": ["s0", "s4"],
        "i-b": ["s1", "s5"],
        "i-c": ["s2", "s6"],
        "i-d": ["s3", "s7"],
        "i-e": ["s0", "s2", "s4", "s6"],
        "i-f": ["s1", "s3", "s5", "s7"],
    }
    nis = [eni(f"eni-{i}-{s}", s, i) for i, subs in spans.items() for s in subs]
    g = build_graph({"meta": {}, "network_interfaces": nis})

    monkeypatch.setattr(html_export, "_RELOC_MAX_NODES", 0)  # disable the local search
    barycenter_only = _count_crossings(html_export._ringed_view_data(g, 80))
    monkeypatch.undo()  # re-enable it
    with_relocation = _count_crossings(html_export._ringed_view_data(g, 80))
    assert with_relocation < barycenter_only


def test_convert_ringed_optimize_passes(graph, tmp_path):
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    out = tmp_path / "opt.html"
    rc = convert.main([str(jp), "--ringed", "--optimize-passes", "10", "-o", str(out)])
    assert rc == 0
    assert out.read_text() == html_export.build_ringed_html(graph, 10)


def test_convert_optimize_passes_negative_errors(graph, tmp_path):
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    rc = convert.main([str(jp), "--optimize-passes", "-1"])
    assert rc == 2


# --------------------------------------------------------------------------- #
# Overlap-free layout — node/edge overlap elimination (--max-passes)
# --------------------------------------------------------------------------- #
_EXAMPLE_GRAPH = Path(__file__).resolve().parents[1] / "docs" / "examples" / "example-graph.json"


def test_optimized_layout_removes_all_overlaps_small():
    # On the crossing fixture the optimiser must leave zero node-node and zero edge-node overlaps.
    g = _crossing_graph()
    data = html_export._optimized_view_data(g, 2000)
    assert html_export._count_overlaps(data["nodes"], data["edges"]) == (0, 0)


def test_optimized_layout_reaches_zero_on_example_graph():
    # The acceptance criteria: the checked-in example graph must reach 0 node and 0 edge overlap
    # in far fewer than 10000 optimisation passes.
    graph = load_graph(_EXAMPLE_GRAPH)
    data = html_export._view_data(graph)
    used = html_export._optimize_layout(data["nodes"], data["edges"], 10000)
    assert used < 10000
    assert html_export._count_overlaps(data["nodes"], data["edges"]) == (0, 0)


def test_connected_components_splits_disjoint_graphs():
    nodes = [{"id": x, "type": "eni"} for x in ("a", "b", "c", "d")]
    edges = [{"source": "a", "target": "b"}, {"source": "c", "target": "d"}]
    comps = html_export._connected_components(nodes, edges)
    got = sorted(sorted(n["id"] for n in cn) for cn, _ in comps)
    assert got == [["a", "b"], ["c", "d"]]


def test_optimized_layout_keeps_components_apart():
    # The example graph's independent VPCs must not overlap: each connected component's bounding
    # box stays clear of every other's, so the reader can tell which clusters are separate.
    graph = load_graph(_EXAMPLE_GRAPH)
    data = html_export._view_data(graph)
    html_export._optimize_layout(data["nodes"], data["edges"], 10000)
    comps = html_export._connected_components(data["nodes"], data["edges"])
    assert len(comps) > 1  # the example really is disconnected

    def bbox(comp_nodes):
        r = html_export._node_radius
        return (
            min(n["x"] - r(n) for n in comp_nodes),
            min(n["y"] - r(n) for n in comp_nodes),
            max(n["x"] + r(n) for n in comp_nodes),
            max(n["y"] + r(n) for n in comp_nodes),
        )

    boxes = [bbox(cn) for cn, _ in comps]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            disjoint = a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]
            assert disjoint, f"components {i} and {j} overlap"


def test_optimized_build_is_deterministic(graph):
    assert html_export.build_optimized_html(graph, 500) == html_export.build_optimized_html(
        graph, 500
    )


def test_optimized_html_is_self_contained_and_labelled(graph):
    text = html_export.build_optimized_html(graph, 500)
    assert "overlap-free" in text  # variant title / HUD badge
    assert "http://" not in text and "https://" not in text  # no external assets


def test_count_overlaps_detects_a_planted_overlap():
    # Two node disks placed on top of each other, and a third node sitting on the edge between two
    # others — one node-node overlap and one edge-node overlap.
    nodes = [
        {"id": "a", "type": "eni", "x": 0.0, "y": 0.0},
        {"id": "b", "type": "eni", "x": 1.0, "y": 0.0},  # overlaps a (radius 9 each)
        {"id": "c", "type": "eni", "x": 100.0, "y": 0.0},
        {"id": "d", "type": "eni", "x": 200.0, "y": 0.0},
        {"id": "e", "type": "eni", "x": 150.0, "y": 0.0},  # sits on the c-d segment
    ]
    edges = [{"source": "c", "target": "d"}]
    assert html_export._count_overlaps(nodes, edges) == (1, 1)


def test_count_crossings_detects_a_planted_crossing():
    # A horizontal edge a-b and a vertical edge c-d that pass through the middle -> one crossing.
    nodes = [
        {"id": "a", "type": "eni", "x": -10.0, "y": 0.0},
        {"id": "b", "type": "eni", "x": 10.0, "y": 0.0},
        {"id": "c", "type": "eni", "x": 0.0, "y": -10.0},
        {"id": "d", "type": "eni", "x": 0.0, "y": 10.0},
    ]
    edges = [{"source": "a", "target": "b"}, {"source": "c", "target": "d"}]
    assert html_export._count_crossings(nodes, edges) == 1
    # Edges that merely share an endpoint never count as a crossing.
    assert html_export._count_crossings(nodes, [edges[0], {"source": "a", "target": "d"}]) == 0


def test_optimized_layout_reduces_crossings(monkeypatch):
    # The crossing-reduction phase must cut crossings versus running the unfold + projection alone,
    # while both keep the primary zero-overlap guarantee.
    g = _crossing_graph()

    def run(sweeps):
        monkeypatch.setattr(html_export, "_OPT_REDUCE_SWEEPS", sweeps)
        data = html_export._view_data(g)
        html_export._optimize_layout(data["nodes"], data["edges"], 10000)
        return (
            html_export._count_crossings(data["nodes"], data["edges"]),
            html_export._count_overlaps(data["nodes"], data["edges"]),
        )

    before_crossings, before_overlaps = run(0)
    after_crossings, after_overlaps = run(8)
    assert after_crossings < before_crossings  # phase 3 removed crossings
    assert before_overlaps == (0, 0)  # ... and neither layout has any overlap
    assert after_overlaps == (0, 0)


def test_convert_optimize_passes_writes_overlap_free(graph, tmp_path):
    # Without --ringed, --optimize-passes N (>0) renders the overlap-free layout.
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    out = tmp_path / "opt.html"
    rc = convert.main([str(jp), "--optimize-passes", "500", "-o", str(out)])
    assert rc == 0
    assert out.read_text() == html_export.build_optimized_html(graph, 500)
    assert "overlap-free" in out.read_text()


def test_convert_optimize_passes_zero_keeps_force_layout(graph, tmp_path):
    # --optimize-passes 0 (the default) leaves the plain in-browser force layout.
    jp = json_export.write_json(graph, tmp_path / "graph.json")
    out = tmp_path / "force.html"
    rc = convert.main([str(jp), "--optimize-passes", "0", "-o", str(out)])
    assert rc == 0
    assert out.read_text() == html_export.build_html(graph)
