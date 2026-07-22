"""Tests for the Phase 2 domain models (``model/resources.py``).

Each ``from_collected`` consumes the Phase 1 normalized dict shape
(``docs/learnings/learnings_phase1.md §2b``).
"""

from __future__ import annotations

from conftest import load_fixture

from cloudbreachgraph.aws import collectors
from cloudbreachgraph.model.resources import (
    ClassicLoadBalancer,
    Ec2Instance,
    Elbv2LoadBalancer,
    Eni,
    SecurityGroup,
    SgIngressRule,
    Subnet,
    Vpc,
)


def _normalized(name: str, key: str, normalize):
    return [normalize(x) for x in load_fixture(name)[key]]


def test_eni_from_collected_instance_attached():
    raw = collectors._normalize_eni(
        load_fixture("ec2_describe-network-interfaces.json")["NetworkInterfaces"][0]
    )
    eni = Eni.from_collected(raw)
    assert eni.id == "eni-00instance0000001"
    assert eni.subnet_id == "subnet-011111111111111"
    assert eni.vpc_id == "vpc-0aaaaaaaaaaaaaaaa"
    assert eni.interface_type == "interface"
    assert eni.attachment_instance_id == "i-0abc0000000000001"
    assert eni.private_ips == ["10.0.1.10"]
    # Public IP is de-duplicated across the interface-level and per-address Associations.
    assert eni.public_ips == ["54.10.20.30"]
    assert eni.security_groups == ["sg-0aaa0001"]


def test_eni_from_collected_service_managed_has_no_instance():
    raw = collectors._normalize_eni(
        load_fixture("ec2_describe-network-interfaces.json")["NetworkInterfaces"][1]
    )
    eni = Eni.from_collected(raw)
    # Service-managed ELB ENI: no InstanceId, description carries the ELB token.
    assert eni.attachment_instance_id is None
    assert eni.description == "ELB app/my-alb/50dc6c495c0c9188"
    # No Association block -> no public IP.
    assert eni.public_ips == []


def test_ec2_instance_from_collected_uses_name_tag():
    raw = collectors._normalize_instance(
        load_fixture("ec2_describe-instances.json")["Reservations"][0]["Instances"][0]
    )
    inst = Ec2Instance.from_collected(raw)
    assert inst.id == "i-0abc0000000000001"
    assert inst.state == "running"
    assert inst.name == "web-server-1"


def test_ec2_instance_without_name_tag_has_none():
    raw = collectors._normalize_instance(
        load_fixture("ec2_describe-instances.json")["Reservations"][1]["Instances"][0]
    )
    inst = Ec2Instance.from_collected(raw)
    assert inst.name is None
    assert inst.state == "stopped"


def test_elbv2_loadbalancer_from_collected_token():
    lbs = _normalized(
        "elbv2_describe-load-balancers.json", "LoadBalancers", collectors._normalize_elbv2
    )
    alb = Elbv2LoadBalancer.from_collected(next(x for x in lbs if x["Type"] == "application"))
    assert alb.node_id == alb.arn
    assert alb.lb_type == "application"
    assert alb.elb_token == "app/my-alb/50dc6c495c0c9188"


def test_classic_loadbalancer_from_collected_uses_odd_vpcid_key():
    raw = collectors._normalize_classic_elb(
        load_fixture("elb_describe-load-balancers.json")["LoadBalancerDescriptions"][0]
    )
    lb = ClassicLoadBalancer.from_collected(raw)
    assert lb.node_id == "legacy-classic-elb"
    assert lb.lb_type == "classic"
    assert lb.vpc_id == "vpc-0aaaaaaaaaaaaaaaa"  # sourced from Classic's "VPCId" spelling
    # Classic ELBs have no ARN token; they're matched by name instead.
    assert not hasattr(lb, "elb_token")


def test_subnet_and_vpc_from_collected():
    subnet = Subnet.from_collected(
        collectors._normalize_subnet(load_fixture("ec2_describe-subnets.json")["Subnets"][0])
    )
    assert subnet.id == "subnet-011111111111111"
    assert subnet.vpc_id == "vpc-0aaaaaaaaaaaaaaaa"
    assert subnet.name == "public-1a"

    vpc = Vpc.from_collected(
        collectors._normalize_vpc(load_fixture("ec2_describe-vpcs.json")["Vpcs"][1])
    )
    assert vpc.id == "vpc-0defdefdefdefdefd"
    assert vpc.is_default is True
    assert vpc.name is None  # no Name tag


def test_security_group_from_collected_parses_ingress():
    raw = collectors._normalize_security_group(
        load_fixture("ec2_describe-security-groups.json")["SecurityGroups"][0]
    )
    sg = SecurityGroup.from_collected(raw)
    assert sg.id == "sg-0aaa0001"
    assert sg.name == "web"
    assert sg.vpc_id == "vpc-0aaaaaaaaaaaaaaaa"
    assert len(sg.ingress) == 3

    https = sg.ingress[0]
    assert https.cidrs == ["0.0.0.0/0"]
    assert https.port_label() == "tcp/443"

    peer = sg.ingress[2]  # the -1 (all traffic) peer-SG rule
    assert peer.referenced_group_ids == ["sg-0aaa0002"]
    assert peer.port_label() == "all"


def test_sg_ingress_rule_port_labels():
    assert SgIngressRule("tcp", 443, 443).port_label() == "tcp/443"
    assert SgIngressRule("tcp", 8000, 8100).port_label() == "tcp/8000-8100"
    assert SgIngressRule("-1", None, None).port_label() == "all"
    assert SgIngressRule("icmp", None, None).port_label() == "icmp"
