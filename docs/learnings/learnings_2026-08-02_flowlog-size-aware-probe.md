# Learnings — 2026-08-02 flowlog-size-aware-probe

## 1. What this change delivered
A refinement of the S3 flow-log **fast-fail probe** (added 2026-08-01) so it samples the
**largest objects first** instead of the first objects by key:

- `_list_s3_flow_log_keys` → renamed `_list_s3_flow_log_objects`, now returns `list[tuple[str, int]]`
  — each `.gz` object's `(key, size_bytes)`. `Size` is already in the `list-objects-v2` response, so
  this is **free** (no extra AWS call). A missing/non-numeric size coerces to `0`.
- New `_probe_order(objects)` returns the download order: the `_FLOW_LOG_PROBE_OBJECTS` (25)
  **largest** objects first (the probe set), then every remaining key in sorted order. Ties (equal or
  unknown/0 size) fall back to key order, so the order stays deterministic.
- `_read_s3_records` iterates `_probe_order(...)`; the fast-fail abort message now reports "probed the
  N largest of M object(s) … (largest ~X B) … not downloading the remaining …".

## 2. Why largest-first (and why *not* the user's hash/size-equality idea)
The user asked whether we could "guess the next object is NODATA by same size/hash." We can't, and the
reasoning is worth keeping:

- **All-NODATA objects are NOT identical**, so content-hash/ETag equality and exact-size equality
  never match across distinct objects: each NODATA line carries a different `interface-id` (per ENI),
  different `start`/`end` epoch timestamps (per window), and files have different record counts (ENIs
  reporting per interval varies). Distinct bytes → distinct ETags → distinct sizes. So hashing can't
  predict "the next one is also NODATA."
- **But `Size` from the listing is a strong, free prior.** An all-NODATA object compresses tiny
  (repetitive `- - -`, no varied IP/port/byte data); real-traffic objects are larger and less
  compressible. So sampling the **largest** objects is the most discriminating probe: if even the
  biggest objects parse zero records, the smaller ones won't either. This fixes the real weakness of
  the old first-25-keys probe — keys are time-ordered, so the first 25 are one (often quiet) window
  that can be atypical of the whole set.
- Size **informs**, it does not **certify**: a small object could hold a few real records, and a huge
  object could be all-NODATA if a VPC has thousands of idle ENIs. So size only decides *sampling
  order* and enriches the abort message — it never labels an object NODATA without reading it, and it
  never skips objects that would otherwise be read once a record is found.

## 3. Determinism
Reordering downloads is safe because **output never depends on download order** — flow-log records are
sorted downstream (`mapping/flowlogs.py`, `mapping/builder.py`) before serialization. `_probe_order`
is itself deterministic (`(-size, key)` then sorted remainder). Existing fixtures without a `Size`
field are unaffected: all sizes 0 → order collapses to sorted-key order, exactly the prior behavior.

## 4. Gotchas / follow-ups
- The probe's `objects_read` counter is cumulative across S3 sources, but `total`/`largest` in the
  abort message are per-source (the current listing). Fine for the common single-source case; if
  multi-source diagnostics ever matter, thread per-source stats through.
- The size-aware probe is orthogonal to the still-outstanding **response cache** work queued in
  `docs/prompts/queue/response_and_s3_ttl_cache.md`. A future optimization noted there but *not* done
  here: skip objects below a tiny-size floor outright (a minor download saver, not the main win).
