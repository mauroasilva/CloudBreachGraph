"""Bounded-memory flow-log fetch (``docs/02_architecture.md §5.7``): the disk-backed record stream,
the streaming gunzip, and manual CloudWatch pagination.

These bound the peak memory of a large flow-log run — records stream through a temp file instead of
a giant in-RAM list, S3 objects gunzip line by line, and CloudWatch is read in bounded pages. Fully
offline: the ``runner`` boundary and ``time.sleep`` are mocked.
"""

from __future__ import annotations

import gzip
import os
import time
from datetime import UTC, datetime

import pytest

from cloudbreachgraph.aws import collectors, runner

_HEADER = (
    "version account-id interface-id srcaddr dstaddr srcport dstport "
    "protocol packets bytes start end action log-status"
)


def _v2_line(iface: str, src: str, dst: str, dport: int = 443) -> str:
    """A default-layout (v2) flow-log record line."""
    return f"2 111111111111 {iface} {src} {dst} 40000 {dport} 6 5 500 1781481600 1781481660 ACCEPT"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    runner.set_verbose(False)


# --------------------------------------------------------------------------- #
# FlowLogRecordStream — disk-backed, re-iterable, self-cleaning
# --------------------------------------------------------------------------- #
def test_record_stream_roundtrips_reiterates_counts_and_closes():
    stream = collectors.FlowLogRecordStream()
    stream.extend([{"InterfaceId": "eni-1", "SrcAddr": "10.0.0.1", "DstPort": 443, "Start": 1}])
    stream.extend([{"InterfaceId": "eni-2", "SrcAddr": None, "DstPort": None, "Start": None}])
    assert stream.count == 2

    # Re-iterable: two passes yield identical, fully round-tripped dicts (ints/None preserved).
    first = list(stream)
    second = list(stream)
    assert first == second
    assert first[0] == {"InterfaceId": "eni-1", "SrcAddr": "10.0.0.1", "DstPort": 443, "Start": 1}
    assert first[1]["SrcAddr"] is None and first[1]["DstPort"] is None

    path = stream._path
    assert path.endswith(".ndjson.gz")  # the spill is gzip-compressed
    assert os.path.exists(path)
    stream.close()
    assert not os.path.exists(path)  # close() deletes the temp file
    stream.close()  # idempotent — no raise


class _BoomFH:
    """A write handle whose every write fails as if the disk were full."""

    def write(self, *_a):
        raise OSError(28, "No space left on device")

    def close(self):  # close must never raise (ENOSPC-abort path)
        pass


def test_spill_out_of_space_raises_actionable_error():
    stream = collectors.FlowLogRecordStream()
    stream._fh = _BoomFH()  # simulate ENOSPC on the next write
    try:
        with pytest.raises(collectors.FlowLogFetchError) as excinfo:
            stream.extend([{"InterfaceId": "eni-1", "SrcAddr": "10.0.0.1"}])
        msg = str(excinfo.value)
        assert "space" in msg.lower()  # names the out-of-space cause
        assert "--spill-dir" in msg and "--flow-log-days" in msg  # actionable guidance
    finally:
        stream.close()


def test_spill_dir_places_file_in_the_configured_directory(tmp_path):
    # Explicit constructor arg…
    s1 = collectors.FlowLogRecordStream(spill_dir=str(tmp_path))
    try:
        assert os.path.dirname(s1._path) == str(tmp_path)
    finally:
        s1.close()
    # …and the module knob the CLI sets from --spill-dir.
    collectors.configure_spill_dir(str(tmp_path))
    try:
        s2 = collectors.FlowLogRecordStream()
        assert os.path.dirname(s2._path) == str(tmp_path)
        s2.close()
    finally:
        collectors.configure_spill_dir(None)


def test_spill_dir_is_created_if_missing(tmp_path):
    # A fresh --spill-dir path (like ./spill/) is auto-created, not an error (mirrors --cache-dir).
    missing = tmp_path / "new" / "spill"
    stream = collectors.FlowLogRecordStream(spill_dir=str(missing))
    try:
        assert missing.is_dir()  # created
        assert os.path.dirname(stream._path) == str(missing)
    finally:
        stream.close()


def test_spill_dir_that_is_a_file_errors_without_mislabelling_as_out_of_space(tmp_path):
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x")
    with pytest.raises(collectors.FlowLogFetchError) as excinfo:
        collectors.FlowLogRecordStream(spill_dir=str(not_a_dir))
    msg = str(excinfo.value)
    assert "--spill-dir" in msg
    assert "space" not in msg.lower()  # NOT the "out of disk space" message — real cause differs


def test_enoent_write_error_names_the_missing_directory_not_out_of_space():
    # An ENOENT (No such file or directory) at write time must read as a missing dir, not ENOSPC —
    # the exact confusion the user hit (400 GB free, but told "out of space").
    msg = collectors._spill_error_message("./spill/", OSError(2, "No such file or directory"))
    assert "does not exist" in msg
    assert "out of disk space" not in msg and "out of space" not in msg


# --------------------------------------------------------------------------- #
# Disk-space guards — stop cleanly BEFORE ENOSPC, never crash with a traceback
# --------------------------------------------------------------------------- #
def test_per_write_guard_stops_when_free_space_below_margin(monkeypatch):
    # Guard 2: free space below the safety margin at the start of a write -> clean abort, no write.
    monkeypatch.setattr(
        collectors, "_free_bytes", lambda _d: collectors._SPILL_FREE_MARGIN_BYTES - 1
    )
    stream = collectors.FlowLogRecordStream()
    try:
        with pytest.raises(collectors.FlowLogFetchError) as excinfo:
            stream.extend([{"InterfaceId": "eni-1", "SrcAddr": "10.0.0.1"}])
        assert "margin" in str(excinfo.value) and "--spill-dir" in str(excinfo.value)
        assert stream.count == 0  # nothing was written
    finally:
        stream.close()


def test_preflight_aborts_before_download_when_objects_wont_fit(monkeypatch):
    # Guard 1: the summed in-scope object sizes + margin exceed free space -> abort BEFORE any
    # get-object. A huge listing (sizes in the GiBs) against a tiny free volume.
    recent = datetime.now(UTC).isoformat()

    def _run(args, *, profile=None, region=None, cache_dir=None):
        assert tuple(args[:2]) == ("s3api", "list-objects-v2")  # listing only; no get here
        return {
            "Contents": [
                {"Key": _obj_key(i), "LastModified": recent, "Size": 2**30}  # 1 GiB each
                for i in range(8)  # ~8 GiB of in-scope objects
            ]
        }

    downloads = {"n": 0}

    def _download(args, dest, *, profile=None, region=None):
        downloads["n"] += 1
        return dest

    monkeypatch.setattr(runner, "run_aws", _run)
    monkeypatch.setattr(runner, "download_object", _download)
    monkeypatch.setattr(
        collectors, "_free_bytes", lambda _d: 100 * 1024 * 1024
    )  # only 100 MiB free

    stream = collectors.FlowLogRecordStream()
    flow_logs = [_fl_s3_cfg("fl-0000000000000a53", "vpc-1", "bucket-a")]
    try:
        with pytest.raises(collectors.FlowLogFetchError) as excinfo:
            collectors._read_s3_records(flow_logs, "prod", "us-east-1", 0.0, None, sink=stream)
    finally:
        stream.close()
    msg = str(excinfo.value)
    assert "before download" in msg and "--spill-dir" in msg
    assert downloads["n"] == 0  # nothing was fetched — failed on the preflight


def _fl_s3_cfg(fl_id: str, resource_id: str, bucket: str) -> dict:
    return {
        "FlowLogId": fl_id,
        "ResourceId": resource_id,
        "LogDestinationType": "s3",
        "LogDestination": f"arn:aws:s3:::{bucket}/AWSLogs/",
        "LogFormat": None,
    }


def _obj_key(n: int) -> str:
    return (
        f"AWSLogs/111111111111/vpcflowlogs/us-east-1/2026/08/01/"
        f"111111111111_vpcflowlogs_us-east-1_fl-0000000000000a53_20260801T00{n:02d}Z_h.log.gz"
    )


def test_record_stream_mapping_matches_a_plain_list():
    """A bundle whose records are a FlowLogRecordStream yields the exact same graph as one whose
    records are the equivalent list — the streaming path changes nothing about the output."""
    from cloudbreachgraph.mapping.builder import build_graph

    records = [
        {
            "InterfaceId": "eni-a",
            "SrcAddr": "10.0.0.1",
            "DstAddr": "203.0.113.9",
            "SrcPort": 40000,
            "DstPort": 443,
            "Protocol": "6",
            "Start": 1781481600,
            "Action": "ACCEPT",
            "LogGroup": "/g",
        }
    ]
    base = {
        "network_interfaces": [
            {
                "NetworkInterfaceId": "eni-a",
                "PrivateIpAddresses": [{"PrivateIpAddress": "10.0.0.1"}],
            }
        ],
        "vpcs": [],
        "subnets": [],
        "flow_logs": [],
        "ip_allocations": [],
        "historical_enis": [],
    }
    via_list = build_graph({**base, "flow_log_records": list(records)}, map_flow_logs=True)

    stream = collectors.FlowLogRecordStream()
    stream.extend(records)
    try:
        via_stream = build_graph({**base, "flow_log_records": stream}, map_flow_logs=True)
        assert via_stream.to_dict() == via_list.to_dict()
    finally:
        stream.close()


# --------------------------------------------------------------------------- #
# Streaming gunzip — an object is parsed line by line, never fully materialized
# --------------------------------------------------------------------------- #
def _install_download(monkeypatch, writer):
    def _download(args, dest, *, profile=None, region=None):
        writer(dest)
        return dest

    monkeypatch.setattr(runner, "download_object", _download)


def test_read_s3_object_parses_header_then_records(monkeypatch):
    rec = _v2_line("eni-s3", "10.0.0.1", "10.0.0.2") + " OK"  # + log-status column

    def _write(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(_HEADER + "\n" + rec + "\n")

    _install_download(monkeypatch, _write)
    out = collectors._read_s3_object_records("b", "k", "p", "r")
    assert len(out) == 1
    assert out[0]["InterfaceId"] == "eni-s3" and out[0]["DstPort"] == 443


def test_read_s3_object_without_header_uses_default_layout(monkeypatch):
    # No header row: the first line is data and must still be parsed (default v2 layout).
    def _write(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write(_v2_line("eni-x", "10.0.0.3", "10.0.0.4", dport=80) + "\n")

    _install_download(monkeypatch, _write)
    out = collectors._read_s3_object_records("b", "k", "p", "r")
    assert len(out) == 1 and out[0]["InterfaceId"] == "eni-x" and out[0]["DstPort"] == 80


def test_iter_gz_lines_streams_lazily_and_cleans_up(monkeypatch):
    # The generator yields one line at a time (not a materialized list) and deletes its temp file.
    def _write(dest):
        with gzip.open(dest, "wt", encoding="utf-8") as fh:
            fh.write("a\nb\nc\n")

    _install_download(monkeypatch, _write)
    gen = collectors._iter_gz_lines("b", "k", "p", "r")
    assert next(gen) == "a"  # lazy — first line available before the rest is read
    assert list(gen) == ["b", "c"]


def test_corrupt_gzip_object_raises_skippable(monkeypatch):
    def _write(dest):
        with open(dest, "wb") as fh:
            fh.write(b"this is not gzip")

    _install_download(monkeypatch, _write)
    with pytest.raises(collectors._SkippableUnitError):
        collectors._read_s3_object_records("b", "k", "p", "r")


# --------------------------------------------------------------------------- #
# CloudWatch pagination — bounded pages, per-page emit, idempotent retry
# --------------------------------------------------------------------------- #
def _cw_flow_log(fl_id: str = "fl-1", group: str = "/g") -> dict:
    return {"FlowLogId": fl_id, "LogGroupName": group, "LogFormat": None}


def test_cloudwatch_pagination_follows_next_token_and_emits_per_page(monkeypatch):
    calls: list[list[str]] = []

    def _run(args, *, profile=None, region=None, cache_dir=None):
        calls.append(list(args))
        starting = next((a for a in args if a.startswith("--starting-token=")), None)
        if starting is None:  # page 1 -> one event + a continuation token
            return {
                "events": [{"message": _v2_line("eni-1", "10.0.0.1", "10.0.0.2")}],
                "NextToken": "TOK",
            }
        return {
            "events": [{"message": _v2_line("eni-1", "10.0.0.3", "10.0.0.4")}]
        }  # page 2, no token

    monkeypatch.setattr(runner, "run_aws", _run)
    sink: list[dict] = []
    per_vpc: dict[str, dict[str, int]] = {}
    fetched, skipped = collectors._read_cloudwatch_records(
        [_cw_flow_log()],
        "p",
        "r",
        0.0,
        None,
        sink=sink,
        fl_to_vpc={"fl-1": "vpc-1"},
        per_vpc=per_vpc,
    )
    # Both pages were read and emitted; each call is bounded by --max-items.
    assert fetched == 2 and len(sink) == 2 and skipped == 0
    assert all(any(a.startswith("--max-items=") for a in c) for c in calls)
    assert any(a.startswith("--starting-token=TOK") for a in calls[1])
    assert not any(a.startswith("--starting-token=") for a in calls[0])
    # Per-VPC accounting sums across pages.
    assert per_vpc["vpc-1"] == {"objects": 2, "records": 2}


def test_cloudwatch_page_retry_does_not_double_emit(monkeypatch):
    attempts = {"n": 0}

    def _run(args, *, profile=None, region=None, cache_dir=None):
        attempts["n"] += 1
        if attempts["n"] == 1:  # first attempt: transient failure
            raise runner.AwsCliError(list(args), 255, "An error occurred (ServiceUnavailable)")
        return {
            "events": [{"message": _v2_line("eni-1", "10.0.0.1", "10.0.0.2")}]
        }  # retry succeeds

    monkeypatch.setattr(runner, "run_aws", _run)
    sink: list[dict] = []
    fetched, skipped = collectors._read_cloudwatch_records(
        [_cw_flow_log()], "p", "r", 0.0, None, sink=sink
    )
    # The page is retried (2 calls) but its records are emitted exactly once (emit is post-success).
    assert attempts["n"] == 2
    assert fetched == 1 and len(sink) == 1 and skipped == 0
