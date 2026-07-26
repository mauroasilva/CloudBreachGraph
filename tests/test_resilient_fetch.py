"""Resilient flow-log fetch: classifier tiers, retry/backoff, trusted-time clock check, SSO
propagation and the failure-rate safeguard (``docs/02_architecture.md §5.7`` design guidance A).

Fully offline: the ``runner`` boundary (``run_aws`` / ``download_object``), the trusted-time fetch
(``_trusted_time_offset``) and ``time.sleep`` are all mocked, so no network and no real waiting.
"""

from __future__ import annotations

import gzip
import time

import pytest

from cloudbreachgraph.aws import collectors, runner

_S3_HEADER = (
    "version account-id interface-id srcaddr dstaddr srcport dstport "
    "protocol packets bytes start end action log-status"
)
_S3_RECORD = (
    "2 111111111111 eni-s3 10.0.0.1 10.0.0.2 40000 443 6 5 500 1781481600 1781481660 ACCEPT OK"
)


def _aws_error(stderr: str, args=None) -> runner.AwsCliError:
    return runner.AwsCliError(args or ["s3api", "get-object"], 255, stderr)


def _skew_error() -> runner.AwsCliError:
    return _aws_error("An error occurred (RequestTimeTooSkewed) when calling GetObject")


@pytest.fixture(autouse=True)
def _no_real_sleep_or_verbose(monkeypatch):
    """Never sleep for real, and keep verbose off so retry echoes don't pollute captured stderr."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    runner.set_verbose(False)


def _record_sleeps(monkeypatch) -> list[int]:
    delays: list[int] = []
    monkeypatch.setattr(time, "sleep", lambda s: delays.append(s))
    return delays


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("stderr", "tier"),
    [
        ("An error occurred (RequestTimeTooSkewed)", collectors._ErrorTier.CLOCK_SKEW),
        ("An error occurred (ExpiredToken)", collectors._ErrorTier.EXPIRED),
        ("An error occurred (ExpiredTokenException)", collectors._ErrorTier.EXPIRED),
        ("An error occurred (InvalidToken)", collectors._ErrorTier.EXPIRED),
        ("An error occurred (AccessDenied)", collectors._ErrorTier.SYSTEMIC),
        ("An error occurred (SignatureDoesNotMatch)", collectors._ErrorTier.SYSTEMIC),
        ("An error occurred (ThrottlingException)", collectors._ErrorTier.TRANSIENT),
        ("An error occurred (ServiceUnavailable)", collectors._ErrorTier.TRANSIENT),
        ("Connection reset by peer", collectors._ErrorTier.TRANSIENT),
        ("An error occurred (NoSuchKey)", collectors._ErrorTier.SKIPPABLE),
        ("An error occurred (ResourceNotFoundException)", collectors._ErrorTier.SKIPPABLE),
        ("something totally unrecognised", collectors._ErrorTier.SKIPPABLE),
    ],
)
def test_classifier_routes_each_tier(stderr, tier):
    assert collectors._classify_aws_error(stderr) is tier


def test_is_expired_error_covers_both_types():
    assert collectors.is_expired_error(collectors.CredentialsExpiredError("x"))
    assert collectors.is_expired_error(_aws_error("An error occurred (ExpiredToken)"))
    assert not collectors.is_expired_error(_aws_error("An error occurred (AccessDenied)"))
    assert not collectors.is_expired_error(ValueError("nope"))


# --------------------------------------------------------------------------- #
# S3 skew handling: network vs real-clock vs unfetchable-time
# --------------------------------------------------------------------------- #
def _s3_config():
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


def _s3_list_one_object(recent):
    return {"Contents": [{"Key": "AWSLogs/x/flow.log.gz", "LastModified": recent}]}


def _install_s3(monkeypatch, download):
    from datetime import UTC, datetime

    recent = datetime.now(UTC).isoformat()

    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return _s3_config()
        if key == ("s3api", "list-objects-v2"):
            return _s3_list_one_object(recent)
        raise AssertionError(f"unexpected run_aws call: {key}")

    monkeypatch.setattr(runner, "run_aws", _run)
    monkeypatch.setattr(runner, "download_object", download)


def _gz_download_writing_record():
    def _download(args, dest, *, profile=None, region=None):
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(_S3_HEADER + "\n" + _S3_RECORD + "\n")
        return dest

    return _download


def test_skew_network_retries_then_succeeds(monkeypatch):
    # Skew twice, then success; trusted-time reports a small offset -> network -> back off & retry.
    monkeypatch.setattr(collectors, "_trusted_time_offset", lambda: 5.0)
    delays = _record_sleeps(monkeypatch)
    write = _gz_download_writing_record()
    calls = {"n": 0}

    def _download(args, dest, *, profile=None, region=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _skew_error()
        return write(args, dest)

    _install_s3(monkeypatch, _download)
    records = collectors.collect_flow_log_records("prod", "us-east-1")
    assert len(records) == 1  # eventual success, identical parsed result
    assert records[0]["InterfaceId"] == "eni-s3"
    assert calls["n"] == 3  # initial + 2 retries
    assert delays == [30, 60]  # exact backoff sequence, <= 3 retries


def test_skew_real_clock_aborts_without_retry(monkeypatch):
    # A large trusted-time offset means a genuine clock problem -> abort, zero retries.
    monkeypatch.setattr(collectors, "_trusted_time_offset", lambda: 4000.0)
    delays = _record_sleeps(monkeypatch)

    def _download(args, dest, *, profile=None, region=None):
        raise _skew_error()

    _install_s3(monkeypatch, _download)
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.collect_flow_log_records("prod", "us-east-1")
    assert "clock" in str(excinfo.value).lower()
    assert delays == []  # no retries on a real clock problem


def test_skew_time_unfetchable_is_treated_as_network(monkeypatch):
    # Trusted time can't be fetched -> treat the skew as transient -> retry (here: succeed on 3rd).
    monkeypatch.setattr(collectors, "_trusted_time_offset", lambda: None)
    delays = _record_sleeps(monkeypatch)
    write = _gz_download_writing_record()
    calls = {"n": 0}

    def _download(args, dest, *, profile=None, region=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _skew_error()
        return write(args, dest)

    _install_s3(monkeypatch, _download)
    records = collectors.collect_flow_log_records("prod", "us-east-1")
    assert len(records) == 1
    assert delays == [30, 60]


def test_transient_exhaustion_skips_the_unit(monkeypatch):
    # A transient error on the only object that never recovers -> warn + skip -> empty result.
    delays = _record_sleeps(monkeypatch)

    def _download(args, dest, *, profile=None, region=None):
        raise _aws_error("An error occurred (ServiceUnavailable)")

    _install_s3(monkeypatch, _download)
    records = collectors.collect_flow_log_records("prod", "us-east-1")
    assert records == []
    assert delays == [30, 60, 120]  # exhausted all 3 retries, then skipped


def test_s3_systemic_access_denied_aborts(monkeypatch):
    def _download(args, dest, *, profile=None, region=None):
        raise _aws_error("An error occurred (AccessDenied)")

    _install_s3(monkeypatch, _download)
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.collect_flow_log_records("prod", "us-east-1")
    msg = str(excinfo.value)
    assert "s3:ListBucket" in msg and "s3:GetObject" in msg and "my-bucket" in msg


def test_s3_expired_token_propagates(monkeypatch):
    def _download(args, dest, *, profile=None, region=None):
        raise _aws_error("An error occurred (ExpiredToken)")

    _install_s3(monkeypatch, _download)
    with pytest.raises(collectors.CredentialsExpiredError):
        collectors.collect_flow_log_records("prod", "us-east-1")


def test_corrupt_gzip_object_is_skipped(monkeypatch):
    # A non-gzip body -> BadGzipFile -> _SkippableUnitError -> warn + skip; run completes empty.
    def _download(args, dest, *, profile=None, region=None):
        with open(dest, "wb") as fh:
            fh.write(b"this is not gzip")
        return dest

    _install_s3(monkeypatch, _download)
    records = collectors.collect_flow_log_records("prod", "us-east-1")
    assert records == []


# --------------------------------------------------------------------------- #
# CloudWatch parity: systemic aborts, per-group ResourceNotFound skips
# --------------------------------------------------------------------------- #
def _cw_config(groups):
    return {
        "FlowLogs": [
            {
                "FlowLogId": f"fl-{i}",
                "ResourceId": "vpc-1",
                "LogDestinationType": "cloud-watch-logs",
                "LogGroupName": g,
                "LogFormat": None,
            }
            for i, g in enumerate(groups)
        ]
    }


def test_cloudwatch_systemic_error_aborts(monkeypatch):
    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return _cw_config(["/vpc/flowlogs/prod"])
        if key == ("logs", "filter-log-events"):
            raise _aws_error("An error occurred (AccessDenied)", args=list(args))
        raise AssertionError(key)

    monkeypatch.setattr(runner, "run_aws", _run)
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.collect_flow_log_records("prod", "us-east-1")
    assert "logs:FilterLogEvents" in str(excinfo.value)


def test_cloudwatch_missing_group_is_skipped_others_read(monkeypatch):
    # Two groups: one ResourceNotFound (skip), one returns a usable event -> parsed from the second.
    good_event = {
        "message": (
            "2 111111111111 eni-cw 10.0.0.1 10.0.0.2 40000 443 6 5 500 "
            "1781481600 1781481660 ACCEPT OK"
        )
    }

    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return _cw_config(["/vpc/missing", "/vpc/present"])
        if key == ("logs", "filter-log-events"):
            group = next(a for a in args if a.startswith("--log-group-name="))
            if group.endswith("/vpc/missing"):
                raise _aws_error("An error occurred (ResourceNotFoundException)", args=list(args))
            return {"events": [good_event]}
        raise AssertionError(key)

    monkeypatch.setattr(runner, "run_aws", _run)
    records = collectors.collect_flow_log_records("prod", "us-east-1")
    assert len(records) == 1
    assert records[0]["InterfaceId"] == "eni-cw"


# --------------------------------------------------------------------------- #
# Failure-rate safeguard (both sources)
# --------------------------------------------------------------------------- #
def test_safeguard_trips_on_streak_of_s3_failures(monkeypatch):
    from datetime import UTC, datetime

    recent = datetime.now(UTC).isoformat()
    keys = [f"AWSLogs/x/flow-{i}.log.gz" for i in range(6)]

    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return _s3_config()
        if key == ("s3api", "list-objects-v2"):
            return {"Contents": [{"Key": k, "LastModified": recent} for k in keys]}
        raise AssertionError(key)

    def _download(args, dest, *, profile=None, region=None):
        raise _aws_error("An error occurred (NoSuchKey)")  # every object missing

    monkeypatch.setattr(runner, "run_aws", _run)
    monkeypatch.setattr(runner, "download_object", _download)
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.collect_flow_log_records("prod", "us-east-1")
    assert "too many" in str(excinfo.value)


def test_safeguard_trips_on_streak_of_cloudwatch_failures(monkeypatch):
    groups = [f"/vpc/g{i}" for i in range(6)]

    def _run(args, *, profile=None, region=None, cache_dir=None):
        key = tuple(args[:2])
        if key == ("ec2", "describe-flow-logs"):
            return _cw_config(groups)
        if key == ("logs", "filter-log-events"):
            raise _aws_error("An error occurred (ResourceNotFoundException)", args=list(args))
        raise AssertionError(key)

    monkeypatch.setattr(runner, "run_aws", _run)
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.collect_flow_log_records("prod", "us-east-1")
    assert "too many" in str(excinfo.value)
