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
    assert os.path.exists(path)
    stream.close()
    assert not os.path.exists(path)  # close() deletes the temp file
    stream.close()  # idempotent — no raise


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
