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
    ("ec2", "describe-security-groups"): "ec2_describe-security-groups.json",
    ("ec2", "describe-route-tables"): "ec2_describe-route-tables.json",
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
    groups=None,
    public_ip=None,
    private_ip=None,
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
        "Association": {"PublicIp": public_ip},
        "PrivateIpAddresses": [{"PrivateIpAddress": private_ip}] if private_ip else [],
        "Groups": [{"GroupId": g} for g in (groups or [])],
    }


def _subnet(subnet_id, *, vpc_id="vpc-1", cidr="10.0.0.0/16"):
    return {
        "SubnetId": subnet_id,
        "VpcId": vpc_id,
        "CidrBlock": cidr,
        "AvailabilityZone": "us-east-1a",
        "Tags": [],
    }


def _vpc_dict(vpc_id="vpc-1", cidr="10.0.0.0/16"):
    return {"VpcId": vpc_id, "CidrBlock": cidr, "IsDefault": False, "Tags": []}


def _rt(rt_id, *, vpc_id="vpc-1", main=False, subnet_ids=(), routes=()):
    """A normalized route-table dict. Each ``routes`` entry is ``(dest_cidr, target[, state])``."""
    norm = []
    for r in routes:
        norm.append(
            {
                "DestinationCidrBlock": r[0],
                "DestinationIpv6CidrBlock": None,
                "Target": r[1],
                "State": r[2] if len(r) > 2 else "active",
            }
        )
    return {
        "RouteTableId": rt_id,
        "VpcId": vpc_id,
        "Main": main,
        "SubnetIds": list(subnet_ids),
        "Routes": norm,
    }


def _sg(group_id, *, name=None, vpc_id="vpc-1", ingress=()):
    """A normalized security-group dict. Each ``ingress`` entry is
    ``(protocol, from_port, to_port, cidrs, ipv6_cidrs, group_ids)`` (trailing items optional)."""
    perms = []
    for rule in ingress:
        proto, fromp, top = rule[0], rule[1], rule[2]
        cidrs = rule[3] if len(rule) > 3 else []
        ipv6 = rule[4] if len(rule) > 4 else []
        gids = rule[5] if len(rule) > 5 else []
        perms.append(
            {
                "IpProtocol": proto,
                "FromPort": fromp,
                "ToPort": top,
                "IpRanges": [{"CidrIp": c} for c in cidrs],
                "Ipv6Ranges": [{"CidrIpv6": c} for c in ipv6],
                "UserIdGroupPairs": [{"GroupId": g} for g in gids],
            }
        )
    return {
        "GroupId": group_id,
        "GroupName": name,
        "VpcId": vpc_id,
        "Description": None,
        "IpPermissions": perms,
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
# §5.5 — reachability, security groups SHOWN (the default)
# --------------------------------------------------------------------------- #
def _collapsed(bundle, **kw):
    """build_graph with security groups hidden (the --no-security-groups view)."""
    return build_graph(bundle, show_security_groups=False, **kw)


def _reach_edges(graph, eni_id):
    # Matches every reachability relationship (can_reach / routable_ / not_routable_can_reach).
    return [e for e in graph.edges if e.target == eni_id and e.relationship.endswith("can_reach")]


def _secured_by(graph, eni_id):
    return [e.target for e in graph.edges if e.source == eni_id and e.relationship == "secured_by"]


def test_shown_eni_links_to_its_security_group():
    bundle = _bundle(
        network_interfaces=[_eni("eni-web", groups=["sg-web"])],
        security_groups=[_sg("sg-web", name="web", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
    )
    graph = build_graph(bundle)  # default: SGs shown
    sg = graph.get_node("sg-web")
    assert sg is not None and sg.type == "security_group" and sg.label == "web"
    assert _secured_by(graph, "eni-web") == ["sg-web"]


def test_shown_sources_connect_to_the_sg_not_the_eni():
    bundle = _bundle(
        network_interfaces=[_eni("eni-web", groups=["sg-web"])],
        security_groups=[
            _sg(
                "sg-web",
                ingress=[("tcp", 443, 443, ["0.0.0.0/0"]), ("tcp", 22, 22, ["203.0.113.0/24"])],
            )
        ],
    )
    graph = build_graph(bundle)
    # A per-SG Internet node and a shared CIDR node, both pointing at the SG (not the ENI).
    inet = graph.get_node("internet:sg-web")
    assert inet is not None and inet.type == "internet"
    edge = next(e for e in graph.edges if e.source == "internet:sg-web")
    assert edge.target == "sg-web" and edge.relationship == "can_reach"
    assert edge.attributes["ports"] == "tcp/443"
    assert next(e for e in graph.edges if e.source == "cidr:203.0.113.0/24").target == "sg-web"
    # No direct source -> ENI edges in the shown view.
    assert _reach_edges(graph, "eni-web") == []


def test_shown_internet_nodes_are_per_sg_not_shared():
    # Two ENIs sharing an SG share ONE per-SG Internet node (the SG collapses the fan-out).
    bundle = _bundle(
        network_interfaces=[_eni("eni-a", groups=["sg-x"]), _eni("eni-b", groups=["sg-x"])],
        security_groups=[_sg("sg-x", ingress=[("tcp", 80, 80, ["0.0.0.0/0"])])],
    )
    graph = build_graph(bundle)
    assert graph.get_node("internet:sg-x") is not None
    assert graph.get_node("internet") is None  # no single shared Internet node
    assert {e.source for e in graph.edges if e.relationship == "secured_by"} == {"eni-a", "eni-b"}


def test_shown_peer_reference_is_an_sg_to_sg_edge():
    bundle = _bundle(
        network_interfaces=[_eni("eni-app", groups=["sg-app"]), _eni("eni-lb", groups=["sg-lb"])],
        security_groups=[
            _sg("sg-app", ingress=[("-1", None, None, [], [], ["sg-lb"])]),
            _sg("sg-lb", name="alb-sg"),
        ],
    )
    graph = build_graph(bundle)
    # sg-lb is both a member SG (of eni-lb) and a *source* of sg-app -> one SG node, SG->SG edge.
    peer = graph.get_node("sg-lb")
    assert peer is not None and peer.label == "alb-sg"
    assert any(
        e.source == "sg-lb" and e.target == "sg-app" and e.relationship == "can_reach"
        for e in graph.edges
    )


def test_shown_view_has_no_routability_split():
    # Routability is per-ENI, so an SG-level edge is plain can_reach even with route data present.
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-1", subnet_id="subnet-1", groups=["sg-1"], public_ip="52.1.2.3")
        ],
        subnets=[_subnet("subnet-1")],
        vpcs=[_vpc_dict()],
        security_groups=[_sg("sg-1", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
        route_tables=[_rt("rtb-1", subnet_ids=["subnet-1"], routes=[("0.0.0.0/0", "igw-1")])],
    )
    graph = build_graph(bundle)
    rels = {e.relationship for e in graph.edges if e.relationship.endswith("can_reach")}
    assert rels == {"can_reach"}


def test_shown_reachability_from_full_fixture(full_bundle):
    graph = build_graph(full_bundle)
    assert graph.get_node("sg-0aaa0001") is not None
    assert graph.get_node("sg-0aaa0002") is not None
    assert graph.get_node("internet:sg-0aaa0001") is not None
    assert graph.get_node("cidr:203.0.113.0/24") is not None
    # membership + the peer SG->SG edge.
    assert "sg-0aaa0001" in _secured_by(graph, "eni-00instance0000001")
    assert any(e.source == "sg-0aaa0002" and e.target == "sg-0aaa0001" for e in graph.edges)


def test_eni_without_security_groups_has_no_reachability():
    bundle = _bundle(network_interfaces=[_eni("eni-none")])  # no groups, either mode
    assert build_graph(bundle).get_node("sg-none") is None
    assert _reach_edges(_collapsed(bundle), "eni-none") == []
    assert _secured_by(build_graph(bundle), "eni-none") == []


def test_missing_security_group_is_skipped_not_synthesized():
    # An ENI references an SG we didn't collect -> a bare SG node (shown) but no invented sources.
    bundle = _bundle(network_interfaces=[_eni("eni-x", groups=["sg-uncollected"])])
    shown = build_graph(bundle)
    assert shown.get_node("sg-uncollected") is not None  # membership still shown
    assert not [
        e for e in shown.edges if e.target == "sg-uncollected" and e.relationship == "can_reach"
    ]
    # Hidden: nothing to bring forward.
    assert _reach_edges(_collapsed(bundle), "eni-x") == []


# --------------------------------------------------------------------------- #
# §5.5 — reachability, security groups HIDDEN (--no-security-groups)
# --------------------------------------------------------------------------- #
def test_hidden_full_internet_exposure_creates_per_eni_internet_node():
    bundle = _bundle(
        network_interfaces=[_eni("eni-web", groups=["sg-web"])],
        security_groups=[_sg("sg-web", name="web", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
    )
    graph = _collapsed(bundle)
    node = graph.get_node("internet:eni-web")
    assert node is not None and node.type == "internet" and node.label == "Internet"
    assert graph.get_node("sg-web") is None  # no SG node in the hidden view
    edges = _reach_edges(graph, "eni-web")
    assert len(edges) == 1 and edges[0].source == "internet:eni-web"
    assert edges[0].attributes["ports"] == "tcp/443"


def test_hidden_internet_nodes_are_not_shared_between_enis():
    bundle = _bundle(
        network_interfaces=[_eni("eni-a", groups=["sg-x"]), _eni("eni-b", groups=["sg-x"])],
        security_groups=[_sg("sg-x", ingress=[("tcp", 80, 80, ["0.0.0.0/0"])])],
    )
    graph = _collapsed(bundle)
    assert graph.get_node("internet:eni-a") is not None
    assert graph.get_node("internet:eni-b") is not None
    assert graph.get_node("internet") is None


def test_hidden_ipv6_full_range_also_maps_to_internet():
    bundle = _bundle(
        network_interfaces=[_eni("eni-6", groups=["sg-6"])],
        security_groups=[_sg("sg-6", ingress=[("tcp", 443, 443, [], ["::/0"])])],
    )
    assert _collapsed(bundle).get_node("internet:eni-6") is not None


def test_hidden_specific_cidr_creates_shared_cidr_node():
    bundle = _bundle(
        network_interfaces=[_eni("eni-a", groups=["sg-a"]), _eni("eni-b", groups=["sg-a"])],
        security_groups=[_sg("sg-a", ingress=[("tcp", 22, 22, ["203.0.113.0/24"])])],
    )
    graph = _collapsed(bundle)
    node = graph.get_node("cidr:203.0.113.0/24")
    assert node is not None and node.type == "cidr"
    targets = {e.target for e in graph.edges if e.source == "cidr:203.0.113.0/24"}
    assert targets == {"eni-a", "eni-b"}


def test_hidden_peer_reference_expands_to_member_private_ips():
    # The point of the flag: a peer-SG rule is expanded to the private IPs *behind* that SG.
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-app", groups=["sg-app"], private_ip="10.0.0.5"),
            _eni("eni-lb", groups=["sg-lb"], private_ip="10.0.0.9"),
        ],
        security_groups=[
            _sg("sg-app", ingress=[("-1", None, None, [], [], ["sg-lb"])]),
            _sg("sg-lb", name="alb-sg"),
        ],
    )
    graph = _collapsed(bundle)
    node = graph.get_node("cidr:10.0.0.9/32")  # eni-lb's private IP, brought forward
    assert node is not None and node.type == "cidr"
    edge = next(e for e in _reach_edges(graph, "eni-app"))
    assert edge.source == "cidr:10.0.0.9/32" and edge.attributes["ports"] == "all"
    assert not [n for n in graph.nodes if n.type == "security_group"]  # no SG nodes


def test_hidden_multiple_ports_from_same_source_aggregate_onto_one_edge():
    bundle = _bundle(
        network_interfaces=[_eni("eni-m", groups=["sg-m"])],
        security_groups=[
            _sg("sg-m", ingress=[("tcp", 443, 443, ["0.0.0.0/0"]), ("tcp", 80, 80, ["0.0.0.0/0"])])
        ],
    )
    edges = _reach_edges(_collapsed(bundle), "eni-m")
    assert len(edges) == 1 and edges[0].attributes["ports"] == "tcp/443, tcp/80"


def test_reachability_is_included_regardless_of_orphans_flag():
    bundle = _bundle(
        network_interfaces=[_eni("eni-web", groups=["sg-web"])],
        security_groups=[_sg("sg-web", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
    )
    assert build_graph(bundle).get_node("sg-web") is not None
    assert build_graph(bundle, include_orphans=True).get_node("sg-web") is not None


def test_hidden_reachability_from_full_fixture_bundle(full_bundle):
    graph = _collapsed(full_bundle)
    assert graph.get_node("internet:eni-00instance0000001") is not None
    assert graph.get_node("internet:eni-00alb00000000002") is not None
    assert graph.get_node("cidr:203.0.113.0/24") is not None
    assert not [n for n in graph.nodes if n.type == "security_group"]
    # The NAT-gateway ENI has no security groups -> no reachability edges.
    assert _reach_edges(graph, "eni-00natgw000000004") == []


# --------------------------------------------------------------------------- #
# §5.6 — routability (routable_can_reach / not_routable_can_reach)
# --------------------------------------------------------------------------- #
def _rel(graph, source, eni_id):
    return next(e.relationship for e in graph.edges if e.source == source and e.target == eni_id)


def _routing_bundle(*, public_subnet, public_ip, source):
    # One ENI with a 0.0.0.0/0 (or CIDR/SG) inbound rule, in a public or private subnet.
    subnet_id = "subnet-1"
    if source == "internet":
        ingress = [("tcp", 443, 443, ["0.0.0.0/0"])]
    elif source == "external_cidr":
        ingress = [("tcp", 443, 443, ["203.0.113.0/24"])]
    elif source == "internal_cidr":
        ingress = [("tcp", 443, 443, ["10.0.5.0/24"])]
    else:  # peer security group
        ingress = [("-1", None, None, [], [], ["sg-peer"])]
    default_route = ("0.0.0.0/0", "igw-1") if public_subnet else ("0.0.0.0/0", "nat-1")
    return _bundle(
        network_interfaces=[
            _eni("eni-1", subnet_id=subnet_id, groups=["sg-1"], public_ip=public_ip)
        ],
        subnets=[_subnet(subnet_id)],
        vpcs=[_vpc_dict()],
        security_groups=[_sg("sg-1", ingress=ingress), _sg("sg-peer", name="peer")],
        route_tables=[
            _rt(
                "rtb-1",
                subnet_ids=[subnet_id],
                routes=[("10.0.0.0/16", "local"), default_route],
            )
        ],
    )


def test_internet_source_in_public_subnet_with_public_ip_is_routable():
    g = _collapsed(_routing_bundle(public_subnet=True, public_ip="52.1.2.3", source="internet"))
    assert _rel(g, "internet:eni-1", "eni-1") == "routable_can_reach"


def test_internet_source_in_private_subnet_is_not_routable():
    g = _collapsed(_routing_bundle(public_subnet=False, public_ip="52.1.2.3", source="internet"))
    assert _rel(g, "internet:eni-1", "eni-1") == "not_routable_can_reach"


def test_internet_source_without_public_ip_is_not_routable():
    # Public subnet (igw default route) but the ENI has no public IP -> not addressable.
    g = _collapsed(_routing_bundle(public_subnet=True, public_ip=None, source="internet"))
    assert _rel(g, "internet:eni-1", "eni-1") == "not_routable_can_reach"


def test_external_cidr_needs_internet_path():
    routable = _collapsed(
        _routing_bundle(public_subnet=True, public_ip="52.1.2.3", source="external_cidr")
    )
    assert _rel(routable, "cidr:203.0.113.0/24", "eni-1") == "routable_can_reach"
    not_routable = _collapsed(
        _routing_bundle(public_subnet=False, public_ip=None, source="external_cidr")
    )
    assert _rel(not_routable, "cidr:203.0.113.0/24", "eni-1") == "not_routable_can_reach"


def test_intra_vpc_cidr_is_routable_via_local_route():
    # A source inside the VPC is always reachable (local route), even in a private subnet.
    g = _collapsed(_routing_bundle(public_subnet=False, public_ip=None, source="internal_cidr"))
    assert _rel(g, "cidr:10.0.5.0/24", "eni-1") == "routable_can_reach"


def test_peer_security_group_member_ip_is_routable():
    # Hidden view: a peer-SG rule expands to the peer's member private IPs (in-VPC -> routable).
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-1", subnet_id="subnet-1", groups=["sg-1"], public_ip=None),
            _eni("eni-peer", subnet_id="subnet-1", groups=["sg-peer"], private_ip="10.0.9.9"),
        ],
        subnets=[_subnet("subnet-1")],
        vpcs=[_vpc_dict()],
        security_groups=[
            _sg("sg-1", ingress=[("-1", None, None, [], [], ["sg-peer"])]),
            _sg("sg-peer", name="peer"),
        ],
        route_tables=[_rt("rtb-1", subnet_ids=["subnet-1"], routes=[("10.0.0.0/16", "local")])],
    )
    g = _collapsed(bundle)
    assert _rel(g, "cidr:10.0.9.9/32", "eni-1") == "routable_can_reach"


def test_external_cidr_routable_over_transit_gateway():
    # A corporate range reachable via a transit-gateway route, no internet path needed.
    bundle = _bundle(
        network_interfaces=[_eni("eni-1", subnet_id="subnet-1", groups=["sg-1"])],
        subnets=[_subnet("subnet-1")],
        vpcs=[_vpc_dict()],
        security_groups=[_sg("sg-1", ingress=[("tcp", 22, 22, ["192.0.2.0/24"])])],
        route_tables=[
            _rt(
                "rtb-1",
                subnet_ids=["subnet-1"],
                routes=[("10.0.0.0/16", "local"), ("192.0.2.0/24", "tgw-1")],
            )
        ],
    )
    g = _collapsed(bundle)
    assert _rel(g, "cidr:192.0.2.0/24", "eni-1") == "routable_can_reach"


def test_main_route_table_is_the_fallback_for_unassociated_subnets():
    # subnet-1 has no explicit RT association; the VPC main RT (public) applies.
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-1", subnet_id="subnet-1", groups=["sg-1"], public_ip="52.1.2.3")
        ],
        subnets=[_subnet("subnet-1")],
        vpcs=[_vpc_dict()],
        security_groups=[_sg("sg-1", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
        route_tables=[
            _rt("rtb-main", main=True, routes=[("10.0.0.0/16", "local"), ("0.0.0.0/0", "igw-1")])
        ],
    )
    g = _collapsed(bundle)
    assert _rel(g, "internet:eni-1", "eni-1") == "routable_can_reach"


def test_no_route_data_leaves_reachability_undetermined():
    # Without route tables the builder can't decide routability -> plain can_reach.
    bundle = _bundle(
        network_interfaces=[_eni("eni-1", groups=["sg-1"], public_ip="52.1.2.3")],
        security_groups=[_sg("sg-1", ingress=[("tcp", 443, 443, ["0.0.0.0/0"])])],
    )
    g = _collapsed(bundle)
    assert _rel(g, "internet:eni-1", "eni-1") == "can_reach"


def test_ports_still_aggregate_onto_the_routed_edge():
    bundle = _bundle(
        network_interfaces=[
            _eni("eni-1", subnet_id="subnet-1", groups=["sg-1"], public_ip="52.1.2.3")
        ],
        subnets=[_subnet("subnet-1")],
        vpcs=[_vpc_dict()],
        security_groups=[
            _sg("sg-1", ingress=[("tcp", 443, 443, ["0.0.0.0/0"]), ("tcp", 80, 80, ["0.0.0.0/0"])])
        ],
        route_tables=[_rt("rtb-1", subnet_ids=["subnet-1"], routes=[("0.0.0.0/0", "igw-1")])],
    )
    g = _collapsed(bundle)
    edge = next(e for e in g.edges if e.source == "internet:eni-1")
    assert edge.relationship == "routable_can_reach"
    assert edge.attributes["ports"] == "tcp/443, tcp/80"


def test_full_fixture_routability_splits_reachability(full_bundle):
    # eni-00instance is in a public subnet WITH a public IP -> routable; eni-00alb is in the same
    # public subnet but has NO public IP -> not routable.
    graph = _collapsed(full_bundle)
    inst = _rel(graph, "internet:eni-00instance0000001", "eni-00instance0000001")
    alb = _rel(graph, "internet:eni-00alb00000000002", "eni-00alb00000000002")
    assert inst == "routable_can_reach"
    assert alb == "not_routable_can_reach"
    # The bastion CIDR reaches the public instance ENI over the internet path -> routable.
    assert _rel(graph, "cidr:203.0.113.0/24", "eni-00instance0000001") == "routable_can_reach"


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
