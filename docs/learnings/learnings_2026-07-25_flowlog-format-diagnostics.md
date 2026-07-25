# Learnings — 2026-07-25 flowlog-format-diagnostics

## 1. What this change delivered
Follow-up after a real-account test where `--flow-logs` produced **no `connects_to` edges / no
`flow_peer` nodes** in `graph.json` (flow records weren't coming through). Two robustness fixes in
`aws/collectors.py`, no model/CLI/graph change:

- **Custom-`LogFormat`-aware parsing.** The v2 record parser previously hard-coded the *default*
  field positions. `describe-flow-logs` returns each flow log's `LogFormat`; `_field_index_from_format`
  now derives field positions from that string (falling back to the default layout when absent), and
  `_parse_flow_log_message` takes the resulting `field_idx`. A format missing a required field
  (`interface-id`/`srcaddr`/`dstaddr`) returns `None` → that group is skipped rather than misread.
- **Stderr diagnostic** (`_report_flow_log_records`): one line — `N config(s) [by destination];
  queried G CloudWatch group(s); fetched E log event(s); parsed P flow record(s).` Plus a targeted
  note when flow logs go to **S3** (records not read) or when events were fetched but none parsed.

## 2. Interface contract / behaviour notes for the next session
- `collect_flow_log_records` now builds a per-group `field_idx` from `LogFormat` and passes it to
  `_parse_flow_log_message(message, log_group, field_idx)`. The third arg defaults to the default
  layout, so old call sites still work.
- The collector prints to **stderr** (via `sys`) — the first collector to do so. It's an opt-in
  (`--flow-logs`) path, so the noise is acceptable and the signal is high. Tests don't assert on it.
- Still **CloudWatch-only** for records. S3 record contents remain unread (see §6 / roadmap).

## 3. Decisions & rationale
- **Parse by `LogFormat`, not a fixed layout.** Custom flow-log formats are common and were the most
  likely silent cause of "zero flows" (wrong positions → `dstaddr` never matches an ENI IP → no
  edges). Deriving positions from the format the account actually uses fixes it generally.
- **Diagnose loudly.** The pipeline has ~5 places it can legitimately drop to zero (S3 destination,
  empty window, format mismatch, region/account scope, no matching ENI). A single stderr summary
  localises the failure without a debugger or new flags.
- **Skip, don't guess, on an unusable format.** If the required fields aren't locatable, skipping the
  group is safer than emitting garbage edges.

## 4. Deviations from the plan
- Collectors were previously "quiet"; this one now emits a stderr diagnostic. Deliberate, and scoped
  to the opt-in flow-logs path.

## 5. Gotchas / AWS quirks
- **Default v2 field positions** (0-indexed): version 0, account-id 1, interface-id 2, srcaddr 3,
  dstaddr 4, srcport 5, dstport 6, protocol 7, packets 8, bytes 9, start 10, end 11, action 12,
  log-status 13.
- `LogFormat` tokens are `${hyphenated-name}` (e.g. `${interface-id}`), mapped to our internal keys
  via `_FLOW_TOKEN_TO_KEY`.
- A CloudWatch log event's `message` **is** the space-delimited flow record.
- If the profile lacks `logs:FilterLogEvents` the run **errors** (non-zero exit surfaced) rather than
  silently returning nothing — so a *silent* empty result is not a permission problem.

## 6. Known gaps / follow-ups
- **S3-destined flow-log records are still not read** (would need `s3api list-objects-v2` +
  `get-object` + gunzip + parse). This is the single most likely reason a real account sees "zero
  flows", now called out explicitly in the diagnostic. Strong candidate for the next change.
- Diagnostic counts live in the collector; they aren't surfaced in `graph.json`/`meta`.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 256 tests, offline
ruff check . && ruff format --check .

cloudbreachgraph --from-cache tests/fixtures --flow-logs --output-dir /tmp/cbg-out
# stderr shows: "flow logs: 2 config(s) [1 cloud-watch-logs, 1 s3]; queried 1 CloudWatch group(s);
#   fetched 7 log event(s); parsed 6 flow record(s)." and a note that the S3 flow log isn't read.
```
Live triage for the "no flows" report: run with `--flow-logs` and read the stderr line —
`queried 0 groups` ⇒ logs go to S3 (or no CloudWatch flow logs); `fetched 0 events` ⇒ empty 60-day
window / wrong region-account; `parsed 0` of many ⇒ unrecognised format; `parsed N` but no edges ⇒
the record `interface-id`s don't match collected ENIs (scope mismatch).
