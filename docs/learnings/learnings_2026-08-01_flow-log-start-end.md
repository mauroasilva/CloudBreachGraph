# Learnings — 2026-08-01 flow-log-start-end

## 1. What this change delivered
An **explicit flow-log record window** alongside the existing day-count window:

- `--flow-log-start <timestamp>` — analyse flow-log records from that time (to now), instead of
  `--flow-log-days`. Optional `--flow-log-end <timestamp>` bounds the end. Timestamps are ISO-8601
  (`2026-05-01`, `2026-05-01T12:00:00Z`, `+00:00`, or naive → assumed UTC) or bare epoch seconds.
- `--flow-log-start` and `--flow-log-days` are **mutually exclusive** (argparse group);
  `--flow-log-end` requires `--flow-log-start`; `end` must be after `start`.
- Files touched: `aws/collectors.py` (range state + bounds + reader plumbing + meta),
  `cli.py` (flags + `_parse_timestamp` + `_configure_flow_log_window`), `mapping/builder.py`
  (meta guard), `README.md`, `docs/02_architecture.md §5.7`, tests.

## 2. Interface contract / how it threads
- New collectors knobs (mirror `set_flow_log_window` / `configure_cache` — module globals, so the
  `collect_x(profile, region)` contract is untouched):
  - `set_flow_log_range(start_epoch, end_epoch=None)` — `start=None` clears back to days mode;
    `end=None` means "up to now".
  - `_flow_log_window_bounds() -> (start_epoch, end_epoch|None)` — explicit range wins, else
    `now − _flow_log_window_days`.
- `collect_flow_log_records` computes `(since_epoch, until_epoch)` and passes **both** to the readers.
  **Reader signature changed** to `(flow_logs, profile, region, since_epoch, until_epoch)` and the
  `FLOW_LOG_READERS` Callable type updated. CloudWatch adds `--end-time` when `until_epoch` is set;
  S3 `_list_s3_flow_log_keys` gained an `until_epoch` param and filters `LastModified` on both ends.
- Meta: in range mode `collect_all` records `flow_log_start` / `flow_log_end` (ISO via `_iso_utc`)
  and **omits** `flow_log_window_days`; days mode records `flow_log_window_days` as before.

## 3. Decisions & rationale
- **CloudTrail history is untouched by the range.** The 90-day CloudTrail lookback
  (`_cloudtrail_lookback_days`, always 90) reconstructs historical ENIs regardless of the record
  window, so a narrow/old record range still resolves peers correctly. An explicit start older than
  90 days can't be reconstructed from CloudTrail (retention cap) — those ENIs fall to the
  unrecognised-ENI / `cloudbreachgraph-merge` path.
- **Determinism preserved.** In range mode the meta timestamps are the *user-supplied* start/end
  (fixed), not wall-clock. Days mode keeps the deterministic int. `--flow-log-end`-less (open) end is
  stored as `null`, never a wall-clock "now".
- **Meta double-write gotcha (fixed).** `mapping/builder.py` independently `setdefault`s
  `flow_log_window_days` (for direct `build_graph` calls). It now skips that when `flow_log_start` is
  already in meta, so range mode doesn't get a misleading `flow_log_window_days: 60` stamped over it.
- **Timestamp parsing** lives in the CLI (`_parse_timestamp`): digits → epoch; else
  `datetime.fromisoformat` (with `Z`→`+00:00`), naive → UTC. Clear `ValueError` message on bad input,
  surfaced as exit 2.

## 4. Deviations from the plan
None — additive feature; days mode is byte-for-byte unchanged.

## 5. Gotchas
- The reader signature change ripples to any caller/registry; the `FLOW_LOG_READERS` Callable type
  had to grow the `float | None` param or ruff/type readers drift.
- Module-global window state leaks across tests: range tests **reset** with
  `set_flow_log_range(None)` / `set_flow_log_window(FLOW_LOG_MAX_LOOKBACK_DAYS)` in a `finally`.
- `--from-cache` output is unaffected by the window (the cache reader ignores the time flags and the
  S3 fixture lists no objects), so range behaviour is tested at the **collector** level (asserting
  `--start-time`/`--end-time` args and `_list_s3_flow_log_keys` filtering) plus **CLI meta**.

## 6. Known gaps / follow-ups
- An explicit start > 90 days ago still can't reconstruct ENIs from live CloudTrail — expected; use
  the `cloudbreachgraph-merge` CloudTrail-file input for older history.
- CloudTrail lookups aren't bounded to the range's end (they always run to now over 90 days); fine,
  since ENI lifetimes are what's needed, but a future optimisation could pass `--end-time` there too.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 388 tests, offline
ruff check . && ruff format --check .

cloudbreachgraph --from-cache tests/fixtures --flow-logs \
  --flow-log-start 2026-05-01T00:00:00Z --flow-log-end 2026-06-01T00:00:00Z --output-dir /tmp/out
#   graph.json meta -> flow_log_start/flow_log_end set, no flow_log_window_days, cloudtrail_window_days 90.
# Error paths: --flow-log-end without --flow-log-start (rc 2); end <= start (rc 2); bad timestamp (rc 2);
#   --flow-log-days with --flow-log-start (argparse "not allowed with").
```
