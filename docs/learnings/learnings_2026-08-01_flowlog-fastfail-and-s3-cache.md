# Learnings — 2026-08-01 flowlog-fastfail-and-s3-cache

## 1. What this change delivered
Three production-triage fixes after a real run reported
`fetched 20443 object(s) from s3; parsed 0 flow record(s)`:

- **Fast-fail on no usable records (S3).** `_read_s3_records` now aborts with `FlowLogFetchError`
  (caught in `cli.main` → non-zero exit) once `_FLOW_LOG_PROBE_OBJECTS` (25) objects download but
  yield **zero** usable flow records — instead of downloading the (tens of thousands) rest. Any single
  parsed record disables the probe. Message names a sample key and the likely cause.
- **S3 object cache.** `runner.download_object` now caches downloaded S3 object bodies under
  `<cache-dir>/s3-objects/<sha256(bucket\0key)>.bin` when `--cache-dir` is set, reusing a copy younger
  than `_OBJECT_CACHE_TTL_SECONDS` (30 days — flow-log objects are immutable) with **no** re-download.
  Atomic store (temp + `replace`), best-effort (a cache write failure never breaks the fetch).
- **Parsing "bug" was not a bug.** The sample line
  `2 062317582477 eni-0bb770af8ddc25a3e - - - - - - - … NODATA` is a **NODATA** record: `srcaddr`/
  `dstaddr` are `-`, so `_parse_flow_log_message` correctly drops it (no connection to map). The
  header is the standard v2 layout and parses fine; 0 records means the sampled objects are all
  NODATA. No parser change — the fast-fail + a clearer abort message are the real remedy.

## 2. Interface / behaviour notes
- `runner.download_object` gained cache behaviour gated on the existing `_cache_dir`
  (`configure_cache`/`--cache-dir`). New helpers `_object_cache_path`/`_cache_fresh`/`_store_in_cache`.
  It extracts `bucket`/`key` from the `--bucket=`/`--key=` args (so the key is stable) — no signature
  change, so all callers/tests are unaffected.
- New collectors constant `_FLOW_LOG_PROBE_OBJECTS = 25` and a raise inside the S3 per-object loop.
  The abort is **per S3 reader** (that's where the download waste is); CloudWatch is bounded by group
  count (few API calls) so it isn't probed.

## 3. Decisions & rationale
- **`--cache-dir` (not a new flag)** carries the S3 object cache: it's what the user already reached
  for, and object bodies sit beside the captured JSON. A dedicated TTL cache (per the earlier caching
  prompt) was never implemented; this is the minimal, expected fix.
- **Probe of 25, not literally 1.** A single NODATA object is legitimate, so failing on the first
  would false-positive constantly; 25 is a representative sample that still caps waste at 25 downloads.
  Caveat: S3 keys sort by date, so the probe samples the *earliest* objects — a wholly-idle first
  period could abort early; the message tells the user to check TrafficType/format or narrow the
  window, and the constant is easy to bump.
- **Immutable ⇒ long TTL.** Flow-log S3 objects never change once written, so 30 days is safe; the TTL
  is really just a cleanup bound.

## 4. Deviations from the plan
The full multi-tier TTL cache (S3 30d + per-command AWS cache) from the earlier caching prompt is
**not** implemented — only the S3 object cache tied to `--cache-dir`. The AWS-response TTL cache
remains a follow-up.

## 5. Gotchas
- `download_object`'s mock boundary is unchanged, but the cache means a mocked/real second call with
  the same bucket/key won't hit the subprocess — tests that count calls must account for it (see
  `test_download_object_caches_and_reuses_with_cache_dir`, which resets `configure_cache(None)` in a
  `finally` so cache state doesn't leak).
- NODATA vs unparseable are different: NODATA parses fine (0 usable); an unrecognised format fails to
  parse. The fast-fail treats both as "0 usable after the probe" — that's intentional (both waste
  downloads), and the message covers both causes.

## 6. Known gaps / follow-ups
- Per-command AWS-response TTL cache (the other half of the caching prompt).
- Optionally surface the **interface-id** from NODATA records as a "known ENI existed" node (they
  carry the ENI id even with no addresses) — currently dropped entirely.
- The probe samples the earliest S3 keys; a spread/random sample would be more robust for
  early-idle-then-active flow logs.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # offline
ruff check . && ruff format --check .

# Fast-fail: an all-NODATA S3 flow log aborts after ~25 downloads, not thousands (test:
#   test_s3_all_nodata_fast_fails_without_downloading_everything).
# Cache: a second cloudbreachgraph --flow-logs --cache-dir ./cache/ reuses S3 objects (verbose shows
#   "+ (cache hit) aws s3api get-object ..."); test_download_object_caches_and_reuses_with_cache_dir.
```
