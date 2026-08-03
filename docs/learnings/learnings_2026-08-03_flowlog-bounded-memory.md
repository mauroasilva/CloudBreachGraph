# Learnings — 2026-08-03 flowlog-bounded-memory

## 1. What this change delivered
The `--flow-logs` fetch was materializing **every** parsed record in RAM (a flat `list[dict]` in the
bundle, then a second `list[FlowLogRecord]` copy in the mapping layer), plus buffering whole
`filter-log-events` / `list-objects-v2` / decompressed-S3-object responses. On a data-rich account
(millions of records) the process was OOM-killed (SIGKILL / exit 137). Three fixes bound peak memory
without changing graph output:

1. **Streaming gunzip** (`_iter_gz_lines` replaces `_download_gz_lines`). An S3 object is downloaded
   to a temp `.gz` and gunzipped **line by line** (`for line in gzip.open(...)`), parsed a record at
   a time — the whole decompressed object is never held. `_read_s3_object_records` consumes the
   generator; a corrupt body still raises `_SkippableUnitError` (skip, not fatal), a failed
   `get-object` still raises `AwsCliError` before any line is yielded (so `_run_unit` retry is clean).
2. **Manual CloudWatch pagination** (`_read_cloudwatch_records`). `logs filter-log-events` is read in
   bounded pages (`--max-items=_CLOUDWATCH_PAGE_SIZE` (10000) + `--starting-token`) instead of one
   auto-paginated response that buffers a busy group's whole history. Each **page** is a `_run_unit`
   unit; the loop follows `NextToken` until absent.
3. **Disk-backed record stream** (`FlowLogRecordStream`). Both readers now `sink.extend(records)`
   into a re-iterable, file-backed sink (one NDJSON line per record) instead of returning/accumulating
   a list. `fetch_flow_log_records` returns the stream; the mapping layer re-reads it in its two
   passes, converting each dict to `FlowLogRecord` lazily. RAM holds only the bounded aggregates.

Files: `aws/collectors.py` (stream class, `_iter_gz_lines`, reader `sink=` refactor + CW paging),
`mapping/flowlogs.py` (two-pass over a re-iterable raw source; `_add_unrecognised_enis` builds a
compact per-ENI freq incrementally; `_infer_own_ip` now takes a freq dict; `_map_connections`
converts lazily), `mapping/builder.py` (pass the raw source, don't pre-materialize), `cli.py`
(`_write_outputs` closes the stream after writing), README + architecture §5.7, tests.

## 2. Interface contract for the next change
- **`FlowLogRecordStream`** (`aws/collectors.py`): `.extend(iterable_of_dict)` (duck-types the `list`
  API the readers use), `__iter__` re-reads the temp file (re-iterable — the two mapping passes both
  work), `.count`, `.close()` (idempotent; deletes the temp file; also called from `__del__`).
- **Readers changed signature**: `_read_s3_records(..., *, sink, resolver=..., ...) -> (fetched,
  skipped)` and `_read_cloudwatch_records(..., *, sink, fl_to_vpc=..., per_vpc=...) -> (fetched,
  skipped)`. They **stream into `sink`** and no longer return records. `FLOW_LOG_READERS`'s value
  type is now `Callable[..., tuple[int, int]]`. `collect_flow_log_records` (the legacy single-account
  path, test-only now) passes `sink=records` (a plain list) — it still returns a list.
- **`fetch_flow_log_records(...) -> (FlowLogRecordStream, per_vpc, resolved_archive)`** — returns the
  stream (not a list). The caller owns it: `_collect_flow_logs_role` stores it in
  `bundle["flow_log_records"]`; the fetch closes it on any error before propagating.
- **`map_flow_logs(graph, enis, flow_logs, allocations, records, ...)`** — `records` is now a
  **re-iterable of raw dicts** (a `list` or a `FlowLogRecordStream`), iterated twice, each dict
  converted with `FlowLogRecord.from_collected` lazily. `_add_unrecognised_enis` and
  `_map_connections` take `Iterable[dict]`.
- **`build_graph`** passes `collected.get("flow_log_records", [])` straight through (no
  `[FlowLogRecord.from_collected(x) ...]` list) and **does not** close it. **The CLI closes it** in
  `_write_outputs` via `_close_record_source` after outputs are written.

## 3. Decisions & rationale
- **Why disk spill and not a single-pass "fold into aggregates as read".** The mapping is inherently
  **two passes** over the records: pass 1 (`_add_unrecognised_enis`) discovers ENIs that are outside
  CloudTrail retention by the address that recurs across their records, and adds them to the
  inventory; pass 2 (`_map_connections`) resolves each record's **peer** *time-awarely* against the
  **complete** inventory (`inventory.resolve(peer_ip, rec.start)`), which now includes those
  unrecognised ENIs. A single streaming pass can't do this: (a) a record whose peer is an
  unrecognised ENI seen only later would wrongly become a `flow_peer`; (b) aggregating by
  `(home, peer_ip)` to fold connections loses the per-record `start`, breaking the reused-ASG-IP
  disambiguation (there's a dedicated test for it). So the records must be re-read. Options were
  re-download (expensive, and CloudWatch is non-deterministic between calls), hold in RAM (the OOM we
  are fixing), or **spill to disk** — the last preserves exact semantics and bounds RAM to
  O(distinct ENIs + distinct edges). The "never hold the full record list [in RAM]" goal is met; the
  records transit through a temp file, not memory.
- **`sink.extend()` duck-typing** keeps the legacy list path and the production stream path on one
  code path — the readers don't know which they're writing to.
- **`build_graph` must not close a caller-owned source.** First attempt closed it inside `build_graph`
  and broke `test_flow_log_mapping_is_deterministic` (it builds twice from one bundle → second build
  hit a deleted temp file). Closing is a lifecycle concern of the bundle's owner (the CLI), not of a
  pure transform. `__del__` is the safety net for any path that skips the explicit close.
- **CloudWatch retry idempotency.** Emitting a page's records only **after** `_run_unit` returns
  success means a retried page (same `--starting-token`) re-fetches but emits once. If emit were
  inside the retried closure, a transient mid-group failure would double-emit. Test:
  `test_cloudwatch_page_retry_does_not_double_emit`.
- **`--max-items` + `--starting-token`** (the CLI's client-side pagination) over `--no-paginate` +
  service `--next-token`: the former is the documented, portable control and its truncation marker is
  the CLI-injected `NextToken` in the response. It bounds each `run_aws` response to ~10k events.

## 4. Deviations from the plan
- We did **not** do a true single-pass streaming aggregation (see §3 — it would change the
  time-aware/unrecognised-ENI semantics). The `--max-flow-records` cap the user considered was
  explicitly declined.

## 5. Gotchas & surprises
- **`subprocess.run(capture_output=True)` buffers the entire stdout** of every `aws` call, and
  `json.loads` then builds a full second copy — so even with our streaming, a *single* unbounded
  `list-objects-v2` (a bucket with millions of keys) or an unpaged `filter-log-events` is a hard
  memory floor. Paging fixes CloudWatch; `list-objects-v2` is still one buffered response per source
  (bounded by key count, ~1 KB each — acceptable, but a future `--page-size` loop could bound it too).
- **Temp-file disk**: the stream spills to `TMPDIR` (system default). A very large run needs disk
  proportional to the records fetched (NDJSON, far smaller than the equivalent RAM dicts). On a
  fixed-allowance disk, point `TMPDIR` at a roomy volume. Documented in the README.
- `FlowLogRecord.from_collected` reads `InterfaceId`/`SrcAddr`/`DstAddr`/`SrcPort`/`DstPort`/
  `Protocol`/`Start`/`Action`/`LogGroup` — the exact keys `_parse_flow_log_message` emits and the
  NDJSON round-trips (ints/None preserved). Keep those key names in sync across the two.
- The `flow_bundle` fixture in `tests/test_flowlogs.py` runs `collect_all`, so it now yields a
  `FlowLogRecordStream` — the whole existing mapping suite already exercises the streaming path and
  proves output equivalence.

## 5b. Follow-up (same day) — the spill hit `ENOSPC` in production; harden the disk trade-off
Trading RAM for disk moved the failure mode: a real run crashed with a raw
`OSError: [Errno 28] No space left on device` from `FlowLogRecordStream.extend`. Fixes, in order of
how they defend:
- **Gzip the spill.** The NDJSON temp file is now written through `gzip.open(..., "wt")` and read
  back via `gzip.open(..., "rt")` (suffix `.ndjson.gz`). Flow records are extremely repetitive, so
  this cuts the footprint ~5-10x — the single biggest lever for actually fitting on disk. Gzip
  requires the write stream to be **finalized before reading** (an unterminated stream can raise at
  EOF), so the stream became **append-only until the first read**: `__iter__` calls `_seal()` (close
  the write handle) then re-reads. Our lifecycle already writes-all-then-reads-twice, so this is a
  no-op in practice. Gzip's header carries an mtime → the `.gz` bytes aren't reproducible, but we
  never hash the spill (only compare decompressed records / the sorted graph), so determinism holds.
- **Guard 1 — preflight.** `_read_s3_records` now lists+filters **all** sources first (phase 1),
  sums the in-scope object sizes, and calls `_preflight_spill_space(total, spill_dir)` **before any
  `get-object`**. If free space < total + margin (`_SPILL_FREE_MARGIN_BYTES` = 128 MiB, or 15%),
  it raises `FlowLogFetchError` immediately. Summed object size is a conservative proxy for the
  parsed+gzipped spill (usually smaller). Gated on `isinstance(sink, FlowLogRecordStream)` so the
  legacy in-RAM list path is unaffected; skipped when the estimate is 0 (no objects / no `Size`
  metadata — common in fixtures) so it never false-aborts a sizeless run.
- **Guard 2 — per-write.** `FlowLogRecordStream.extend` re-checks `shutil.disk_usage(...).free`
  before each batch (one S3 object or one CloudWatch page — a cheap, natural granularity, *not*
  per-record) and raises if it's below the margin. This catches concurrent disk pressure and covers
  CloudWatch (no up-front sizes for a preflight).
- **Backstop.** Any `OSError` from the write is still caught and re-raised as `FlowLogFetchError`
  with the same actionable message, so even if the heuristics miss, there's never a raw traceback.
- **`--spill-dir` flag** (+ `configure_spill_dir`, mirroring `configure_cache`) lets ops put the
  spill on a roomy volume; default stays `$TMPDIR`. All three guards' messages name
  `--spill-dir` / `--flow-log-days`.

Gotchas:
- **Don't `disk_usage` per record** — it's a syscall; do it per batch (per `extend`). Millions of
  per-record stat calls would be a real slowdown.
- **The margin can false-abort a tiny run on a nearly-full disk** (free < 128 MiB). That's an
  accepted, documented stance ("disk nearly full → refuse to spill"), and the message points at
  `--spill-dir`. Tune `_SPILL_FREE_MARGIN_BYTES` if it's too aggressive for an environment.
- **`shutil.disk_usage` can raise** (missing dir, permissions) → `_free_bytes` returns `None` and the
  pre-checks no-op; the write backstop still catches a real ENOSPC. A missing `--spill-dir` fails at
  `mkstemp` with an actionable `FlowLogFetchError`.
- Sandbox note: this repo's CI/sandbox has ~30 GiB free on `$TMPDIR`, so the 128 MiB margin doesn't
  trip the flow-log tests; guard tests mock `collectors._free_bytes` to simulate a full disk.

## 6. Known gaps / TODO
- `s3api list-objects-v2` and `cloudtrail lookup-events` are still single buffered responses (bounded
  by item count, not streamed). If a bucket's key count or CloudTrail event count ever dominates,
  page those too (same `--max-items`/`--starting-token` pattern).
- The temp file lives for the duration of `build_graph`; a `--stream-to <dir>` knob could let ops
  place it deliberately.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest -q                                   # 417 tests, offline
ruff check . && ruff format --check .

# Focused tests: tests/test_flowlog_streaming.py
#  - FlowLogRecordStream round-trip / re-iterate / count / close-deletes-file
#  - streaming gunzip parses header+records / no-header default layout / corrupt -> skippable / lazy
#  - CloudWatch pagination follows NextToken, per-page emit, per-VPC accounting, idempotent retry
#  - mapping via a stream == mapping via the equivalent list (output unchanged)
#  - gzip spill round-trip; ENOSPC -> actionable FlowLogFetchError (not a traceback)
#  - disk guards: preflight aborts before download when objects won't fit; per-write stops on low
#    space; --spill-dir honoured (unit + tests/test_cli.py::test_flow_logs_spill_dir_is_honoured)
# The whole tests/test_flowlogs.py suite runs the mapping through a stream (via the flow_bundle fixture).
```
