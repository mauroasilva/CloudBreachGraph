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

---

## UPDATE (2026-08-03) — read before designing the cache; two changes shipped since this was queued

Since this prompt was written, two flow-log changes landed that **materially change how the
cache must be built**. Do not design the cache as "one `run_aws` call → one JSON file" — that
model is now wrong in two ways and would (a) reintroduce a stale-data bug and (b) reintroduce an
OOM. Read `docs/learnings/learnings_2026-08-03_flowlog-bounded-memory.md` and
`docs/learnings/learnings_2026-08-02_cross-account-flow-logs.md` first.

**(1) The existing capture/replay key is already broken — fix it as part of "one coherent model".**
`runner._cache_key(args)` keeps only args that do **not** start with `-`, so it **drops every
value-flag**. Every `logs filter-log-events` (any `--log-group-name`), every `s3api
list-objects-v2` (any `--bucket`/`--prefix`), and every `cloudtrail lookup-events` (any
EventName) therefore collides onto a **single** file (`logs-filter-log-events.json`, etc.),
last-write-wins. Consequences you must fix, not preserve:
  - a `--cache-dir` capture is **lossy** for any such command with >1 distinct value (only the
    last group/bucket/EventName survives);
  - a later `--from-cache` replay then serves that one response for **all** of them → **wrong /
    incomplete results**. The `--from-cache` reader (`cli._make_cache_reader`) only partially
    compensates via a hand-rolled `_cache_variant` for `lookup-events`' EventName; nothing
    disambiguates log group or bucket/prefix.
  The new unified key (§B) must be applied to **all three** mechanisms — align `_cache_key` and
  `_make_cache_reader` with it — so capture/replay becomes faithful too. This is squarely inside
  "one coherent, documented caching model."

**(2) `logs filter-log-events` is now MANUALLY PAGINATED — one logical query is many `run_aws`
calls.** To bound memory, the CloudWatch reader now pages with `--max-items` + `--starting-token`
(`_CLOUDWATCH_PAGE_SIZE`), issuing **N `run_aws` calls per log group** and streaming each page's
records into a disk-backed sink. Records also stream (S3 gunzipped line-by-line via
`_iter_gz_lines`; parsed records spilled to a `FlowLogRecordStream`, never a giant in-RAM list).
This forces two cache rules (see the new §E):
  - **Cache at logical-query granularity, not per `run_aws` call.** Excluding the pagination
    token from the key (which §B correctly requires) while caching **per page** would map every
    page of a group to the same key and corrupt the cache (page 1 served for page 2). Cache the
    **aggregated** result of the whole paginated query under one token-free key.
  - **Do not reintroduce the OOM.** A record/listing result can be millions of rows. Caching it as
    one JSON blob and `json.loads`-ing it back on a hit would balloon RAM — the exact failure the
    bounded-memory work just fixed. The cache artifact for record/listing commands must be
    **streamable** (append-per-page NDJSON, read back lazily into the same `sink.extend(...)`
    pipeline), mirroring how S3 object bodies are already cached as gz files and streamed.

Also note the cross-account split (§(1) learning): flow-log **config/CloudTrail/CloudWatch** run in
the **network** account and **S3 object I/O** in the **archive** account, and the archive account
may be **auto-resolved** (primary→AccessDenied fallback). So the cache key's **profile** component
must reflect the account each command actually ran under (network vs. resolved-archive), and an S3
object's cache identity is still `s3://<bucket>/<key>` (content is account-independent; the profile
gates access, not content).

**Acceptance criteria**
- Two ENIs sharing a VPC never cause the same `describe-*` / `list-objects-v2` / log-group
  fetch (or the same flow-log S3 object) to be fetched twice — within a run *and* across runs
  within the TTL. (Within a single run it's already deduped by source; this adds the
  cross-run/TTL layer.)
- Each cached AWS command respects its TTL; expired entries are re-fetched.
- Enabling the cache never changes graph output for the same underlying data (determinism holds).
- `sts get-caller-identity` is **never** served from cache.
- **No value-flag collision:** distinct `--log-group-name` / `--bucket`+`--prefix` / EventName get
  distinct entries — within a run and across `--cache-dir`/`--from-cache`. (Regression for the
  `_cache_key` bug in the UPDATE; today two log groups overwrite one cache file.)
- **Paginated queries cache at logical-query granularity** (all pages under one token-free key), and
  a cache hit on a large `filter-log-events` group **streams** (does not `json.loads` the whole
  result into RAM) — the cache preserves the bounded-memory guarantee, feeding the same
  `sink.extend(...)` path as a live fetch.
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
- **`docs/learnings/learnings_2026-08-03_flowlog-bounded-memory.md`** — streaming gunzip, the
  disk-backed `FlowLogRecordStream`, and **manual CloudWatch pagination**. The cache must not undo
  any of this (see the UPDATE above and §E).
- **`docs/learnings/learnings_2026-08-02_cross-account-flow-logs.md`** — the two-account split and
  archive-account auto-resolution that determine which profile each command runs under (cache-key
  `profile` component).
- Source: `src/cloudbreachgraph/aws/runner.py` (`run_aws`, `download_object`, `configure_cache`,
  `_cache_dir`, `_cache_key`, `_object_cache_path`/`_cache_fresh`/`_store_in_cache`);
  `src/cloudbreachgraph/aws/collectors.py` (`_read_cloudwatch_records` pagination, `_iter_gz_lines`,
  `FlowLogRecordStream`, `FLOW_LOG_READERS`, the `sink.extend(...)` reader contract);
  `src/cloudbreachgraph/cli.py` (`_make_cache_reader`, `_cache_variant`, `_close_record_source`).

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
  `--start-time=...`, `--bucket=...`, `--prefix=...`, `--max-items=...`, `--starting-token=...`).
- **The `run_aws` JSON dump is write-only.** `run_aws` **always** shells out to `aws` and only
  *writes* the response to `--cache-dir`; it never *reads* it. So `--cache-dir` does not save a
  single API call on a re-run — only `download_object` reuses fresh S3 bodies, and `--from-cache`
  is the only read-from-disk path. The new TTL cache is what adds the "reuse on a live re-run"
  behavior; wire it into `run_aws` (and the paginated readers, §E) without breaking the
  write-only capture dump semantics of `--cache-dir`. **(Design fix: §F.)**
- **`--from-cache` does not serve `s3api get-object`.** `_make_cache_reader` swaps only `run_aws`,
  not `download_object`, so an offline `--from-cache --flow-logs` with **S3** destinations would
  hit the network on every object (today's tests avoid it with empty S3 listings). The unified
  cache should let `download_object` be served from the object cache under `--from-cache` too, so a
  captured S3-backed flow-log run replays fully offline. **(Design fix: §F.)**

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
The response-cache key must include the **service+subcommand**, the **profile** (the account the
command actually ran under — network vs. resolved-archive, per the cross-account split), the
**region**, and the **identifying** value-flags (`--log-group-name`, `--bucket`, `--prefix`, and
`lookup-events`' `--lookup-attributes` EventName), but must **EXCLUDE volatile flags** — the
time-window flags **`--start-time`**/**`--end-time`** (computed as `now − <window>d`, so they
change every run — in the key the hit rate is **0%**) **and the pagination flags**
`--max-items`/`--starting-token`/`--next-token`. Store the key as a hash (arg lists get long /
contain unsafe chars). **Add tests that (a) two runs with *different* `--start-time` but identical
everything-else hit the same entry, and (b) two *different* `--log-group-name` (or `--bucket`/
`--prefix`, or EventName) get *distinct* entries** — the second is the regression test for the
`_cache_key` value-flag collision described in the UPDATE.

> **Excluding the pagination flags is only correct if you cache the *aggregated* logical result,
> not each `run_aws` page** — otherwise every page hashes to the same key and overwrites the last.
> This is the whole point of §E: the cache unit is the **logical query**, not the `run_aws` call.

Apply this **same canonicalization to the capture/replay path** (`runner._cache_key` and
`cli._make_cache_reader`) so `--cache-dir` captures are faithful and `--from-cache` replays each
group/bucket/EventName distinctly. Retire the ad-hoc `_cache_variant` in favour of the unified key.

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

### E. Pagination + streaming — the accuracy-critical section (added 2026-08-03)

The cache must respect the bounded-memory design that just shipped, or it will silently corrupt
data and/or re-OOM. Two hard requirements:

- **Cache the LOGICAL query, not the `run_aws` call.** `logs filter-log-events` is read as
  **multiple** `run_aws` calls per group (manual `--max-items`/`--starting-token` paging). Since
  §B (correctly) excludes the pagination token from the key, all pages of a group share one key —
  so you must cache the **aggregated** result of the whole paginated query under that single key,
  not each page. Two acceptable shapes:
  1. **Wrap the pagination loop** in the cache: on a hit, replay the aggregated result and issue
     **zero** page calls; on a miss, page live and append each page to the cache entry as it
     arrives, then mark the entry complete. Only mark complete after the **final** page (no
     `NextToken`) — never leave a half-paged entry that a later run would treat as the full result.
  2. Give the record/listing readers their own cache layer keyed by the logical query, and keep the
     naive per-`run_aws` cache only for genuinely single-call commands (the `describe-*` config).
  (Today `s3api list-objects-v2` and `cloudtrail lookup-events` are still single `run_aws` calls —
  the CLI auto-aggregates their pages — so they cache at the `run_aws` level fine **once the key
  includes their identifying flags**. Only `filter-log-events` is manually paged. But design the
  cache so that if `list-objects-v2`/`lookup-events` are later manually paged for memory, they slot
  into the same logical-query caching without a redesign.)

- **Do NOT reintroduce the OOM.** A `filter-log-events` group can be millions of events. Caching it
  as one JSON blob and `json.loads`-ing it back on a hit would rebuild the exact multi-GB in-RAM
  structure the bounded-memory work eliminated. So the cache **artifact for record/listing
  commands must be streamable**:
  - store `filter-log-events` results as **append-per-page NDJSON** (or per-event), and on a hit
    **read them back lazily**, feeding the existing `sink.extend(...)` contract
    (`FlowLogRecordStream`) the live reader uses — never a single `json.loads` of the whole file;
  - a cache hit and a live fetch must be **indistinguishable downstream** (same sink, same order),
    so determinism holds and memory stays bounded on both paths;
  - S3 object bodies are already cached as gz files and streamed via `_iter_gz_lines` — that is the
    exemplar; keep it, and make `--from-cache` serve them through the same object cache so an
    S3-backed flow-log run replays fully offline (see "Current state").
  - `list-objects-v2`'s cached artifact (key+size list) is smaller, but still read it without
    materializing a second full copy.

  A useful litmus test: **a cache hit on a huge `filter-log-events` group must not raise peak RSS
  meaningfully above a cache miss.** At minimum, assert the cache-read path goes through the sink
  (not a whole-file `json.loads`).

### F. Three distinct read/write behaviours — make them coherent (added 2026-08-03)

Today the three "cache" surfaces have subtly different and partly-broken read/write semantics.
Unify them so a user can reason about one model:

| Mechanism | Reads on a live run? | Writes? | Gap to close |
|-----------|----------------------|---------|--------------|
| `--cache-dir` (capture dump) | **No** — `run_aws` always shells out and only *writes* the JSON | yes (write-only) | the value-flag collision (§UPDATE/§B); it saves **zero** API calls today |
| `--from-cache` (offline replay) | reads for `run_aws` only | no | **does not serve `s3api get-object`** — S3-backed flow logs hit the network |
| TTL cache (this change) | **yes** — reuse fresh entries, skip the call | yes | new; must honour §B/§E |

Required outcomes:

- **The TTL cache is the only surface that *reads on a live run*.** Wire it into `run_aws` (and the
  paginated/streaming readers, §E) so a fresh entry short-circuits the AWS call. Leave the
  `--cache-dir` capture dump write-only (don't make it silently start serving stale data), but base
  its filenames on the **unified key** so the dump is faithful (no collision).
- **Make `--from-cache` a *true* offline replay for S3-backed flow logs.** It must serve
  `download_object` (`s3api get-object`) from the object cache — same `s3://<bucket>/<key>` identity,
  copy the cached gz body to `dest`, **zero** network — not just `run_aws`. Preserve the
  no-live-call invariant for `download_object` under `--from-cache` (today it's only enforced for
  `run_aws`, which is why the offline S3 path silently reaches out).
- **One cache root, one opt-in, consistent bypass.** `--cache` (TTL), `--no-cache`/`--refresh`
  (bypass), and the object cache all share the unified root (§A) and the unified key (§B); document
  how the TTL cache relates to the two capture/replay flags so all three are distinguishable
  (the README "Caching" section in the DoD).

## Proposed per-command TTLs (by volatility — adjust with justification)
| TTL | Commands | Why |
|-----|----------|-----|
| **30 days** | S3 flow-log **objects** (`s3api get-object` bodies) | immutable once written |
| **7 days** | `ec2 describe-vpcs`, `describe-subnets`, `describe-flow-logs`, `describe-nat-gateways`, `describe-vpc-endpoints`, `describe-route-tables`, `elbv2/elb describe-load-balancers` | stable config/topology, rarely changes |
| **24 h** (requested floor) | `ec2 describe-network-interfaces`, `describe-instances`, `describe-security-groups`, `cloudtrail lookup-events`, `logs filter-log-events`, `s3api list-objects-v2` | more volatile — ENIs/instances churn, record/listing data accrues continuously; 24h bounds staleness |
| **never cached** | `sts get-caller-identity` | account/credential verification must be live |

Implement the TTL policy as an explicit map keyed by `(service, subcommand)` (mirroring how
`ROLE_COLLECTORS` is data, not control flow), with a sane default TTL for anything unlisted. The
`logs filter-log-events` TTL (24h) applies to the **whole logical group result** (all pages), not
to an individual page (§E). Freshness for a paginated entry is the age of the completed aggregate.

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
- **No collision:** `filter-log-events` for two different `--log-group-name` values → two distinct
  entries; a replay returns each group's own events (not the last-written group's for both). Same
  for `list-objects-v2` on two `--bucket`/`--prefix` values and `lookup-events` on two EventNames.
- **Paginated logical-query caching:** a 2-page `filter-log-events` group caches as **one** entry;
  a second run within TTL makes **zero** page calls and replays **both** pages' events in order.
  A run that fails mid-pagination does **not** leave a "complete" entry that a later run trusts.
- **Cache read stays bounded:** a cache hit on a record-bearing command feeds the streaming sink
  (assert it does not `json.loads` the whole artifact) — parallels the bounded-memory tests in
  `tests/test_flowlog_streaming.py`.
- **Offline S3 replay:** `--from-cache` (with the object cache populated) serves `s3api get-object`
  from disk and makes no network call for S3-backed flow logs.

## Definition of done
- [ ] Response cache implemented per the TTL table, opt-in and overridable, atomic writes; S3
      cache relocated onto the same unified cache root/surface (behavior preserved).
- [ ] **Unified cache key** applied to all three mechanisms (TTL cache, `runner._cache_key`,
      `cli._make_cache_reader`): includes the identifying value-flags, excludes window/pagination
      flags; the value-flag collision (multiple groups/buckets/EventNames → one file) is fixed and
      the ad-hoc `_cache_variant` retired.
- [ ] **Paginated commands cached at logical-query granularity** in a **streamable** artifact
      (append-per-page NDJSON, read lazily into `sink.extend(...)`); a cache hit on a large
      `filter-log-events` group does not `json.loads` the whole result — the bounded-memory
      guarantee holds on both live and cache paths. Half-paged entries are never trusted as complete.
- [ ] `--from-cache` serves `s3api get-object` from the object cache (full offline S3 replay);
      `--cache-dir` / `--from-cache` capture/replay semantics otherwise unchanged; `sts` never
      cached; determinism preserved.
- [ ] `pytest` passes offline with new tests; `ruff` clean.
- [ ] Verified end-to-end: `cloudbreachgraph --from-cache tests/fixtures --flow-logs --cache
      --output-dir /tmp/out` — confirm cache hits on a second run via the stderr diagnostic.
- [ ] Docs updated: `README.md` (flags + a "Caching" section distinguishing the three
      mechanisms), `docs/02_architecture.md` (§3/§5.7 + a short cache design note), IAM notes
      unchanged.
- [ ] Write `docs/learnings/learnings_<YYYY-MM-DD>_<slug>.md` (per docs/04_conventions.md) and
      commit it with the code. Capture the cache-key canonicalization (esp. the `--start-time`
      **and pagination-token** exclusion, and the fix for the `_cache_key` value-flag collision),
      the TTL policy + rationale, the bypass rules, how the S3 cache was reconciled onto the unified
      root, and — critically — **the pagination/streaming interaction**: why paginated commands
      must cache at logical-query granularity and why the cache artifact for record/listing commands
      must be streamable (so a hit doesn't re-OOM), plus how `--from-cache` now serves S3 objects.

## Git
- Branch off the latest `main`. Commit in logical chunks; push. Do **not** open a PR unless asked.
