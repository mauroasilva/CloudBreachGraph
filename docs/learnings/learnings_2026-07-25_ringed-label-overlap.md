# Learnings — 2026-07-25 ringed-label-overlap

## 1. What this change delivered
The **ringed** layout's optimisation (`--ringed --optimize-passes N`, N > 0) now drives **label
overlap to zero** — both label-on-label and disk-on-another-node's-label — on top of the node-node
and edge-over-node overlaps it already cleared, matching the guarantee the sibling overlap-free
layout gives, **without** breaking the concentric-ring shape, the `<45°` LB-sharing-subnet
adjacency, or the existing crossing reduction. This closes the "Ringed layout labels are not
zeroed" gap noted in `learnings_2026-07-22_label-overlap-minimization.md §6`. All work is in
`src/cloudbreachgraph/output/html_export.py`.

Measured on `docs/examples/example-graph.json` at `--optimize-passes 200`: whole graph and every
`--split-by-vpc` sub-graph reach `_count_label_overlaps == (0, 0)` **and** `_count_overlaps ==
(0, 0)`; whole-graph crossings 25 (was ~24 before, i.e. unchanged in spirit).

## 2. How it works (three cooperating pieces, all gated on `passes > 0`)
1. **Label-aware ring radii** — `_label_ring_radii(counts, half_widths)`: a ring's arc spacing is
   its widest label (`2·half_w + _RINGED_LABEL_PAD`) instead of the fixed `_RING_ARC` (~92px, far
   narrower than a ~130px label), and its radial step is widened by the neighbouring rings' label
   widths so an inner ring's labels can't reach the next ring. A `_RINGED_LABEL_HEADROOM` (1.35)
   factor leaves circumference slack so connected nodes can still bunch. The default (`passes == 0`)
   path still calls the unchanged `_ring_radii`, so it stays byte-identical.
2. **Label-aware barycenter gap** — `_place_min_gap(..., label_aware=True)` sizes the min angular
   gap to the widest label half-width, not the disk radius, so the barycenter step packs connected
   nodes no tighter than their labels. Only `_optimize_cluster` (a `passes > 0`-only caller) passes
   `label_aware=True`; the flag defaults to `False` so the byte-stable path is untouched.
3. **Per-cluster inflate + project** — `_clear_cluster_overlaps(members, cx, cy, adj)`: uniformly
   **inflates** the finished cluster about its centre (a similarity transform — grows every ring
   radius equally, preserves crossings and every node's angle) and runs `_separate_overlaps(...,
   include_labels=True)` sweeps until nothing moves, **escalating** the inflation
   (`_RINGED_INFLATE_STEP`, `_RINGED_INFLATE_CAP` attempts) if a projection can't reach zero. This
   clears all four overlap kinds at once and returns the factor applied (used to report the grown
   ring radii). Because inflation supplies the room, the projection's nudges are tiny — measured
   radial std ≤ 8px on radii up to ~1700px, i.e. nodes stay essentially on their rings.

**Structure change:** for `passes > 0`, `_ringed_view_data` now lays out / optimises / label-clears
each cluster **about the origin first** (so its final inflated size is known), then sizes the grid
cell from `_cluster_label_extent` (node disks **and** label rectangles) and translates each cluster
into its cell — the "layout then pack" pattern the overlap-free layout uses (`_pack_components`).
The `passes == 0` path keeps the original "place straight into the grid cell" code, so it is
byte-for-byte unchanged (`test_ringed_passes_zero_is_unchanged`).

**Rendering:** `_render_static_layout` gained an explicit `scale_labels: bool` parameter (was
sniffing `variant == "overlap-free"`). `build_ringed_html` passes `scale_labels=passes > 0` — the
optimised ringed page now scales label fonts with the view (its labels are separated in world space,
like the overlap-free layout), while `passes == 0` keeps fixed-size fonts.

## 3. Decisions & rationale
- **Why inflation for the ringed layout specifically?** It's the one room-making transform that
  keeps nodes on concentric rings *and* preserves their angles — so the ring shape and the `<45°`
  adjacency both survive by construction. Free `_separate_overlaps` alone would move nodes off the
  rings. Inflation + a *tiny* residual projection gets the best of both.
- **Why still project after inflating?** Inflation is scale-invariant, so it can't fix an
  edge-over-node graze (a near-radial `attached_to`/`in_subnet` edge clipping a neighbour on the
  intermediate ring) — those survive any uniform scale. The projection sweep handles them (and any
  last label touch); inflation just makes its moves small.
- **Why label-aware radii *and* label-aware min-gap, not inflation alone?** Inflating the tight
  barycenter-packed layout needed ~12× to clear labels (connected nodes were packed a disk-gap
  ~30px apart vs ~130px labels). Sizing the rings and the min-gap for labels up front makes the
  starting layout nearly clear, so inflation needs only a small factor — compact clusters, tiny
  projection drift.

## 4. Deviations from the suggested approach
The suggested "finish with a label projection step (reuse `_separate_overlaps(include_labels=True)`
with light radial freedom, or a ring-constrained variant)" is realised as the **inflate-then-
project escalation** (`_clear_cluster_overlaps`) rather than a bespoke ring-constrained projection —
it reuses the existing `_separate_overlaps`/`_has_overlap` unchanged and leans on inflation to keep
the moves small, which empirically keeps nodes on-ring (§2) with much less new code.

## 5. Gotchas & surprises
- **The old ringed optimiser never *guaranteed* zero edge-over-node — it just happened to be zero
  on the example.** `_optimize_cluster` ends with `_nudge_overlaps`, which only separates node
  *disks*. Growing the rings for labels changed the geometry and surfaced 4 edge-over-node grazes;
  the new projection is what actually guarantees they're zero now (and `test_ringed_optimize_leaves
  _no_overlaps` only ever checked node-node, so it never caught this).
- **`build_ringed_html(g, 20) != build_ringed_html(g, 200)` on the *example* graph** — that's fine
  and pre-existing: `_optimize_cluster`'s cooling hasn't frozen a large cluster by pass 20. The
  convergence tests (`_converges_early`, `_freezes_on_tangled_graph`) use small graphs that do
  freeze, and they still pass; `_clear_cluster_overlaps` is deterministic given its input.
- Determinism holds because inflation factors, projection order, and the escalation are all fixed
  and coords are rounded once after translation (`test_ringed_optimize_is_deterministic`).

## 6. Known gaps / TODO
- Zero-overlap ringed clusters are **large** (the 57-node VPC spans ~4000px); at fit-zoom labels are
  tiny/hidden and you zoom in to read them — inherent, same as the overlap-free layout.
- Inflation is uniform, so one tight cross-ring pair can over-spread a whole cluster. A non-uniform
  compaction (pull over-separated rings back while keeping labels clear) would be tighter.
- The **force** layout still doesn't separate labels (it has no optimisation pass) — unchanged.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest && ruff check . && ruff format --check .
cloudbreachgraph-to-html docs/examples/example-graph.json --ringed --optimize-passes 200 -o /tmp/r.html
cloudbreachgraph-to-html docs/examples/example-graph.json --ringed --optimize-passes 200 \
    --split-by-vpc -o /tmp/rsplit
python - <<'PY'
import re, json, glob
import cloudbreachgraph.output.html_export as H
for f in ["/tmp/r.html"] + sorted(glob.glob('/tmp/rsplit/*.html')):
    d = json.loads(re.search(r'const GRAPH = (\{.*?\});\n', open(f).read(), re.S).group(1))
    print(f.split('/')[-1], 'labels', H._count_label_overlaps(d['nodes'], d['edges']),
          'nodeovl', H._count_overlaps(d['nodes'], d['edges']),
          'cross', H._count_crossings(d['nodes'], d['edges']))
PY
```
Relevant tests (`tests/test_convert.py`): `test_ringed_optimize_reaches_zero_label_overlap_on_example`,
`test_ringed_optimize_reaches_zero_label_overlap_on_each_split_vpc`,
`test_ringed_optimize_keeps_clusters_apart`, `test_ringed_passes_zero_is_unchanged`,
`test_ringed_optimize_places_lb_sharing_subnets_adjacent`, `test_ringed_optimize_is_deterministic`,
`test_optimized_template_scales_label_fonts_but_ringed_does_not`.
