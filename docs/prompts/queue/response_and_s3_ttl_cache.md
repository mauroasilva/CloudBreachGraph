## CHANGE REQUEST — AWS response cache (TTL-based) + reconcile the S3-object cache

**Status / why this exists**
A first slice of the original "freshness-aware, TTL-based caching" request has already
shipped: the **S3 flow-log object cache** (30-day TTL, immutable objects). But it landed
tied to the existing `--cache-dir` flag (`<cache-dir>/s3-objects/`), not as the standalone,
default-available cache the original request envisioned, and the **AWS command/response
cache** — the larger half — was never built. This prompt finishes the job.

**What I want**
1. **AWS command (response) cache** — cache `aws` JSON responses on disk with **per-command
   TTLs chosen by how volatile each command's data is** (table below; the requested floor is
   24h). On a live run, if a fresh entry exists, reuse it and make **no** AWS call; otherwise
   fetch live and (re)write the entry. This is the missing half and the biggest repeat-run win.
2. **Reconcile the S3-object cache** with the response cache so both are governed by one
   coherent, documented caching model (one opt-in surface, one cache root, consistent
   bypass/override semantics) rather than the S3 cache being quietly bolted onto `--cache-dir`.

**Acceptance criteria**
- Two ENIs sharing a VPC never cause the same `describe-*` / `list-objects-v2` / log-group
  fetch (or the same flow-log S3 object) to be fetched twice — within a run *and* across runs
  within the TTL. (Within a single run it's already deduped by source; this adds the
  cross-run/TTL layer.)
- Each cached AWS command respects its TTL; expired entries are re-fetched.
- Enabling the cache never changes graph output for the same underlying data (determinism holds).
- `sts get-caller-identity` is **never** served from cache.
- The read-only guarantee is intact; stdlib only; `ruff`-clean; tests fully offline.

---

## Read first (only what's relevant)
- `README.md` (the flags table, the "Offline: build from cached JSON" and "Flow-log analysis"
  sections, and the `--cache-dir` prose that now also documents the S3-object cache).
- `docs/02_architecture.md` §3 (AWS commands), §5.7 (flow logs — the S3 reader + the
  `<cache-dir>/s3-objects/` cache note), §9 (read-only / error handling).
- `docs/04_conventions.md` (hard rules + the mandatory learnings-file protocol).
- `docs/learnings/learnings_2026-08-01_flowlog-fastfail-and-s3-cache.md` — how the S3-object
  cache was implemented (keyed by sha256 of `bucket\0key`, 30-day mtime TTL, atomic
  temp+`os.replace`, tied to `--cache-dir`) and the "per-command AWS TTL cache" follow-up it
  explicitly deferred.
- Source: `src/cloudbreachgraph/aws/runner.py` (`run_aws`, `download_object`, `configure_cache`,
  `_cache_dir`, `_object_cache_path`/`_cache_fresh`/`_store_in_cache`), `src/cloudbreachgraph/cli.py`.

## Current state you must not break
- `aws/runner.py` is the **only** place that shells out to `aws`. It exposes:
  - `run_aws(args, *, profile, region, cache_dir=None)` → parsed JSON.
  - `download_object(args, dest, *, profile, region)` → writes a binary (gzip) body to `dest`.
  - **Two distinct existing mechanisms — keep them straight:**
    - **Capture/replay:** `configure_cache()` / `_cache_dir` / `_cache_key()` back the
      `--cache-dir` (write raw JSON for replay) and `--from-cache` (offline replay, no live
      calls) flags. Do **not** overload or break these.
    - **S3-object cache:** already implemented in `download_object` via `_object_cache_path`
      (keyed by `bucket`+`key` under `<cache-dir>/s3-objects/`), `_cache_fresh`
      (`_OBJECT_CACHE_TTL_SECONDS = 30*86400`), `_store_in_cache` (atomic). Currently only
      active when `--cache-dir` is set.
- Value-carrying `aws` flags are passed in `--flag=value` form (e.g. `--log-group-name=...`,
  `--start-time=...`, `--bucket=...`, `--prefix=...`).

## Design guidance

### A. Where the cache lives
- Put the response-cache logic in a small new module `src/cloudbreachgraph/aws/cache.py`
  (keep `runner.py` small/single-purpose); have `run_aws` consult it. Reuse the same module
  for the S3-object side so there is **one** cache implementation, not two.
- **Default cache root:** the XDG cache dir — `$XDG_CACHE_HOME/cloudbreachgraph/` (fallback
  `~/.cache/cloudbreachgraph/`), with `aws/` (responses) and `s3/` (objects) subdirs. Use
  **file mtime** for age; write atomically (temp file + `os.replace`) so a crash can't leave a
  half-written entry.
- **Reconcile the S3 cache:** move it onto this same root/opt-in surface so it no longer
  depends on `--cache-dir`. Preserve the shipped behavior (30-day TTL, immutable, atomic) —
  this is a relocation + unification, not a rewrite. If you keep a `--cache-dir` fallback for
  back-compat, document it; the primary path should be the unified cache.

### B. Cache key — the critical part
The response-cache key must include the **service+subcommand**, the **profile**, the
**region**, and the **identifying** value-flags (`--log-group-name`, `--bucket`, `--prefix`),
but must **EXCLUDE volatile/time-window flags** — especially **`--start-time`**, which is
computed as `now − <window>d` and therefore changes every run. If `--start-time` is in the key
the cache hit rate is **0%**. Also exclude pagination tokens. Store the key as a hash (arg
lists get long / contain unsafe chars). **Add a test that two runs with *different*
`--start-time` but identical everything-else hit the same entry.**

S3 objects stay keyed by `s3://<bucket>/<key>` (immutable content; profile/region gate access,
not content), as already implemented.

### C. Bypass & safety rules
- `sts get-caller-identity` must **never** be cached (it's the account/credential safety check
  and is cheap).
- The TTL cache must be a **no-op under `--from-cache`** (that path swaps `run_aws` entirely, so
  it's naturally bypassed — verify with a test).
- Never let a cache read cause a mutating call (it can't — reads only — but keep the invariant).

### D. CLI surface
- Add an opt-in `--cache` flag. Recommend **off by default** to preserve today's always-fresh
  behavior for an audit tool (you may argue the immutable S3-object cache should be
  on-by-default — justify whichever you choose; note the S3 cache is currently gated on
  `--cache-dir`, so changing its trigger is a deliberate, documented behavior change).
- Add a `--no-cache` / `--refresh` bypass, an overridable cache dir, and a way to override TTLs
  (a `--cache-ttl` knob or documented config).
- Update the `--from-cache` and `--cache-dir` docs so all three mechanisms (capture/replay,
  offline replay, TTL cache) are clearly distinguished.

## Proposed per-command TTLs (by volatility — adjust with justification)
| TTL | Commands | Why |
|-----|----------|-----|
| **30 days** | S3 flow-log **objects** (`s3api get-object` bodies) | immutable once written |
| **7 days** | `ec2 describe-vpcs`, `describe-subnets`, `describe-flow-logs`, `describe-nat-gateways`, `describe-vpc-endpoints`, `describe-route-tables`, `elbv2/elb describe-load-balancers` | stable config/topology, rarely changes |
| **24 h** (requested floor) | `ec2 describe-network-interfaces`, `describe-instances`, `describe-security-groups`, `cloudtrail lookup-events`, `logs filter-log-events`, `s3api list-objects-v2` | more volatile — ENIs/instances churn, record/listing data accrues continuously; 24h bounds staleness |
| **never cached** | `sts get-caller-identity` | account/credential verification must be live |

Implement the TTL policy as an explicit map keyed by `(service, subcommand)` (mirroring how
`ROLE_COLLECTORS` is data, not control flow), with a sane default TTL for anything unlisted.

## Hard rules (from docs/04_conventions.md)
- Python 3.11+, full type hints, stdlib only (no new required runtime dependency).
- Read-only: only `describe-*`/`list-*`/`get-*`/`head-*` + the existing `sts` /
  `cloudtrail lookup-events` / `logs filter-log-events` reads, and the error-gated
  `aws sso login`. The cache must never cause a mutating call.
- Deterministic graph output; `ruff check` + `ruff format --check` clean.
- Tests are fully offline — mock at the `runner` boundary (`run_aws` and `download_object`).

## Tests to add (offline)
- Response-cache miss → live call made + entry written; second call within TTL → **no** live
  call, same result.
- Entry older than TTL → re-fetched (mock the clock / mtime).
- **`--start-time` excluded from the key** (two runs, different start-time, one shared entry).
- The motivating case: several ENIs in one VPC → the shared S3 object / log group /
  `list-objects-v2` fetched once across runs within TTL.
- `sts get-caller-identity` is never served from cache.
- `--from-cache` still makes zero live calls and ignores the TTL cache.
- S3-object cache hit copies the cached body to `dest` and parses identically (keep the
  existing `test_download_object_caches_and_reuses_*` green after the relocation).

## Definition of done
- [ ] Response cache implemented per the TTL table, opt-in and overridable, atomic writes; S3
      cache relocated onto the same unified cache root/surface (behavior preserved).
- [ ] `--cache-dir` / `--from-cache` semantics unchanged; `sts` never cached; determinism preserved.
- [ ] `pytest` passes offline with new tests; `ruff` clean.
- [ ] Verified end-to-end: `cloudbreachgraph --from-cache tests/fixtures --flow-logs --cache
      --output-dir /tmp/out` — confirm cache hits on a second run via the stderr diagnostic.
- [ ] Docs updated: `README.md` (flags + a "Caching" section distinguishing the three
      mechanisms), `docs/02_architecture.md` (§3/§5.7 + a short cache design note), IAM notes
      unchanged.
- [ ] Write `docs/learnings/learnings_<YYYY-MM-DD>_<slug>.md` (per docs/04_conventions.md) and
      commit it with the code. Capture the cache-key canonicalization (esp. the `--start-time`
      exclusion), the TTL policy + rationale, the bypass rules, and how the S3 cache was
      reconciled onto the unified root.

## Git
- Branch off the latest `main`. Commit in logical chunks; push. Do **not** open a PR unless asked.
