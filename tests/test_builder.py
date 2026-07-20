"""Tests for the relationship-mapping builder (``mapping/builder.py``).

Covers every rule in ``docs/02_architecture.md §5`` plus the graph invariants required by
``docs/03_phase_plan.md``:

* instance-attached ENI, ALB ENI, NLB ENI, Classic-ELB ENI,
* unattached service ENI (NAT gateway), InterfaceType fallback,
* missing-subnet / missing-VPC synthetic nodes,
* no ENI attached to both instance and LB; one ``in_subnet`` per ENI; one ``in_vpc`` per subnet;
* deterministic ordering.
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
}


@pytest.fixture
def full_bundle(monkeypatch):
    """A real Phase 1 ``collect_all()`` bundle served from the recorded fixtures (offline)."""

    def _run(args, *, profile=None, region=None, cache_dir=None):
        return load_fixture(_COMMAND_FIXTURES[tuple(args[:2])])

    monkeypatch.setattr(runner, "run_aws", _run)
    resolved = ResolvedTarget(
        target="prod",
        roles={
            "network": ResolvedAccount(
                profile="prod-audit", account_id="111111111111", region="us-east-1"
            )
        },
    )
    return collectors.collect_all(resolved)


# --------------------------------------------------------------------------- #
# Hand-built normalized bundles for the rules the fixtures don't exercise.
# --------------------------------------------------------------------------- #
def _eni(
    eni_id,
    *,
    subnet_id="subnet-1",
    vpc_id="vpc-1",
    interface_type="interface",
    description="",
    instance_id=None,
):
    return {
        "NetworkInterfaceId": eni_id,
        "SubnetId": subnet_id,
        "VpcId": vpc_id,
        "InterfaceType": interface_type,
        "Description": description,
        "Status": "in-use",
        "AvailabilityZone": "us-east-1a",
        "RequesterId": None,
        "RequesterManaged": False,
        "Attachment": {
            "AttachmentId": None,
            "InstanceId": instance_id,
            "InstanceOwnerId": None,
            "DeviceIndex": None,
            "Status": None,
        },
        "PrivateIpAddresses": [],
        "Groups": [],
    }


def _bundle(**kw):
    base = {
        "meta": {"target": None, "region": "us-east-1", "accounts": {"network": "111111111111"}},
        "network_interfaces": [],
        "ec2_instances": [],
        "load_balancers_v2": [],
        "load_balancers_classic": [],
        "subnets": [],
        "vpcs": [],
    }
    base.update(kw)
    return base


def _edges_from(graph, eni_id, relationship):
    return [e for e in graph.edges if e.source == eni_id and e.relationship == relationship]


def _attached_targets(graph, eni_id):
    return _edges_from(graph, eni_id, "attached_to")


# --------------------------------------------------------------------------- #
# §5.3 — instance attachment
# --------------------------------------------------------------------------- #
def test_instance_attached_eni(full_bundle):
    graph = build_graph(full_bundle)
    attached = _attached_targets(graph, "eni-00instance0000001")
    assert len(attached) == 1
    edge = attached[0]
    assert edge.target == "i-0abc0000000000001"
    assert edge.attributes == {}  # no match_rule for a plain instance attachment
    inst = graph.get_node("i-0abc0000000000001")
    assert inst.type == "ec2_instance"
    assert inst.label == "web-server-1"


def test_instance_attached_eni_is_never_also_attached_to_lb(full_bundle):
    graph = build_graph(full_bundle)
    # Even though there are load balancers in the account, an instance ENI attaches only once.
    assert len(_attached_targets(graph, "eni-00instance0000001")) == 1


# --------------------------------------------------------------------------- #
# §5.4.1 — ELBv2 (ALB / NLB) via description
# --------------------------------------------------------------------------- #
def test_alb_eni_matches_elbv2_description(full_bundle):
    graph = build_graph(full_bundle)
    attached = _attached_targets(graph, "eni-00alb00000000002")
    assert len(attached) == 1
    edge = attached[0]
    assert edge.target.endswith("loadbalancer/app/my-alb/50dc6c495c0c9188")
    assert edge.attributes == {"match_rule": "elbv2_description"}
    lb = graph.get_node(edge.target)
    assert lb.type == "load_balancer"
    assert lb.label == "my-alb"
    assert lb.attributes["lb_type"] == "application"


def test_nlb_eni_matches_elbv2_description(full_bundle):
    graph = build_graph(full_bundle)
    attached = _attached_targets(graph, "eni-00nlb00000000003")
    assert len(attached) == 1
    edge = attached[0]
    assert edge.target.endswith("loadbalancer/net/my-nlb/1a2b3c4d5e6f7a8b")
    assert edge.attributes == {"match_rule": "elbv2_description"}
    assert graph.get_node(edge.target).attributes["lb_type"] == "network"


# --------------------------------------------------------------------------- #
# §5.4.2 — Classic ELB via description
# --------------------------------------------------------------------------- #
def test_classic_elb_eni_matches_by_name():
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-classic", description="ELB legacy-classic-elb", interface_type="interface")
        ],
        load_balancers_classic=[
            collectors._normalize_classic_elb(
                load_fixture("elb_describe-load-balancers.json")["LoadBalancerDescriptions"][0]
            )
        ],
        subnets=[
            collectors._normalize_subnet(s)
            for s in load_fixture("ec2_describe-subnets.json")["Subnets"]
        ],
        vpcs=[collectors._normalize_vpc(v) for v in load_fixture("ec2_describe-vpcs.json")["Vpcs"]],
    )
    graph = build_graph(bundle)
    attached = _attached_targets(graph, "eni-classic")
    assert len(attached) == 1
    edge = attached[0]
    assert edge.target == "legacy-classic-elb"
    assert edge.attributes == {"match_rule": "classic_elb_description"}
    assert graph.get_node("legacy-classic-elb").attributes["lb_type"] == "classic"


# --------------------------------------------------------------------------- #
# §5.4 tail — unattached service ENI (NAT gateway)
# --------------------------------------------------------------------------- #
def test_nat_gateway_eni_has_no_attachment(full_bundle):
    graph = build_graph(full_bundle)
    eni_id = "eni-00natgw000000004"
    assert _attached_targets(graph, eni_id) == []
    # Still mapped to its subnet, and tagged with its interface type so the map explains it.
    assert len(_edges_from(graph, eni_id, "in_subnet")) == 1
    assert graph.get_node(eni_id).attributes["interface_type"] == "nat_gateway"


# --------------------------------------------------------------------------- #
# §5.4.3 — InterfaceType fallback (LB-type ENI whose description didn't resolve)
# --------------------------------------------------------------------------- #
def test_interface_type_fallback_creates_unresolved_lb():
    bundle = _bundle(
        network_interfaces=[
            _eni(
                "eni-ghost-nlb",
                interface_type="network_load_balancer",
                description="ELB net/ghost-nlb/deadbeefdeadbeef",  # no matching collected LB
            )
        ],
        subnets=[
            {
                "SubnetId": "subnet-1",
                "VpcId": "vpc-1",
                "CidrBlock": "10.0.0.0/24",
                "AvailabilityZone": "us-east-1a",
                "Tags": [],
            }
        ],
        vpcs=[{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "IsDefault": False, "Tags": []}],
    )
    graph = build_graph(bundle)
    attached = _attached_targets(graph, "eni-ghost-nlb")
    assert len(attached) == 1
    edge = attached[0]
    assert edge.attributes == {"match_rule": "interface_type_fallback"}
    lb = graph.get_node(edge.target)
    assert lb.type == "load_balancer"
    assert lb.attributes["synthetic"] is True
    assert lb.attributes["unresolved"] is True
    assert lb.attributes["interface_type"] == "network_load_balancer"


def test_interface_type_fallback_without_description_keys_on_eni():
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-bare-gwlb", interface_type="gateway_load_balancer", description="")
        ],
    )
    graph = build_graph(bundle)
    edge = _attached_targets(graph, "eni-bare-gwlb")[0]
    assert edge.target == "unresolved-lb:eni-bare-gwlb"
    assert edge.attributes == {"match_rule": "interface_type_fallback"}


# --------------------------------------------------------------------------- #
# §5.1 / §5.2 — missing subnet / VPC become synthetic nodes
# --------------------------------------------------------------------------- #
def test_missing_subnet_becomes_synthetic_but_edge_still_drawn():
    bundle = _bundle(
        network_interfaces=[_eni("eni-x", subnet_id="subnet-missing", vpc_id="vpc-1")],
        vpcs=[{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "IsDefault": False, "Tags": []}],
    )
    graph = build_graph(bundle)
    subnet = graph.get_node("subnet-missing")
    assert subnet.attributes["synthetic"] is True
    assert len(_edges_from(graph, "eni-x", "in_subnet")) == 1
    # Synthetic subnet still gets a VPC edge, using the ENI's VpcId as the hint.
    in_vpc = [e for e in graph.edges if e.source == "subnet-missing" and e.relationship == "in_vpc"]
    assert len(in_vpc) == 1
    assert in_vpc[0].target == "vpc-1"


def test_missing_vpc_becomes_synthetic():
    bundle = _bundle(
        network_interfaces=[_eni("eni-y", subnet_id="subnet-1", vpc_id="vpc-missing")],
        subnets=[
            {
                "SubnetId": "subnet-1",
                "VpcId": "vpc-missing",
                "CidrBlock": "10.0.0.0/24",
                "AvailabilityZone": "us-east-1a",
                "Tags": [],
            }
        ],
    )
    graph = build_graph(bundle)
    vpc = graph.get_node("vpc-missing")
    assert vpc.attributes["synthetic"] is True
    assert vpc.attributes["unresolved"] is True


def test_missing_subnet_with_no_vpc_hint_gets_placeholder_vpc():
    bundle = _bundle(
        network_interfaces=[_eni("eni-z", subnet_id="subnet-orphan", vpc_id=None)],
    )
    graph = build_graph(bundle)
    in_vpc = [e for e in graph.edges if e.source == "subnet-orphan" and e.relationship == "in_vpc"]
    assert len(in_vpc) == 1
    assert in_vpc[0].target == "unknown-vpc:subnet-orphan"
    assert graph.get_node("unknown-vpc:subnet-orphan").attributes["synthetic"] is True


# --------------------------------------------------------------------------- #
# Invariants (docs/03_phase_plan.md acceptance criteria)
# --------------------------------------------------------------------------- #
def test_invariant_one_in_subnet_edge_per_eni(full_bundle):
    graph = build_graph(full_bundle)
    eni_ids = [n.id for n in graph.nodes if n.type == "eni"]
    for eni_id in eni_ids:
        assert len(_edges_from(graph, eni_id, "in_subnet")) == 1


def test_invariant_one_in_vpc_edge_per_subnet(full_bundle):
    graph = build_graph(full_bundle)
    subnet_ids = [n.id for n in graph.nodes if n.type == "subnet"]
    for subnet_id in subnet_ids:
        in_vpc = [e for e in graph.edges if e.source == subnet_id and e.relationship == "in_vpc"]
        assert len(in_vpc) == 1


def test_invariant_no_eni_attached_to_both_instance_and_lb(full_bundle):
    graph = build_graph(full_bundle)
    eni_ids = [n.id for n in graph.nodes if n.type == "eni"]
    for eni_id in eni_ids:
        assert len(_attached_targets(graph, eni_id)) <= 1


# --------------------------------------------------------------------------- #
# include_orphans — show/hide subnets & VPCs that no ENI references
# --------------------------------------------------------------------------- #
def test_orphan_vpc_hidden_by_default(full_bundle):
    # The fixtures include a default VPC (vpc-0defdefdefdefdefd) that no subnet/ENI references.
    graph = build_graph(full_bundle)
    assert graph.get_node("vpc-0defdefdefdefdefd") is None


def test_include_orphans_adds_unreferenced_vpc(full_bundle):
    graph = build_graph(full_bundle, include_orphans=True)
    orphan = graph.get_node("vpc-0defdefdefdefdefd")
    assert orphan is not None
    assert orphan.type == "vpc"
    assert orphan.attributes["is_default"] is True
    # It is a real (not synthetic) node, just isolated (no in_vpc edge points at it).
    assert "synthetic" not in orphan.attributes


def test_include_orphans_adds_unreferenced_subnet_with_vpc_edge():
    bundle = _bundle(
        network_interfaces=[_eni("eni-a", subnet_id="subnet-used", vpc_id="vpc-1")],
        subnets=[
            {
                "SubnetId": "subnet-used",
                "VpcId": "vpc-1",
                "CidrBlock": "10.0.1.0/24",
                "AvailabilityZone": "us-east-1a",
                "Tags": [],
            },
            {
                "SubnetId": "subnet-orphan",
                "VpcId": "vpc-1",
                "CidrBlock": "10.0.9.0/24",
                "AvailabilityZone": "us-east-1b",
                "Tags": [],
            },
        ],
        vpcs=[{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "IsDefault": False, "Tags": []}],
    )
    # Hidden by default.
    default_graph = build_graph(bundle)
    assert default_graph.get_node("subnet-orphan") is None

    # Shown, with its own in_vpc edge, when requested.
    graph = build_graph(bundle, include_orphans=True)
    assert graph.get_node("subnet-orphan") is not None
    in_vpc = [e for e in graph.edges if e.source == "subnet-orphan" and e.relationship == "in_vpc"]
    assert len(in_vpc) == 1
    assert in_vpc[0].target == "vpc-1"
    # The invariant still holds: every subnet node has exactly one in_vpc edge.
    for subnet_id in [n.id for n in graph.nodes if n.type == "subnet"]:
        edges = [e for e in graph.edges if e.source == subnet_id and e.relationship == "in_vpc"]
        assert len(edges) == 1


def test_orphan_instance_and_lb_hidden_by_default(full_bundle):
    # Fixtures contain an instance (i-...002) and a Classic ELB (legacy-classic-elb) that no
    # ENI references — both absent from the ENI-anchored graph.
    graph = build_graph(full_bundle)
    assert graph.get_node("i-0abc0000000000002") is None
    assert graph.get_node("legacy-classic-elb") is None


def test_include_orphans_adds_unreferenced_instance(full_bundle):
    graph = build_graph(full_bundle, include_orphans=True)
    orphan = graph.get_node("i-0abc0000000000002")
    assert orphan is not None
    assert orphan.type == "ec2_instance"
    assert orphan.attributes["state"] == "stopped"
    assert "synthetic" not in orphan.attributes  # a real, just-isolated node
    # Isolated: nothing attaches to it.
    assert [e for e in graph.edges if e.target == "i-0abc0000000000002"] == []


def test_include_orphans_adds_unreferenced_load_balancer(full_bundle):
    graph = build_graph(full_bundle, include_orphans=True)
    orphan = graph.get_node("legacy-classic-elb")
    assert orphan is not None
    assert orphan.type == "load_balancer"
    assert orphan.attributes["lb_type"] == "classic"
    assert [e for e in graph.edges if e.target == "legacy-classic-elb"] == []


def test_include_orphans_does_not_change_eni_anchored_edges(full_bundle):
    # Orphans only add isolated nodes/edges; the ENI-anchored core is unchanged.
    base = build_graph(full_bundle).to_dict()
    withorphans = build_graph(full_bundle, include_orphans=True).to_dict()
    assert base["edges"] == [e for e in withorphans["edges"] if e in base["edges"]]
    base_ids = {n["id"] for n in base["nodes"]}
    assert base_ids.issubset({n["id"] for n in withorphans["nodes"]})


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_build_is_deterministic(full_bundle):
    first = build_graph(full_bundle).to_dict()
    second = build_graph(full_bundle).to_dict()
    assert first == second
    # Node/edge ordering is stable and sorted.
    assert first["nodes"] == sorted(first["nodes"], key=lambda n: (n["type"], n["id"]))


def test_meta_is_passed_through_with_tool_version(full_bundle):
    graph = build_graph(full_bundle)
    assert graph.meta["region"] == "us-east-1"
    assert graph.meta["accounts"] == {"network": "111111111111"}
    assert "tool_version" in graph.meta
