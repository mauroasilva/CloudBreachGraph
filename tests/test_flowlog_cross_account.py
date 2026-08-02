"""Cross-account flow-log collection (``docs/02_architecture.md §5.7`` guidance A–D).

Covers the two-account split (config/CloudTrail/CloudWatch in the network account, S3 object I/O in
the archive account), the archive-account auto-resolution (primary→AccessDenied fallback), the VPC
coverage check, and the download precision + completeness accounting. Fully offline: the ``runner``
boundary (``run_aws`` / ``download_object``) and ``time.sleep`` are mocked.
"""

from __future__ import annotations

import gzip
import time
from datetime import UTC, datetime

import pytest
from conftest import load_fixture

from cloudbreachgraph.aws import collectors, runner
from cloudbreachgraph.config import ResolvedAccount, ResolvedTarget

# Discovered universe comes from the standard network fixtures (VPCs / subnets / ENIs).
VPC_A = "vpc-0aaaaaaaaaaaaaaaa"  # has subnets subnet-011.../subnet-022... and every fixture ENI
VPC_D = "vpc-0defdefdefdefdefd"  # a second discovered VPC with no subnet/ENI in the fixtures

NET = ResolvedAccount(profile="net-prof", account_id="111111111111", region="us-east-1")
ARCH = ResolvedAccount(profile="arch-prof", account_id="999999999999", region="eu-west-1")

_NET_FIXTURES = {
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
}

_HEADER = (
    "version account-id interface-id srcaddr dstaddr srcport dstport "
    "protocol packets bytes start end action log-status"
)
_RECORD = (
    "2 111111111111 eni-00instance0000001 10.0.1.10 203.0.113.9 "
    "40000 443 6 5 500 1781481600 1781481660 ACCEPT OK"
)


@pytest.fixture(autouse=True)
def _reset_collector_globals(monkeypatch):
    """Deterministic collector globals + no real sleeping; historical-ENI reconstruction off (its
    empty CloudTrail calls aren't under test here)."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    collectors.set_flow_log_range(None)
    collectors.set_flow_log_window(collectors.FLOW_LOG_MAX_LOOKBACK_DAYS)
    collectors.set_historical_enis(False)
    runner.set_verbose(False)
    yield
    collectors.set_historical_enis(True)


def _s3_key(
    fl_id: str, *, acct: str = "111111111111", region: str = "us-east-1", n: int = 0
) -> str:
    """A realistic VPC flow-log S3 object key embedding its ``fl-…`` id and source account."""
    return (
        f"AWSLogs/{acct}/vpcflowlogs/{region}/2026/08/01/"
        f"{acct}_vpcflowlogs_{region}_{fl_id}_20260801T00{n:02d}Z_abcd1234.log.gz"
    )


def _fl_s3(fl_id: str, resource_id: str, bucket: str) -> dict:
    return {
        "FlowLogId": fl_id,
        "ResourceId": resource_id,
        "LogDestinationType": "s3",
        "LogDestination": f"arn:aws:s3:::{bucket}/AWSLogs/",
        "LogGroupName": None,
        "LogFormat": None,
    }


def _fl_cw(fl_id: str, resource_id: str, group: str) -> dict:
    return {
        "FlowLogId": fl_id,
        "ResourceId": resource_id,
        "LogDestinationType": "cloud-watch-logs",
        "LogGroupName": group,
        "LogDestination": None,
        "LogFormat": None,
    }


class _World:
    """A mock ``run_aws``/``download_object`` boundary recording every call's profile/region."""

    def __init__(
        self,
        flow_logs: list[dict],
        s3_list: dict[str, list[str]] | None = None,
        *,
        deny_list_for: set[str] | None = None,
    ) -> None:
        self.flow_logs = flow_logs
        self.s3_list = s3_list or {}
        self.deny_list_for = deny_list_for or set()
        self.calls: list[dict] = []
        self.downloads: list[dict] = []
        self._recent = datetime.now(UTC).isoformat()

    def run_aws(self, args, *, profile=None, region=None, cache_dir=None):
        self.calls.append({"args": list(args), "profile": profile, "region": region})
        key = tuple(args[:2])
        if key == ("sts", "get-caller-identity"):
            return {"Account": "111111111111"}
        if key == ("ec2", "describe-flow-logs"):
            return {"FlowLogs": self.flow_logs}
        if key == ("cloudtrail", "lookup-events"):
            return {"Events": []}
        if key == ("logs", "filter-log-events"):
            return {"events": []}
        if key == ("s3api", "list-objects-v2"):
            if profile in self.deny_list_for:
                raise runner.AwsCliError(
                    list(args), 255, "An error occurred (AccessDenied) when calling ListObjectsV2"
                )
            bucket = next(a.split("=", 1)[1] for a in args if a.startswith("--bucket="))
            return {
                "Contents": [
                    {"Key": k, "LastModified": self._recent, "Size": 500}
                    for k in self.s3_list.get(bucket, [])
                ]
            }
        return load_fixture(_NET_FIXTURES[key])

    def download(self, args, dest, *, profile=None, region=None):
        key = next(a.split("=", 1)[1] for a in args if a.startswith("--key="))
        self.downloads.append(
            {"args": list(args), "profile": profile, "region": region, "key": key}
        )
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(_HEADER + "\n" + _RECORD + "\n")
        return dest

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(runner, "run_aws", self.run_aws)
        monkeypatch.setattr(runner, "download_object", self.download)

    # -- convenience accessors ------------------------------------------------ #
    def profiles_for(self, cmd: tuple[str, str]) -> set[str | None]:
        return {c["profile"] for c in self.calls if tuple(c["args"][:2]) == cmd}

    def regions_for(self, cmd: tuple[str, str]) -> set[str | None]:
        return {c["region"] for c in self.calls if tuple(c["args"][:2]) == cmd}

    def count(self, cmd: tuple[str, str]) -> int:
        return sum(1 for c in self.calls if tuple(c["args"][:2]) == cmd)


def _resolved(network: ResolvedAccount, flow_logs: ResolvedAccount) -> ResolvedTarget:
    return ResolvedTarget(target="t", roles={"network": network, "flow_logs": flow_logs})


def _collect(world: _World, resolved: ResolvedTarget, archive: collectors.ArchiveAccess) -> dict:
    return collectors.collect_all(resolved, roles=("network", "flow_logs"), archive_access=archive)


# --------------------------------------------------------------------------- #
# A. Two-account split
# --------------------------------------------------------------------------- #
def test_account_split_runs_config_in_network_and_s3_in_archive(monkeypatch):
    world = _World(
        [
            _fl_cw("fl-0000000000000ac1", VPC_A, "/vpc/flowlogs/prod"),
            _fl_s3("fl-0000000000000a53", VPC_A, "bucket-a"),
        ],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
    )
    world.install(monkeypatch)
    resolved = _resolved(NET, ARCH)
    # A two-account target binds flow_logs to the archive account -> explicit S3 account.
    archive = collectors.ArchiveAccess(primary=NET, explicit=ARCH)

    bundle = _collect(world, resolved, archive)

    # Config, CloudTrail history and CloudWatch reads all ran under the NETWORK account.
    assert world.profiles_for(("ec2", "describe-flow-logs")) == {"net-prof"}
    assert world.profiles_for(("cloudtrail", "lookup-events")) == {"net-prof"}
    assert world.profiles_for(("logs", "filter-log-events")) == {"net-prof"}
    assert world.regions_for(("logs", "filter-log-events")) == {"us-east-1"}
    # S3 list + get ran under the ARCHIVE account (its own profile AND region).
    assert world.profiles_for(("s3api", "list-objects-v2")) == {"arch-prof"}
    assert world.regions_for(("s3api", "list-objects-v2")) == {"eu-west-1"}
    assert world.downloads and all(d["profile"] == "arch-prof" for d in world.downloads)
    assert all(d["region"] == "eu-west-1" for d in world.downloads)
    # Provenance records both accounts.
    assert bundle["meta"]["accounts"]["network"] == "111111111111"
    assert bundle["meta"]["accounts"]["flow_logs"] == "999999999999"


def test_describe_flow_logs_is_queried_once(monkeypatch):
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
    )
    world.install(monkeypatch)
    _collect(world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH))
    # describe-flow-logs runs exactly once (no second query inside the record fetch), under network.
    assert world.count(("ec2", "describe-flow-logs")) == 1
    assert world.profiles_for(("ec2", "describe-flow-logs")) == {"net-prof"}


def test_single_account_runs_every_flow_log_command_under_one_profile(monkeypatch):
    world = _World(
        [
            _fl_cw("fl-0000000000000ac1", VPC_A, "/vpc/flowlogs/prod"),
            _fl_s3("fl-0000000000000a53", VPC_A, "bucket-a"),
        ],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
    )
    world.install(monkeypatch)
    # network and flow_logs resolve to the same account -> ArchiveAccess primary == explicit == NET.
    resolved = _resolved(NET, NET)
    archive = collectors.ArchiveAccess(primary=NET, explicit=NET)
    _collect(world, resolved, archive)
    for cmd in (
        ("ec2", "describe-flow-logs"),
        ("cloudtrail", "lookup-events"),
        ("logs", "filter-log-events"),
        ("s3api", "list-objects-v2"),
    ):
        assert world.profiles_for(cmd) == {"net-prof"}, cmd
    assert world.downloads and all(d["profile"] == "net-prof" for d in world.downloads)


# --------------------------------------------------------------------------- #
# B. Archive-account auto-resolution
# --------------------------------------------------------------------------- #
def test_auto_fallback_retries_under_second_profile_on_access_denied(monkeypatch):
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
        deny_list_for={"net-prof"},  # the primary can't read the bucket
    )
    world.install(monkeypatch)
    # No explicit binding: primary NET tried first, ARCH is the configured fallback candidate.
    archive = collectors.ArchiveAccess(primary=NET, explicit=None, candidates=(ARCH,))
    bundle = _collect(world, _resolved(NET, NET), archive)

    # The list was attempted under the primary (denied) and then succeeded under the candidate.
    assert world.profiles_for(("s3api", "list-objects-v2")) == {"net-prof", "arch-prof"}
    assert world.downloads and all(d["profile"] == "arch-prof" for d in world.downloads)
    assert len(bundle["flow_log_records"]) >= 1
    # Provenance reflects the resolved archive account, not the primary.
    assert bundle["meta"]["accounts"]["flow_logs"] == "999999999999"
    # Resolution never issues an --expected-bucket-owner probe to discover the owner.
    assert not any("--expected-bucket-owner" in a for c in world.calls for a in c["args"])


def test_auto_fallback_exhausted_errors_with_bucket_and_profiles(monkeypatch):
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
        deny_list_for={"net-prof", "arch-prof"},  # nobody can read it
    )
    world.install(monkeypatch)
    archive = collectors.ArchiveAccess(primary=NET, explicit=None, candidates=(ARCH,))
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        _collect(world, _resolved(NET, NET), archive)
    msg = str(excinfo.value)
    assert "bucket-a" in msg and "net-prof" in msg and "arch-prof" in msg
    assert world.downloads == []  # never got to a get-object


def test_explicit_binding_skips_the_primary_trial(monkeypatch):
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
        deny_list_for={"net-prof"},  # would fail if the primary were tried — it must not be
    )
    world.install(monkeypatch)
    archive = collectors.ArchiveAccess(primary=NET, explicit=ARCH, candidates=(ARCH,))
    _collect(world, _resolved(NET, ARCH), archive)
    # Only the bound archive profile is used for S3 — the primary is never attempted.
    assert world.profiles_for(("s3api", "list-objects-v2")) == {"arch-prof"}
    assert world.downloads and all(d["profile"] == "arch-prof" for d in world.downloads)


# --------------------------------------------------------------------------- #
# C. VPC coverage reconciliation
# --------------------------------------------------------------------------- #
def test_coverage_hard_fails_before_any_download_when_all_foreign(monkeypatch):
    # Every flow log references a VPC that is NOT in the discovered set (the cross-account symptom).
    world = _World(
        [
            _fl_s3("fl-00000000000ff001", "vpc-foreign000001", "bucket-x"),
            _fl_s3("fl-00000000000ff002", "vpc-foreign000002", "bucket-x"),
        ],
        {"bucket-x": [_s3_key("fl-00000000000ff001"), _s3_key("fl-00000000000ff002")]},
    )
    world.install(monkeypatch)
    with pytest.raises(collectors.FlowLogCoverageError) as excinfo:
        _collect(world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH))
    msg = str(excinfo.value)
    assert "wrong account" in msg
    # It failed BEFORE any S3 I/O — no listing, no downloads.
    assert world.count(("s3api", "list-objects-v2")) == 0
    assert world.downloads == []


def test_coverage_partial_proceeds_and_warns_about_uncovered_vpc(monkeypatch, capsys):
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {"bucket-a": [_s3_key("fl-0000000000000a53")]},
    )
    world.install(monkeypatch)
    bundle = _collect(
        world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH)
    )
    err = capsys.readouterr().err
    assert VPC_D in err and "no flow log configured" in err
    cov = bundle["meta"]["flow_log_coverage"]
    assert cov["vpcs_total"] == 2 and cov["vpcs_covered"] == 1
    assert cov["covered_vpcs"] == [VPC_A] and cov["uncovered_vpcs"] == [VPC_D]


# --------------------------------------------------------------------------- #
# D. Precision + completeness
# --------------------------------------------------------------------------- #
def test_precision_downloads_only_in_scope_flow_log_objects(monkeypatch):
    # bucket-a mixes in-scope (fl-s3...) and out-of-scope (fl-other...) objects on a shared prefix.
    world = _World(
        [_fl_s3("fl-0000000000000a53", VPC_A, "bucket-a")],
        {
            "bucket-a": [
                _s3_key("fl-0000000000000a53", n=1),
                _s3_key("fl-0000000000000a53", n=2),
                _s3_key("fl-00000000000000ff", n=3),
                _s3_key("fl-00000000000000ff", n=4),
            ]
        },
    )
    world.install(monkeypatch)
    _collect(world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH))
    fetched = [d["key"] for d in world.downloads]
    assert fetched and all("fl-0000000000000a53" in k for k in fetched)
    assert not any("fl-00000000000000ff" in k for k in fetched)


def test_completeness_warns_on_a_covered_but_empty_vpc(monkeypatch, capsys):
    world = _World(
        [
            _fl_s3("fl-0000000000000a53", VPC_A, "bucket-a"),
            _fl_s3("fl-0000000000000d53", VPC_D, "bucket-d"),
        ],
        {"bucket-a": [_s3_key("fl-0000000000000a53")], "bucket-d": []},  # VPC_D has no objects
    )
    world.install(monkeypatch)
    bundle = _collect(
        world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH)
    )
    err = capsys.readouterr().err
    assert VPC_D in err and "no in-window flow-log data" in err
    per_vpc = bundle["meta"]["flow_log_coverage"]["per_vpc"]
    assert per_vpc[VPC_A]["objects"] == 1
    assert per_vpc[VPC_D]["objects"] == 0


def test_completeness_hard_errors_when_every_covered_vpc_is_empty(monkeypatch):
    world = _World(
        [
            _fl_s3("fl-0000000000000a53", VPC_A, "bucket-a"),
            _fl_s3("fl-0000000000000d53", VPC_D, "bucket-d"),
        ],
        {"bucket-a": [], "bucket-d": []},  # nothing in-window for either VPC
    )
    world.install(monkeypatch)
    with pytest.raises(collectors.FlowLogCoverageError) as excinfo:
        _collect(world, _resolved(NET, ARCH), collectors.ArchiveAccess(primary=NET, explicit=ARCH))
    msg = str(excinfo.value)
    assert VPC_A in msg and VPC_D in msg and "zero in-window" in msg


# --------------------------------------------------------------------------- #
# Pure coverage helper
# --------------------------------------------------------------------------- #
def test_check_vpc_coverage_maps_subnet_and_eni_scopes():
    vpcs = [{"VpcId": VPC_A}, {"VpcId": VPC_D}]
    subnets = [{"SubnetId": "subnet-1", "VpcId": VPC_A}]
    enis = [{"NetworkInterfaceId": "eni-1", "VpcId": VPC_D}]
    flow_logs = [
        {"FlowLogId": "fl-a", "ResourceId": "subnet-1"},  # subnet scope -> VPC_A
        {"FlowLogId": "fl-b", "ResourceId": "eni-1"},  # eni scope -> VPC_D
    ]
    cov = collectors.check_vpc_coverage(flow_logs, vpcs, subnets, enis)
    assert cov.covered_vpcs == [VPC_A, VPC_D]
    assert cov.fl_to_vpc == {"fl-a": VPC_A, "fl-b": VPC_D}
    assert cov.in_scope_fl_ids == {"fl-a", "fl-b"}
