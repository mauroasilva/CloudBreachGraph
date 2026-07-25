"""Tests for the collectors, the role registry, and the collect_all driver.

The mock boundary is ``runner.run_aws`` — no subprocess, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    ("ec2", "describe-nat-gateways"): "ec2_describe-nat-gateways.json",
    ("ec2", "describe-vpc-endpoints"): "ec2_describe-vpc-endpoints.json",
    ("ec2", "describe-flow-logs"): "ec2_describe-flow-logs.json",
    ("cloudtrail", "lookup-events"): "cloudtrail_lookup-events.json",
    ("logs", "filter-log-events"): "logs_filter-log-events.json",
    ("s3api", "list-objects-v2"): "s3api_list-objects-v2.json",
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
        "eni-00vpce00000000006",
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


def test_collect_nat_gateways_normalizes(fake_aws):
    nats = collectors.collect_nat_gateways("prod-audit", "us-east-1")
    assert len(nats) == 1
    nat = nats[0]
    assert nat["NatGatewayId"] == "nat-0abc00000000005"
    assert nat["VpcId"] == "vpc-0aaaaaaaaaaaaaaaa"
    assert nat["SubnetId"] == "subnet-022222222222222"
    # The address block carries the authoritative ENI id (the ownership signal) + the public IP.
    addr = nat["NatGatewayAddresses"][0]
    assert addr["NetworkInterfaceId"] == "eni-00natgw000000004"
    assert addr["PublicIp"] == "34.201.10.20"


def test_collect_vpc_endpoints_normalizes(fake_aws):
    endpoints = collectors.collect_vpc_endpoints("prod-audit", "us-east-1")
    assert len(endpoints) == 1
    vpce = endpoints[0]
    assert vpce["VpcEndpointId"] == "vpce-0abc00000000006"
    assert vpce["VpcEndpointType"] == "Interface"
    # The interface endpoint's ENIs are its authoritative ownership signal.
    assert vpce["NetworkInterfaceIds"] == ["eni-00vpce00000000006"]
    assert vpce["ServiceName"].endswith("secretsmanager")


# --------------------------------------------------------------------------- #
# flow_logs role (§5.7)
# --------------------------------------------------------------------------- #
def test_collect_flow_logs_normalizes(fake_aws):
    fls = collectors.collect_flow_logs("prod-audit", "us-east-1")
    by_id = {f["FlowLogId"]: f for f in fls}
    assert set(by_id) == {"fl-0abc00000000001", "fl-0abc00000000002"}
    cw = by_id["fl-0abc00000000001"]
    assert cw["ResourceId"] == "vpc-0aaaaaaaaaaaaaaaa"
    assert cw["LogDestinationType"] == "cloud-watch-logs"
    assert cw["LogGroupName"] == "/vpc/flowlogs/prod"
    s3 = by_id["fl-0abc00000000002"]
    assert s3["LogDestinationType"] == "s3"
    assert s3["LogDestination"].startswith("arn:aws:s3:::")


def test_collect_ip_allocation_events_parses_cloudtrail(fake_aws):
    allocs = collectors.collect_ip_allocation_events("prod-audit", "us-east-1")
    by_eni = {a["NetworkInterfaceId"]: a for a in allocs}
    assert set(by_eni) == {"eni-00instance0000001", "eni-00alb00000000002"}
    inst = by_eni["eni-00instance0000001"]
    assert inst["PrivateIpAddress"] == "10.0.1.10"
    assert inst["AllocatedAt"].startswith("2026-06-01")
    # The lookup was scoped to CreateNetworkInterface via --lookup-attributes.
    call = next(c for c in fake_aws if tuple(c["args"][:2]) == ("cloudtrail", "lookup-events"))
    assert any("CreateNetworkInterface" in a for a in call["args"])
    # The lookback is set explicitly and aligned to the 60-day flow-log window.
    start_arg = next(a for a in call["args"] if a.startswith("--start-time="))
    start = datetime.strptime(start_arg.split("=", 1)[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    window_days = (datetime.now(UTC) - start).total_seconds() / 86400
    assert abs(window_days - collectors.FLOW_LOG_MAX_LOOKBACK_DAYS) < 1


def test_collect_flow_log_records_parses_and_skips_nodata(fake_aws):
    records = collectors.collect_flow_log_records("prod-audit", "us-east-1")
    # The fixture has 7 events; the NODATA line with a "-" address is dropped -> 6 usable records.
    assert len(records) == 6
    assert all(r["SrcAddr"] not in (None, "-") and r["DstAddr"] not in (None, "-") for r in records)
    outbound = next(
        r for r in records if r["SrcAddr"] == "10.0.1.10" and r["DstAddr"] == "10.0.2.30"
    )
    assert outbound["InterfaceId"] == "eni-00instance0000001"
    assert outbound["DstPort"] == 443
    assert outbound["Protocol"] == "6"
    assert outbound["Action"] == "ACCEPT"
    assert isinstance(outbound["Start"], int)
    # It discovered the CloudWatch log group from describe-flow-logs and filtered that group.
    filt = next(c for c in fake_aws if tuple(c["args"][:2]) == ("logs", "filter-log-events"))
    assert any("--log-group-name=/vpc/flowlogs/prod" == a for a in filt["args"])


def test_field_index_from_format_default_and_custom():
    # Empty/absent LogFormat -> the standard v2 layout.
    assert collectors._field_index_from_format(None) == collectors._FLOW_FIELD_IDX
    assert collectors._field_index_from_format("  ") == collectors._FLOW_FIELD_IDX
    # A custom format is parsed by position from its own token order.
    idx = collectors._field_index_from_format(
        "${version} ${interface-id} ${srcaddr} ${dstaddr} ${protocol} ${dstport} ${action} ${start}"
    )
    assert idx == {
        "interface_id": 1,
        "srcaddr": 2,
        "dstaddr": 3,
        "protocol": 4,
        "dstport": 5,
        "action": 6,
        "start": 7,
    }
    # A format missing a required field (dstaddr) is rejected so we skip rather than misread.
    assert collectors._field_index_from_format("${version} ${interface-id} ${srcaddr}") is None


def test_parse_flow_log_message_honours_a_custom_format():
    idx = collectors._field_index_from_format(
        "${version} ${interface-id} ${srcaddr} ${dstaddr} ${protocol} ${dstport} ${action} ${start}"
    )
    rec = collectors._parse_flow_log_message(
        "5 eni-abc 10.0.0.1 10.0.0.2 6 443 ACCEPT 1781481600", "/g", idx
    )
    assert rec["InterfaceId"] == "eni-abc"
    assert rec["SrcAddr"] == "10.0.0.1" and rec["DstAddr"] == "10.0.0.2"
    assert rec["DstPort"] == 443 and rec["Protocol"] == "6" and rec["Action"] == "ACCEPT"
    assert rec["Start"] == 1781481600
    assert rec["SrcPort"] is None  # not present in this custom format


def test_collect_flow_log_records_reads_s3(monkeypatch):
    import gzip

    header = (
        "version account-id interface-id srcaddr dstaddr srcport dstport "
        "protocol packets bytes start end action log-status"
    )
    record = (
        "2 111111111111 eni-s3 10.0.0.1 10.0.0.2 40000 443 6 5 500 1781481600 1781481660 ACCEPT OK"
    )
    recent = datetime.now(UTC).isoformat()

    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return {
                "FlowLogs": [
                    {
                        "FlowLogId": "fl-s3",
                        "ResourceId": "vpc-1",
                        "LogDestinationType": "s3",
                        "LogDestination": "arn:aws:s3:::my-bucket/AWSLogs/",
                        "LogFormat": None,
                    }
                ]
            }
        if key == ("s3api", "list-objects-v2"):
            # A .gz flow-log object (recent) plus a non-.gz object that must be ignored.
            assert any(a == "--bucket=my-bucket" for a in args)
            return {
                "Contents": [
                    {"Key": "AWSLogs/x/flow.log.gz", "LastModified": recent},
                    {"Key": "AWSLogs/x/other.txt", "LastModified": recent},
                ]
            }
        raise AssertionError(f"unexpected run_aws call: {key}")

    def _download(args, dest, *, profile=None, region=None):
        assert ("s3api", "get-object") == tuple(args[:2])
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(header + "\n" + record + "\n")
        return dest

    monkeypatch.setattr(runner, "run_aws", _run)
    monkeypatch.setattr(runner, "download_object", _download)

    records = collectors.collect_flow_log_records("prod-audit", "us-east-1")
    assert len(records) == 1  # the .txt object was skipped, the .gz parsed via its header row
    rec = records[0]
    assert rec["InterfaceId"] == "eni-s3"
    assert rec["SrcAddr"] == "10.0.0.1" and rec["DstAddr"] == "10.0.0.2"
    assert rec["DstPort"] == 443 and rec["Protocol"] == "6" and rec["Action"] == "ACCEPT"
    assert rec["LogGroup"] == "s3://my-bucket/AWSLogs/x/flow.log.gz"


def test_unsupported_flow_log_destination_raises(monkeypatch):
    def _run(args, *, profile=None, region=None, cache_dir=None):
        if tuple(args[:2]) == ("ec2", "describe-flow-logs"):
            return {
                "FlowLogs": [
                    {
                        "FlowLogId": "fl-fh",
                        "LogDestinationType": "kinesis-data-firehose",
                        "LogDestination": "arn:aws:firehose:us-east-1:111111111111:deliverystream/x",  # noqa: E501
                    }
                ]
            }
        raise AssertionError("must not fetch records for an unsupported destination")

    monkeypatch.setattr(runner, "run_aws", _run)
    with pytest.raises(collectors.FlowLogDestinationError) as excinfo:
        collectors.collect_flow_log_records("prod-audit", "us-east-1")
    assert "kinesis-data-firehose" in str(excinfo.value)


def test_parse_s3_arn():
    assert collectors._parse_s3_arn("arn:aws:s3:::bucket/AWSLogs/") == ("bucket", "AWSLogs/")
    assert collectors._parse_s3_arn("arn:aws:s3:::bucket") == ("bucket", "")
    assert collectors._parse_s3_arn("not-an-arn") is None
    assert collectors._parse_s3_arn(None) is None


def test_flow_logs_role_registered():
    assert "flow_logs" in collectors.ROLE_COLLECTORS
    assert collectors.ROLE_RESULT_KEYS["flow_logs"] == [
        "flow_logs",
        "ip_allocations",
        "flow_log_records",
    ]


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
        "nat_gateways",
        "vpc_endpoints",
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
        "nat_gateways",
        "vpc_endpoints",
    }
    assert bundle["meta"] == {
        "target": "prod",
        "region": "us-east-1",
        "accounts": {"network": "111111111111"},
    }
    assert len(bundle["network_interfaces"]) == 5
    assert len(bundle["ec2_instances"]) == 2

    # Every network collector ran with the network role's resolved profile/region.
    assert all(c["profile"] == "prod-audit" for c in fake_aws)
    assert all(c["region"] == "us-east-1" for c in fake_aws)
