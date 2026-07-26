# Learnings — 2026-07-26 resilient-flowlog-fetch

Change: make the flow-log record fetch **best-effort with smart recovery** (mandatory for **both**
the S3 and CloudWatch readers), and add a `--verbose` command echo. Robustness + observability for
`--flow-logs`, where an account can hold thousands of S3 objects / CloudWatch groups and today a
single failed AWS call aborted the entire run.

## 1. What this change delivered

All in the three AWS-facing modules; no graph/model change, output stays byte-for-byte deterministic.

- **`aws/runner.py`**
  - `set_verbose(bool)` / `is_verbose()` + module `_verbose` flag (mirrors the existing
    `configure_cache`/`_cache_dir` pattern). `run_aws` and `download_object` now echo the full
    `aws …` command to **stderr** with a short `OK`/`NOT OK (exit N, Ts)` result and elapsed time,
    gated on verbose. Stdout and the graph files stay clean.
  - **Bug fixed:** `download_object` raised `AwsCliError(args, …)` omitting the `dest` positional;
    it now passes the **full** command (`[*args, str(dest)]`) so the message names the exact call.
  - `sso_login(profile)` — a new **interactive** runner entry. Unlike the other two it does **not**
    capture stdio (`subprocess.run(["aws","sso","login","--profile",p])` with inherited terminal),
    because SSO login prints a device code / opens a browser. Raises `AwsCliError` on non-zero so the
    caller can tolerate a per-profile failure. This is the **only** non-read command in the tool.

- **`aws/collectors.py`** — the resilience core (new section between `FlowLogDestinationError` and
  `collect_flow_log_records`):
  - `_classify_aws_error(stderr) -> _ErrorTier` — routes a failed unit into
    `CLOCK_SKEW | EXPIRED | SYSTEMIC | TRANSIENT | SKIPPABLE`. **Order matters** (skew, then expiry,
    then systemic, then transient, else skippable) so an `ExpiredToken` never falls through to skip.
  - `_run_unit(fetch, *, source, unit, iam_hint) -> _FetchOutcome` — the **shared retry wrapper**
    used by both readers. `fetch` is a zero-arg closure re-invoked from scratch on each retry (fresh
    subprocess ⇒ re-signs with a current timestamp). Tiers:
    - **CLOCK_SKEW:** call `_trusted_time_offset()`; offset beyond `_CLOCK_SKEW_TOLERANCE_S` (900s)
      ⇒ real clock problem ⇒ raise `FlowLogFetchError` (actionable "sync your clock", no retry);
      within tolerance **or `None` (unfetchable)** ⇒ treat as network ⇒ back off.
    - **TRANSIENT / skew-network:** `_RETRY_BACKOFF = (30, 60, 120)` — up to **3 retries**; exhausted
      ⇒ warn + skip.
    - **EXPIRED:** raise `CredentialsExpiredError` (propagates to `cli.main`).
    - **SYSTEMIC:** raise `FlowLogFetchError` with a source-aware IAM message.
    - **SKIPPABLE / `_SkippableUnitError`:** warn (naming the unit) + skip, return `ok=False`.
  - `_trusted_time_offset()` — stdlib trusted external clock: `_http_date_epoch` (HTTPS `Date`
    header via `urllib`, parsed with `email.utils.parsedate_to_datetime`) first, then `_sntp_epoch`
    (minimal SNTP UDP query via `socket`/`struct`, NTP epoch delta `2208988800`). ~5s timeout;
    returns `remote - time.time()` (positive ⇒ local behind), or `None` if neither can be reached.
  - `_FailureTracker` — per-source attempted/failed/streak; aborts (`FlowLogFetchError`) on
    `_FAILURE_STREAK_ABORT` (5) in a row, or `>_FAILURE_RATE_THRESHOLD` (0.5) of a `>=
    _FAILURE_MIN_SAMPLE` (4) sample. Stops a silent near-empty graph.
  - New exceptions: `CredentialsExpiredError`, `FlowLogFetchError` (both caught in `cli.main`),
    `_SkippableUnitError` (internal). `is_expired_error(exc)` — public helper the CLI uses.
  - Both readers refactored: `_read_s3_records` wraps the per-source **list** and each per-object
    **get** in `_run_unit`; `_read_cloudwatch_records` wraps each **group**. Both now return
    `(records, fetched, skipped)` (was `(records, fetched)`) — `FLOW_LOG_READERS` type + the dispatch
    loop in `collect_flow_log_records` + `_report_flow_log_records` updated to thread skip counts.
  - `_download_gz_lines` now **raises** `_SkippableUnitError` on `(OSError, EOFError, BadGzipFile)`
    instead of silently returning `[]`, so a corrupt object is warned + counted, not vanished. (A
    legitimately empty gzip still returns `[]` = a successful empty read.)

- **`cli.py`**
  - `--verbose` / `-v` (`store_true`), wired once via `runner.set_verbose(bool(args.verbose))` at the
    top of `main`. `_make_cache_reader` echoes `+ [cache] aws … -> <file>` when verbose.
  - `main` restructured: the live path goes through `_run_live_with_sso_retry(cfg, …)`, which catches
    an expired-token error (`CredentialsExpiredError` **or** an expired `AwsCliError` from anywhere,
    via `collectors.is_expired_error`), runs `_sso_login_all(cfg)` = `aws sso login` for **every
    distinct profile** in the config (tolerating per-profile failures), and **retries the run once**.
    Still expired ⇒ rc 1. New top-level `except FlowLogFetchError` ⇒ rc 1.

## 2. Interface contract for the next change

- **Reader signature changed:** `FLOW_LOG_READERS[type](flow_logs, profile, region, since_epoch)`
  now returns `tuple[list[dict], int, int]` = `(records, fetched, skipped)`. A new destination-type
  reader must return the third element.
- To reuse the resilience policy for a **new** per-unit fetch, wrap it in `_run_unit(...)` and feed a
  `_FailureTracker`. `_run_unit` handles tiers/backoff; the caller records `outcome` and reads
  `outcome.value` when `outcome.ok`.
- `collectors.CredentialsExpiredError` / `FlowLogFetchError` are the flow-log fetch's two abort/relogin
  signals to `cli.main`; `collectors.is_expired_error(exc)` classifies expiry for any exception.
- `runner.set_verbose` / `is_verbose` is the verbose seam; `runner.sso_login` is the third AWS mock
  boundary (besides `run_aws`, `download_object`) — tests patch it.

## 3. Decisions & rationale

- **One shared `_run_unit` for both readers** (not duplicated logic) — the acceptance criteria demand
  the tiers apply *equally and mandatorily* to S3 and CloudWatch. The unit granularity is one S3
  object (get) / one CloudWatch group (filter); the S3 list call and the SSO login are handled around
  it, not counted in the object tracker.
- **Clock-vs-network from a trusted clock, not a guess.** `RequestTimeTooSkewed` is ambiguous (bad
  local clock vs a slow/re-driven request). Asking NTP/HTTPS `Date` distinguishes them; a real >15m
  skew aborts (retrying is futile and slow), a transient one retries. **Unfetchable trusted time ⇒
  network** (fail open to retry) so an offline/locked-down box still recovers from a blip.
- **Backoff 30/60/120 with fresh subprocesses** — the retry must re-sign (new timestamp), so each
  attempt is a new `aws` invocation, not a re-parse.
- **SSO re-login lives in the CLI, not the collector** — the collector is role-agnostic and has no
  config; only `cli.main` knows the profiles. The reader just raises `CredentialsExpiredError`.
- **Expired detection also at the CLI top level** (`is_expired_error` on `AwsCliError`) — realistically
  an expired token hits the *first* network collector, long before the flow-log readers, so gating
  only on the reader-raised type would miss the common case.
- **Failure-rate safeguard** — best-effort skipping could otherwise hide a total outage as an
  empty-but-successful graph; the streak/rate abort makes "everything is failing" loud.
- **`set_verbose` mirrors `configure_cache`** — a module flag toggled once by the CLI, per the prompt.
  Verbose is stderr-only to keep stdout/graph output clean and deterministic.

## 4. Deviations from the plan

None material. The prompt offered "SNTP **or** HTTP-Date"; both are implemented (HTTPS `Date` first,
SNTP fallback) for robustness. Thresholds chosen: clock tolerance 900s; safeguard = 5-in-a-row or
>50% of ≥4 attempts.

## 5. Gotchas & AWS quirks

- **`--from-cache` only swaps `runner.run_aws`, not `download_object`.** The S3 record path calls
  `download_object` directly, so under `--from-cache` an S3 flow log with a *recent* `.gz` key would
  attempt a real subprocess. The shipped fixtures list only old-timestamped keys (filtered out), so
  the offline demo never downloads. Unchanged by this work, but worth knowing.
- **`InvalidClientTokenId` ≠ `InvalidToken`.** The classifier matches the substring `InvalidToken`;
  `InvalidClientTokenId` (a bad access key) does not contain it, so it isn't mis-tiered as expiry.
- **Tests must mock three things** to stay offline/fast: the `runner` boundary, `time.sleep`
  (`monkeypatch.setattr(time, "sleep", …)`), and `collectors._trusted_time_offset`. Never hits the
  network. See `tests/test_resilient_fetch.py`.
- **Module-global `_verbose` leak:** `cli.main` sets it every call, so it self-resets; unit tests use
  `monkeypatch.setattr(runner, "_verbose", …)` (auto-restored) and the resilient-fetch suite forces
  it off in an autouse fixture.

## 6. Known gaps / TODO

- Trusted-time is re-fetched on *each* skew failure within a retry loop (cheap, mocked in tests, but a
  real 3-retry skew loop makes up to 3 NTP/HTTPS calls). Could cache the first offset per run.
- S3 listing is still whole-prefix (the pre-existing large-bucket cost noted in the 2026-07-25
  learnings); resilience doesn't change that.
- The failure-rate thresholds are constants, not flags. If a user legitimately expects a high skip
  rate they'd have to edit the module. Consider a `--max-flow-log-failures` knob if it comes up.

## 7. How to verify

```bash
pip install -e '.[dev]'
pytest                       # 330 tests, fully offline
ruff check . && ruff format --check .

# Verbose offline demo — echoes every (cached) command to stderr; stdout stays the two "wrote" lines:
cloudbreachgraph --from-cache tests/fixtures --flow-logs --verbose --output-dir /tmp/out
```

Behaviour is covered offline by `tests/test_resilient_fetch.py` (classifier tiers; skew
network/real-clock/unfetchable; transient exhaustion; systemic abort; expired propagation; corrupt
gzip skip; CloudWatch parity; failure-rate safeguard for both sources), `tests/test_runner.py`
(verbose echo on/off + NOT OK; `download_object` dest in error; interactive uncaptured `sso_login`),
and `tests/test_cli.py` (expired → `aws sso login` for every profile + retry once; still-expired ⇒
rc 1; non-SSO profile tolerated; `--from-cache --verbose` echoes served commands).
