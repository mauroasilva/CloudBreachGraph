"""Tests for the collectors, the role registry, and the collect_all driver.

The mock boundary is ``runner.run_aws`` — no subprocess, no network.
"""

from __future__ import annotations

import pytest
from conftest import load_fixture

from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget

# Map the AWS sub-command (first two args) to its recorded fixture file.
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
def fake_aws(monkeypatch):
    """Patch runner.run_aws to serve fixtures and record every (args, profile, region)."""
    calls: list[dict] = []

    def _run(args, *, profile=None, region=None, cache_dir=None):
        calls.append({"args": args, "profile": profile, "region": region})
        key = tuple(args[:2])
        return load_fixture(_COMMAND_FIXTURES[key])

    monkeypatch.setattr(runner, "run_aws", _run)
    return calls


def test_collect_network_interfaces_normalizes(fake_aws):
    enis = collectors.collect_network_interfaces("prod-audit", "us-east-1")
    assert [e["NetworkInterfaceId"] for e in enis] == [
        "eni-00instance0000001",
        "eni-00alb00000000002",
        "eni-00nlb00000000003",
        "eni-00natgw000000004",
    ]

    instance_eni = enis[0]
    assert instance_eni["Attachment"]["InstanceId"] == "i-0abc0000000000001"
    assert instance_eni["SubnetId"] == "subnet-011111111111111"
    assert instance_eni["VpcId"] == "vpc-0aaaaaaaaaaaaaaaa"

    alb_eni = enis[1]
    # Service-managed ELB ENI: no InstanceId, description carries the LB token.
    assert alb_eni["Attachment"]["InstanceId"] is None
    assert alb_eni["Description"] == "ELB app/my-alb/50dc6c495c0c9188"
    assert alb_eni["InterfaceType"] == "interface"

    nlb_eni = enis[2]
    assert nlb_eni["InterfaceType"] == "network_load_balancer"

    # The runner was invoked with the threaded-through profile/region.
    assert fake_aws[0]["profile"] == "prod-audit"
    assert fake_aws[0]["region"] == "us-east-1"


def test_collect_ec2_instances_flattens_reservations(fake_aws):
    instances = collectors.collect_ec2_instances(None, None)
    # Two reservations, one instance each -> flat list of two.
    assert [i["InstanceId"] for i in instances] == [
        "i-0abc0000000000001",
        "i-0abc0000000000002",
    ]
    assert instances[0]["State"]["Name"] == "running"
    assert instances[0]["Tags"] == [{"Key": "Name", "Value": "web-server-1"}]


def test_collect_load_balancers_v2_normalizes(fake_aws):
    lbs = collectors.collect_load_balancers_v2(None, None)
    assert {lb["LoadBalancerName"] for lb in lbs} == {"my-alb", "my-nlb"}
    alb = next(lb for lb in lbs if lb["Type"] == "application")
    assert alb["LoadBalancerArn"].endswith("loadbalancer/app/my-alb/50dc6c495c0c9188")


def test_collect_load_balancers_classic_normalizes(fake_aws):
    lbs = collectors.collect_load_balancers_classic(None, None)
    assert lbs[0]["LoadBalancerName"] == "legacy-classic-elb"
    # Classic ELB uses the odd "VPCId" spelling — preserved.
    assert lbs[0]["VPCId"] == "vpc-0aaaaaaaaaaaaaaaa"


def test_collect_load_balancers_classic_handles_empty(monkeypatch):
    monkeypatch.setattr(
        runner, "run_aws", lambda *a, **k: load_fixture("elb_describe-load-balancers.empty.json")
    )
    assert collectors.collect_load_balancers_classic(None, None) == []


def test_collect_subnets_and_vpcs(fake_aws):
    subnets = collectors.collect_subnets(None, None)
    assert {s["SubnetId"] for s in subnets} == {
        "subnet-011111111111111",
        "subnet-022222222222222",
    }
    vpcs = collectors.collect_vpcs(None, None)
    default_vpc = next(v for v in vpcs if v["IsDefault"])
    assert default_vpc["VpcId"] == "vpc-0defdefdefdefdefd"


def test_collect_security_groups_normalizes(fake_aws):
    sgs = collectors.collect_security_groups("prod-audit", "us-east-1")
    by_id = {s["GroupId"]: s for s in sgs}
    assert set(by_id) == {"sg-0aaa0001", "sg-0aaa0002"}

    web = by_id["sg-0aaa0001"]
    assert web["GroupName"] == "web"
    # Only ingress (IpPermissions) is kept; egress is dropped.
    assert "IpPermissionsEgress" not in web
    # The 0.0.0.0/0 HTTPS rule, the bastion CIDR rule, and the peer-SG rule are all present.
    protos = {
        (p["FromPort"], tuple(r["CidrIp"] for r in p["IpRanges"])) for p in web["IpPermissions"]
    }
    assert (443, ("0.0.0.0/0",)) in protos
    assert (22, ("203.0.113.0/24",)) in protos
    peer_rule = next(p for p in web["IpPermissions"] if p["UserIdGroupPairs"])
    assert peer_rule["UserIdGroupPairs"][0]["GroupId"] == "sg-0aaa0002"


def test_collect_route_tables_normalizes(fake_aws):
    rts = collectors.collect_route_tables("prod-audit", "us-east-1")
    by_id = {r["RouteTableId"]: r for r in rts}
    assert set(by_id) == {"rtb-0public00000001", "rtb-0private0000002", "rtb-0main0000000003"}

    public = by_id["rtb-0public00000001"]
    assert public["Main"] is False
    assert public["SubnetIds"] == ["subnet-011111111111111"]
    # The default route's target is collapsed to the igw id.
    default = next(r for r in public["Routes"] if r["DestinationCidrBlock"] == "0.0.0.0/0")
    assert default["Target"] == "igw-0abc00000000001"

    # The main route table is flagged.
    assert by_id["rtb-0main0000000003"]["Main"] is True
    # The private RT's default route points at a NAT gateway, not an igw.
    private = by_id["rtb-0private0000002"]
    priv_default = next(r for r in private["Routes"] if r["DestinationCidrBlock"] == "0.0.0.0/0")
    assert priv_default["Target"] == "nat-0abc00000000005"


def test_role_registry_is_consistent():
    # Each role's collectors and result keys line up 1:1.
    for role, funcs in collectors.ROLE_COLLECTORS.items():
        assert len(funcs) == len(collectors.ROLE_RESULT_KEYS[role])
    assert collectors.ROLE_RESULT_KEYS["network"] == [
        "network_interfaces",
        "ec2_instances",
        "load_balancers_v2",
        "load_balancers_classic",
        "subnets",
        "vpcs",
        "security_groups",
        "route_tables",
    ]


def test_collect_all_bundle_shape_and_provenance(fake_aws):
    resolved = ResolvedTarget(
        target="prod",
        roles={
            "network": ResolvedAccount(
                profile="prod-audit", account_id="111111111111", region="us-east-1"
            )
        },
    )
    bundle = collectors.collect_all(resolved)

    assert set(bundle) == {
        "meta",
        "network_interfaces",
        "ec2_instances",
        "load_balancers_v2",
        "load_balancers_classic",
        "subnets",
        "vpcs",
        "security_groups",
        "route_tables",
    }
    assert bundle["meta"] == {
        "target": "prod",
        "region": "us-east-1",
        "accounts": {"network": "111111111111"},
    }
    assert len(bundle["network_interfaces"]) == 4
    assert len(bundle["ec2_instances"]) == 2

    # Every network collector ran with the network role's resolved profile/region.
    assert all(c["profile"] == "prod-audit" for c in fake_aws)
    assert all(c["region"] == "us-east-1" for c in fake_aws)
