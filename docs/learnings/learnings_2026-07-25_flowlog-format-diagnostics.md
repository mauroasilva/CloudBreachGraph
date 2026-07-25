# Learnings — 2026-07-25 flowlog-format-diagnostics-and-s3

## 1. What this change delivered
Follow-up after a real-account test where `--flow-logs` produced **no `connects_to` edges / no
`flow_peer` nodes** in `graph.json` (the account's flow logs deliver to **S3**, which we didn't
read). All in `aws/collectors.py` + a runner helper; no model/graph change:

- **Per-destination-type dispatch.** `describe-flow-logs` gives each flow log's `LogDestinationType`;
  `collect_flow_log_records` now dispatches to `FLOW_LOG_READERS[type]` — `_read_cloudwatch_records`
  (`logs filter-log-events`) or `_read_s3_records` (`s3api list-objects-v2` + `get-object`). A type
  with **no reader** (e.g. `kinesis-data-firehose`, or a missing type) raises
  `FlowLogDestinationError`, caught in `cli.main` → rc 1. This is the "always pull from the right
  source, else throw" behaviour.
- **S3 reader.** Parses the `LogDestination` ARN → `(bucket, prefix)`, lists `.gz` objects modified
  within the 60-day window, downloads each via a **new read-only runner helper**
  `runner.download_object` (S3 bodies are gzip, not JSON, so they can't go through `run_aws`),
  gunzips, and parses. Each S3 object's **first line is the field-name header**, so the field index
  is read from it (falling back to default layout).
- **Custom-`LogFormat`-aware parsing.** `_field_index_from_format` derives field positions from a
  CloudWatch group's `LogFormat` (or an S3 header row); `_parse_flow_log_message(msg, group,
  field_idx)`. A format missing a required field (`interface-id`/`srcaddr`/`dstaddr`) → skip.
- **Stderr diagnostic** (`_report_flow_log_records`): `N config(s) [by destination]; fetched E
  event(s) from cloud-watch-logs, K object(s) from s3; parsed P flow record(s).` Plus a note if data
  was fetched but nothing parsed.

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
- **S3 listing cost.** `_read_s3_records` lists under the destination *prefix* and filters by
  `LastModified`; a busy bucket over 60 days can be a large list + many `get-object` downloads. A
  future optimisation could construct date-partitioned prefixes (`.../vpcflowlogs/<region>/<Y>/<M>/<D>/`)
  to narrow the listing — needs the source account id + region, which we don't currently derive.
- **`kinesis-data-firehose`** has no reader → raises. Add a reader + `FLOW_LOG_READERS` entry if a
  user needs it (would read from the Firehose delivery destination, typically S3 again).
- Diagnostic counts live in the collector; they aren't surfaced in `graph.json`/`meta`.
- `runner.download_object` is the second AWS mock boundary (besides `run_aws`); tests patch it.

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
