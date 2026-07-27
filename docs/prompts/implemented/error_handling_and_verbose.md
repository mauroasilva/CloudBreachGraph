## CHANGE REQUEST — Resilient flow-log fetch (CloudWatch + S3) + a `--verbose` command echo

**What I want**
Two related robustness/observability improvements to the flow-log fetch:

1. **Resilient record download for BOTH record sources.** Today a single failed AWS call aborts the
   entire `--flow-logs` run — a failed `s3api get-object` on one of thousands of S3 objects, or a
   failed `logs filter-log-events` on one CloudWatch group. Make per-unit failures **best-effort**:
   warn and skip a bad unit (one S3 object, one CloudWatch log group) and keep going — but **fail fast
   with an actionable message on *systemic* errors** (clock skew, expired/invalid creds, access
   denied) that would affect everything, so a run never silently produces a near-empty graph. This
   applies to **`_read_s3_records` and `_read_cloudwatch_records` equally** — both are mandatory.

2. **A `--verbose` flag** that prints each `aws` command as it runs (to **stderr**), so the user can
   see progress — especially during the long S3 download phase.

**Acceptance criteria**
- **S3:** one corrupt/missing object (`NoSuchKey`, bad gzip, transient) → warned + skipped; the run
  completes and builds a graph from the rest.
- **CloudWatch:** one failed/absent log group (`ResourceNotFoundException`, throttling) → warned +
  skipped; other groups still read; the run completes.
- **Either source:** a systemic error (`RequestTimeTooSkewed`, `ExpiredToken`/`InvalidToken`,
  `SignatureDoesNotMatch`, `AccessDenied`/`Forbidden`) → the run **stops** with a clear, actionable,
  source-aware message. Exit non-zero.
- A safeguard covering both sources: if an implausibly high fraction of fetch units fail (unrecognised
  systemic problem), abort rather than hand back a near-empty graph — reporting count + last error.
- `--verbose` prints the full `aws …` invocation for every command actually run (describe-*,
  cloudtrail, filter-log-events, list-objects-v2, and each get-object), to stderr, without polluting
  stdout or the written graph files.
- Read-only guarantee intact; stdlib only; deterministic graph output; `ruff`-clean; tests offline.

---

## Read first
- `docs/02_architecture.md` §3 (AWS commands), §5.7 (flow logs), §9 (error handling: "prefer a
  partial graph clearly flagging what's missing over aborting"). `docs/04_conventions.md` (hard rules).
- `docs/learnings/learnings_2026-07-25_flowlog-format-diagnostics.md` (the flow-log/S3 collector design).
- Source: `src/cloudbreachgraph/aws/runner.py`, `src/cloudbreachgraph/aws/collectors.py`,
  `src/cloudbreachgraph/cli.py`. Tests: `tests/test_runner.py` (mocks `subprocess`),
  `tests/test_collectors.py` (mocks `runner.run_aws` / `runner.download_object`), `tests/test_cli.py`.

## Current state (the exact seams)
- `aws/runner.py` is the **only** place that shells out to `aws`:
  - `run_aws(args, *, profile, region, cache_dir=None)` → parsed JSON; raises `AwsCliError(args, rc, stderr)`.
  - `download_object(args, dest, *, profile, region)` → writes a binary body to `dest` (S3 `get-object`);
    also raises `AwsCliError`. **Bug to fix:** it raises `AwsCliError(args, ...)` where `args` omits the
    `dest` positional, so the message isn't the real command — pass the full command (append `dest`).
  - Module-level config pattern already exists: `configure_cache()` / `_cache_dir`. **Mirror it for
    verbose** (`set_verbose(bool)` + module flag) rather than threading a param through every call.
- `aws/collectors.py`:
  - S3: `_read_s3_records` → `_read_s3_object_records(bucket, key, …)` → `_download_gz_lines(bucket, key, …)`.
    `_download_gz_lines` catches `(OSError, EOFError, gzip.BadGzipFile)` → `[]`, but **not**
    `runner.AwsCliError` (why one failed download aborts everything).
  - CloudWatch: `_read_cloudwatch_records(flow_logs, profile, region, since_epoch)` calls
    `runner.run_aws(["logs","filter-log-events", …])` per group — a failure there aborts too.
  - `_report_flow_log_records(...)` prints the one-line stderr diagnostic (extend it to report skipped units).
  - `collect_flow_log_records` dispatches to `FLOW_LOG_READERS` and can raise `FlowLogDestinationError`.
- `cli.py`: `build_parser()`, `_make_cache_reader()` (the `--from-cache` reader that replaces `run_aws`),
  `main()` (top-level `except`: `ConfigError`→2, `FlowLogDestinationError`→1, `AwsCliError`→1).

## Design guidance

### A. Resilient fetch — MANDATORY for both readers
Add a shared classifier (a small helper in `collectors.py`) that inspects an `AwsCliError`'s stderr and
returns "systemic" vs "skippable", used by **both** `_read_s3_records` (per S3 object) and
`_read_cloudwatch_records` (per log group):

- **Systemic → raise** (abort the run) with a tailored, source-aware, actionable message:
  - `RequestTimeTooSkewed` → "system clock is out of sync with AWS (>15 min); sync it
    (macOS: System Settings → General → Date & Time → 'Set time automatically') and re-run."
  - `AccessDenied`/`Forbidden`/`AuthorizationHeaderMalformed` → name the missing permission per source:
    S3 → "s3:ListBucket + s3:GetObject on <bucket>"; CloudWatch → "logs:FilterLogEvents on <log group>".
    (Treat access-denied as abort so an IAM gap is surfaced loudly, not silently partial.)
  - `ExpiredToken`/`ExpiredTokenException`/`InvalidToken`/`SignatureDoesNotMatch` → "credentials
    expired/invalid; refresh the profile's session and re-run."
  Use a dedicated exception (e.g. `FlowLogFetchError`) caught in `cli.main` → rc 1 (or reuse existing
  handling), message actionable and naming the bucket/object or log group.
- **Skippable (everything else — S3 `NoSuchKey` / corrupt gzip, CloudWatch `ResourceNotFoundException`,
  throttling, etc.) → warn to stderr (naming the S3 object or the log group) and skip that unit**,
  continuing with the rest.
- **Failure-rate safeguard (both sources):** track attempted vs failed units; if failures exceed a
  threshold (e.g. the first N consecutive all fail, or > ~50% overall), abort with "too many flow-log
  fetches failed (<failed>/<attempted>); last error: <…>" so an unrecognised systemic problem never
  yields a silent near-empty graph.
- Report skipped counts in the diagnostic, e.g. "… parsed P records (skipped K units: J S3 objects,
  L log groups)."
- Determinism note: skipping unreachable units makes output depend on reachability — inherent to
  best-effort and consistent with §9; document it. Tests must simulate the failures (never real timing).

### B. `--verbose`
- Add `--verbose`/`-v` (`store_true`) to `cli.py`; wire once via `runner.set_verbose(True)` (module-level
  flag mirroring `configure_cache`). `run_aws` and `download_object` print the full `aws …` command to
  **stderr** when verbose (stdout stays clean for "wrote …" and graph files). No credentials are in argv.
- Make `--from-cache` verbose too: `_make_cache_reader` should print which cached response it serves
  when verbose (that path swaps out the real `run_aws`, so the runner echo won't fire there).
- Optional: print elapsed time per command (helps spot slow S3 downloads); stderr-only.

## Hard rules (docs/04_conventions.md)
- Python 3.11+, full type hints, **stdlib only**, read-only. `ruff`-clean.
- Deterministic graph output (stderr logging/warnings must not affect the JSON/DOT/HTML).
- Tests fully offline — mock at the `runner` boundary (`run_aws`, `download_object`) or `subprocess`
  (as `tests/test_runner.py` does). Never hit the network.

## Tests to add
- **S3 resilience:** `download_object` raising `AwsCliError` with a *systemic* stderr
  (`RequestTimeTooSkewed`, `AccessDenied`) → aborts with the actionable message (assert message + rc at
  CLI level). A *per-object* error (`NoSuchKey`) or corrupt gzip → that object skipped, others parse, rc 0.
- **CloudWatch resilience:** `run_aws` for `logs filter-log-events` raising a *systemic* error → aborts;
  a *per-group* error (`ResourceNotFoundException`) with multiple groups → that group skipped, others read.
- **Safeguard:** a high failure rate across units trips the abort (both sources).
- **Verbose:** unit-test the runner echo with `subprocess` mocked (like `test_runner.py`): verbose on →
  `aws …` on stderr; verbose off → absent. CLI check that `--from-cache --verbose` echoes served commands.
- Keep the existing S3 reader test and CloudWatch record test green.

## Definition of done
- [ ] Resilient fetch implemented for **both** `_read_s3_records` and `_read_cloudwatch_records` via the
      shared classifier + failure-rate safeguard; `AwsCliError` message includes the full command.
- [ ] `--verbose` echoes every real command (incl. get-objects) and the `--from-cache` served responses.
- [ ] `pytest` passes offline with new tests; `ruff check` + `ruff format --check` clean.
- [ ] Verified offline: `cloudbreachgraph --from-cache tests/fixtures --flow-logs --verbose
      --output-dir /tmp/out` prints commands; simulated systemic errors abort cleanly for each source.
- [ ] Read-only + determinism preserved.
- [ ] Docs updated: `README.md` (flags table + best-effort fetch + `--verbose` in the flow-log section),
      `docs/02_architecture.md` (§5.7 resilience for both sources, §9 partial-graph note).
- [ ] `docs/learnings/learnings_<YYYY-MM-DD>_<slug>.md` written and committed with the code — capture the
      systemic-vs-skippable classifier, the per-source messages, the failure-rate safeguard, and the
      `set_verbose` module-flag pattern.

## Git
- Branch off the latest `main`. Commit in logical chunks; push. Do **not** open a PR unless asked.
