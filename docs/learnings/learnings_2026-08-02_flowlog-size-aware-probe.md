# Learnings — 2026-08-02 flowlog-size-aware-probe

## 1. What this change delivered
A rework of the S3 flow-log **fast-fail probe** (added 2026-08-01) into a size-ordered read with an
early-stop streak, so it (a) samples the largest objects first, and (b) trims a NODATA **tail** off
an otherwise data-rich source instead of downloading it:

- `_list_s3_flow_log_keys` → renamed `_list_s3_flow_log_objects`, now returns `list[tuple[str, int]]`
  — each `.gz` object's `(key, size_bytes)`. `Size` is already in the `list-objects-v2` response, so
  this is **free** (no extra AWS call). A missing/non-numeric size coerces to `0`.
- New `_size_descending_keys(objects)` returns **all** keys largest-first (ties by key, deterministic)
  — not just a probe set. (Replaced the earlier `_probe_order`, which put only the 25 largest first
  then reverted to key order, and therefore still downloaded the whole NODATA tail once it found data.)
- `_read_s3_records` reads a source in that order and tracks `zero_streak` (consecutive successfully
  read objects that parsed **zero** records). When the streak reaches a threshold it stops reading the
  source (its remaining objects are smaller and almost certainly NODATA). Two thresholds:
  - `_FLOW_LOG_PROBE_OBJECTS` (25) for a **cold** source (no records yet) → all-NODATA/unrecognised;
  - `_FLOW_LOG_TAIL_STREAK` (3) for a source that has **already** yielded records → trim its dead tail.
  A `_warn_nodata_skip` stderr line names the source and how many objects were skipped.

Why two thresholds: a cold source has no positive evidence, so it needs a conservative sample before
we give up (and if *every* source is cold-empty the run still fails loudly). A source that has already
produced records is known-real, so a short run of NODATA at the small-size tail is strong evidence the
rest of the tail is dead — 3 is enough, and the cost of over-trimming is at most a few tiny data
objects (and NODATA is dropped by design anyway). This is the "15k data + 5k NODATA tail" case: read
the 15k, skip the 5k. The value is deliberately small per the user's request; raise it if a fleet has
data files that compress smaller than its NODATA files (interleaved sizes — see §5).

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

## 3. Per-source, not global (fixed the same day)
The original fast-fail probe (2026-08-01) shared **one** `records`/`objects_read` counter across all
S3 sources and `raise`d out of the whole function. That made it **global and order-dependent**: an
all-NODATA VPC that sorted first (or several small NODATA sources reaching 25 objects cumulatively
before any record parsed) aborted the entire run — data-rich VPCs that sorted later were never read.
Asymmetric, too: a data-rich first source disabled the probe for the rest, so a *later* NODATA VPC
was harmless. That asymmetry was the tell that the scoping was wrong.

Fix: the probe is now **per-source**. Each source tracks its own `src_read`/`src_parsed`; when a
source's largest `_FLOW_LOG_PROBE_OBJECTS` objects parse nothing, its remaining objects are skipped
(a stderr warning names the source, count, and largest size) and the loop moves to the next source.
The hard `FlowLogFetchError` fires only **after** all sources, and only if `records` is still empty
and we probed enough objects to be confident (`objects_read >= _FLOW_LOG_PROBE_OBJECTS`). Net effect:
- one all-NODATA VPC + one data-rich VPC → empty VPC skipped after 25 objects, data VPC read, run
  succeeds with a partial graph (matches §9 "partial over abort");
- every VPC all-NODATA → still raises loudly (non-zero exit), preserving the original intent;
- tiny all-NODATA source (< 25 objects) → read fully, no raise (nothing meaningful to save).

## 4. Determinism
Reordering downloads is safe because **output never depends on download order** — flow-log records are
sorted downstream (`mapping/flowlogs.py`, `mapping/builder.py`) before serialization.
`_size_descending_keys` is itself deterministic (`(-size, key)`). Existing fixtures without a `Size`
field are unaffected: all sizes 0 → order collapses to sorted-key order, exactly the prior behavior.

## 5. Gotchas / follow-ups
- **Interleaved sizes are the heuristic's blind spot.** Size is a *prior*, not a certainty: a NODATA
  window with many idle ENIs can compress larger than a data window with one connection. If a fleet's
  NODATA files are routinely bigger than its data files, size-descending order interleaves them and the
  tail-trim (`_FLOW_LOG_TAIL_STREAK = 3`) could skip a few small real-data objects. Accepted trade-off
  (NODATA is dropped by design; the loss is a handful of edges vs. thousands of downloads), but the
  constant is a one-line knob — raise it (or set it equal to `_FLOW_LOG_PROBE_OBJECTS`) for a fleet
  with that profile.
- The final all-empty `FlowLogFetchError` uses the cumulative `objects_read` and lists the abandoned
  sources. The threshold for the hard raise is cumulative (`objects_read >= _FLOW_LOG_PROBE_OBJECTS`),
  so several small all-NODATA sources still trip it once their combined reads pass the probe size.
- Orthogonal to the still-outstanding **response cache** work queued in
  `docs/prompts/queue/response_and_s3_ttl_cache.md`. A future optimization noted there but *not* done
  here: skip objects below a tiny-size floor outright using the listing sizes (no download at all).
