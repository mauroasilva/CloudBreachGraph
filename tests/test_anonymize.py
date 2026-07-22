"""Tests for the ``cloudbreachgraph-anonymize`` auxiliary CLI and its core.

Fully offline: a graph is built from the recorded fixtures via the real collectors/builder
(mocking only ``runner.run_aws``), then anonymised. The point of every assertion is the one
guarantee the tool makes — a source value maps to exactly one replacement *everywhere*, so all
references (including ones embedded inside ARNs, DNS names and ENI descriptions) stay
consistent — while nodes, edges and structural vocabulary are preserved.
"""

from __future__ import annotations

import json

import pytest
from conftest import load_fixture

from cloudbreachgraph import anonymize
from cloudbreachgraph.anonymize import Anonymizer, anonymize_graph
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
}


@pytest.fixture
def graph_dict(monkeypatch):
    monkeypatch.setattr(
        runner, "run_aws", lambda args, **k: load_fixture(_COMMAND_FIXTURES[tuple(args[:2])])
    )
    resolved = ResolvedTarget(
        target="prod",
        roles={"network": ResolvedAccount("prod-audit", "111111111111", "us-east-1")},
    )
    return build_graph(collectors.collect_all(resolved)).to_dict()


# --------------------------------------------------------------------------- #
# Structure preservation
# --------------------------------------------------------------------------- #
def test_preserves_every_node_and_edge(graph_dict):
    anon = anonymize_graph(graph_dict, seed=1)
    assert len(anon["nodes"]) == len(graph_dict["nodes"])
    assert len(anon["edges"]) == len(graph_dict["edges"])
    # Node types and edge relationships (the closed structural vocabulary) survive verbatim.
    assert sorted(n["type"] for n in anon["nodes"]) == sorted(
        n["type"] for n in graph_dict["nodes"]
    )
    assert sorted(e["relationship"] for e in anon["edges"]) == sorted(
        e["relationship"] for e in graph_dict["edges"]
    )
    # ...and so do attribute *keys* and the closed match_rule vocabulary.
    for orig, new in zip(graph_dict["nodes"], anon["nodes"], strict=True):
        assert set(orig["attributes"]) == set(new["attributes"])
    orig_rules = {e["attributes"].get("match_rule") for e in graph_dict["edges"]}
    new_rules = {e["attributes"].get("match_rule") for e in anon["edges"]}
    assert orig_rules == new_rules


def test_edge_endpoints_are_all_real_nodes(graph_dict):
    # Anonymisation must not break the graph: every edge still points at existing node ids.
    anon = anonymize_graph(graph_dict, seed=7)
    ids = {n["id"] for n in anon["nodes"]}
    for e in anon["edges"]:
        assert e["source"] in ids and e["target"] in ids


def test_actually_changes_identifiers(graph_dict):
    anon = anonymize_graph(graph_dict, seed=1)
    assert {n["id"] for n in anon["nodes"]} != {n["id"] for n in graph_dict["nodes"]}
    # No original ip / id / account leaks through anywhere in the serialized output.
    blob = json.dumps(anon)
    for leaked in ("10.0.1.20", "54.10.20.30", "111111111111", "my-alb", "my-nlb", "us-east-1"):
        assert leaked not in blob


# --------------------------------------------------------------------------- #
# The core guarantee: consistent, referential replacement
# --------------------------------------------------------------------------- #
def _node(nodes, node_id):
    return next(n for n in nodes if n["id"] == node_id)


def test_vpc_id_replacement_is_consistent_everywhere(graph_dict):
    anon = anonymize_graph(graph_dict, seed=3)
    # The single VPC is referenced by its node id, every subnet/instance/LB attribute, and edges.
    vpc = next(n for n in anon["nodes"] if n["type"] == "vpc")
    new_vpc_id = vpc["id"]
    assert new_vpc_id != "vpc-0aaaaaaaaaaaaaaaa"
    for n in anon["nodes"]:
        if "vpc_id" in n["attributes"]:
            assert n["attributes"]["vpc_id"] == new_vpc_id
    for e in anon["edges"]:
        if e["relationship"] == "in_vpc":
            assert e["target"] == new_vpc_id


def test_load_balancer_name_and_hash_flow_into_arn_dns_and_description(graph_dict):
    # my-alb / its hash appear in: the LB label, the LB ARN node id, the edge that targets it,
    # the LB DNS name, and the fronting ENI's Description token. All must move together.
    anon = anonymize_graph(graph_dict, seed=5)
    alb = next(
        n
        for n in anon["nodes"]
        if n["type"] == "load_balancer" and n["attributes"]["lb_type"] == "application"
    )
    new_name = alb["label"]
    assert new_name != "my-alb"
    assert new_name in alb["id"]  # ARN embeds the name
    assert new_name in alb["attributes"]["dns_name"]  # DNS embeds the name
    # The ENI whose description referenced the ALB now references the new name.
    eni = next(
        n
        for n in anon["nodes"]
        if n["type"] == "eni" and "ELB app/" in n["attributes"].get("description", "")
    )
    assert new_name in eni["attributes"]["description"]
    assert "my-alb" not in eni["attributes"]["description"]
    # And an edge targets exactly this (anonymised) ARN.
    assert any(e["target"] == alb["id"] for e in anon["edges"])


def test_nlb_hash_embedded_in_dns_matches_the_arn(graph_dict):
    # The NLB fixture embeds its ARN hash inside the DNS name too — the trickiest cross-ref.
    anon = anonymize_graph(graph_dict, seed=9)
    nlb = next(
        n
        for n in anon["nodes"]
        if n["type"] == "load_balancer" and n["attributes"]["lb_type"] == "network"
    )
    # ARN suffix hash == the token embedded in the DNS name.
    arn_hash = nlb["id"].rsplit("/", 1)[-1]
    assert arn_hash != "1a2b3c4d5e6f7a8b"
    assert arn_hash in nlb["attributes"]["dns_name"]


def test_region_and_az_stay_consistent(graph_dict):
    anon = anonymize_graph(graph_dict, seed=2)
    # Every AZ shares the same (new) region prefix, and it matches the region in the ARNs/DNS.
    azs = {
        n["attributes"]["availability_zone"]
        for n in anon["nodes"]
        if "availability_zone" in n["attributes"]
    }
    assert azs and all(az[-1] in "ab" for az in azs)  # AZ letters preserved
    new_regions = {az[:-1] for az in azs}
    assert len(new_regions) == 1 and "us-east-1" not in new_regions
    new_region = new_regions.pop()
    arn = next(n["id"] for n in anon["nodes"] if n["type"] == "load_balancer")
    assert f":{new_region}:" in arn


def test_ip_is_consistent_across_references():
    # A shared IP referenced from two ENIs must map to a single new value in both.
    data = {
        "meta": {},
        "nodes": [
            {
                "id": "eni-0a11111111",
                "type": "eni",
                "label": "eni-0a11111111",
                "attributes": {"private_ips": ["10.0.0.9"]},
            },
            {
                "id": "eni-0b22222222",
                "type": "eni",
                "label": "eni-0b22222222",
                "attributes": {"description": "peer 10.0.0.9"},
            },
        ],
        "edges": [],
    }
    anon = anonymize_graph(data, seed=1)
    holder = next(n for n in anon["nodes"] if "private_ips" in n["attributes"])
    referer = next(n for n in anon["nodes"] if "description" in n["attributes"])
    new_ip = holder["attributes"]["private_ips"][0]
    assert new_ip != "10.0.0.9"
    assert referer["attributes"]["description"] == f"peer {new_ip}"


# --------------------------------------------------------------------------- #
# Format preservation
# --------------------------------------------------------------------------- #
def test_cidr_keeps_prefix_length_and_class(graph_dict):
    import ipaddress

    def is_rfc1918(net):
        # The anonymiser's notion of "private" is RFC1918 (10/8, 172.16/12, 192.168/16) — it maps
        # anything else, including the 203.0.113.0/24 doc range, to a public replacement.
        return any(
            net.subnet_of(ipaddress.ip_network(b))
            for b in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )

    anon = anonymize_graph(graph_dict, seed=4)
    # Anonymisation preserves order, so pair each node with its scrubbed counterpart and check the
    # cidr attribute (subnet/vpc ranges plus §5.5 reachability CIDR sources) keeps its prefix
    # length and RFC1918 class — a private range stays private, a public source stays public.
    for orig, new in zip(graph_dict["nodes"], anon["nodes"], strict=True):
        old_cidr = orig["attributes"].get("cidr")
        new_cidr = new["attributes"].get("cidr")
        assert bool(old_cidr) == bool(new_cidr)
        if not old_cidr:
            continue
        old_net = ipaddress.ip_network(old_cidr, strict=False)
        net = ipaddress.ip_network(new_cidr, strict=False)
        assert new_cidr == str(net)  # host bits zeroed -> canonical network form
        assert net.prefixlen == old_net.prefixlen  # prefix length preserved
        assert is_rfc1918(net) == is_rfc1918(old_net)  # private/public class preserved


def test_private_stays_private_public_stays_public(graph_dict):
    import ipaddress

    anon = anonymize_graph(graph_dict, seed=6)
    for n in anon["nodes"]:
        for ip in n["attributes"].get("private_ips", []):
            assert ipaddress.ip_address(ip).is_private
        for ip in n["attributes"].get("public_ips", []):
            assert ipaddress.ip_address(ip).is_global


def test_resource_ids_keep_prefix_and_suffix_length(graph_dict):
    anon = anonymize_graph(graph_dict, seed=8)
    for n in anon["nodes"]:
        prefixes = {"ec2_instance": "i", "eni": "eni", "subnet": "subnet", "vpc": "vpc"}
        if n["type"] in prefixes:
            assert n["id"].startswith(prefixes[n["type"]] + "-")


def test_injective_no_two_nodes_collapse(graph_dict):
    anon = anonymize_graph(graph_dict, seed=11)
    ids = [n["id"] for n in anon["nodes"]]
    assert len(ids) == len(set(ids))  # still unique after scrubbing


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_same_seed_is_reproducible(graph_dict):
    assert anonymize_graph(graph_dict, seed=42) == anonymize_graph(graph_dict, seed=42)


def test_different_seeds_differ(graph_dict):
    assert anonymize_graph(graph_dict, seed=1) != anonymize_graph(graph_dict, seed=2)


def test_mapping_is_available_on_the_anonymizer(graph_dict):
    anon = Anonymizer(seed=1)
    anon.anonymize(graph_dict)
    assert anon.mapping  # non-empty source->replacement map exposed for inspection
    assert "vpc-0aaaaaaaaaaaaaaaa" in anon.mapping


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_writes_anonymised_graph(graph_dict, tmp_path):
    in_path = tmp_path / "graph.json"
    in_path.write_text(json.dumps(graph_dict), encoding="utf-8")
    rc = anonymize.main([str(in_path), "--seed", "1"])
    assert rc == 0
    out = tmp_path / "anonymised_graph.json"
    assert out.is_file()
    written = json.loads(out.read_text())
    assert len(written["nodes"]) == len(graph_dict["nodes"])
    assert "111111111111" not in out.read_text()


def test_cli_explicit_output_and_seed_is_reproducible(graph_dict, tmp_path):
    in_path = tmp_path / "graph.json"
    in_path.write_text(json.dumps(graph_dict), encoding="utf-8")
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    assert anonymize.main([str(in_path), "--seed", "5", "-o", str(out1)]) == 0
    assert anonymize.main([str(in_path), "--seed", "5", "-o", str(out2)]) == 0
    assert out1.read_text() == out2.read_text()  # deterministic for a fixed seed


def test_cli_missing_file_returns_2(tmp_path):
    assert anonymize.main([str(tmp_path / "nope.json")]) == 2


def test_cli_invalid_json_returns_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert anonymize.main([str(bad)]) == 2


def test_cli_non_graph_returns_2(tmp_path):
    notgraph = tmp_path / "x.json"
    notgraph.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert anonymize.main([str(notgraph)]) == 2
