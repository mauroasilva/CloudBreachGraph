# Learnings — 2026-07-22 label-overlap-minimization

## 1. What this change delivered
The overlap-free HTML layout (`--optimize-passes N` without `--ringed`) now also drives
**label overlap to zero**, alongside its existing node-node and edge-over-node guarantees.
"Label overlap" = a node's drawn label rectangle intersecting another label, or a node's disk
sitting on another node's label. All work is in `src/cloudbreachgraph/output/html_export.py`:

- **Label geometry helpers** (shared, near `_node_radius`): `_label_dims(node) -> (half_w, height)`
  estimates a label's pixel box from its character count (11px label line + optional 9px detail
  line; `_LABEL_CHAR_W`, `_LABEL_FONT`, `_DETAIL_FONT`, `_LABEL_GAP`, `_LABEL_LINE_H`,
  `_DETAIL_LINE_H`). `_label_rect(x, y, r, half_w, h)` returns the world-space rectangle drawn
  just under the disk. There is **no font engine** (stdlib-only rule), so widths are an estimate;
  it only needs to bracket the real glyph advances and be deterministic.
- **Counting**: new `_count_label_overlaps(nodes, edges) -> (label_label, node_label)` (the twin of
  `_count_overlaps`, which is unchanged and still returns `(node_node, edge_node)`), plus helpers
  `_rects_overlap`, `_disk_rect_overlap`, and the array-based `_count_remaining_label_overlaps`.
- **Projection**: `_separate_overlaps` gained an `include_labels: bool` param and two new rules —
  (b) label-rect vs label-rect (AABB min-translation), (c) node-disk vs other label-rect. Label
  pushes are under-relaxed by `_OPT_LABEL_RELAX=0.5` to damp limit cycles. `_has_overlap` got the
  same `include_labels` toggle.
- **Four-phase `_layout_nodes`**: phases 1-3 (unfold → disk/edge projection → crossing reduction)
  now run **disk-only** (`include_labels=False`) so crossing reduction still works on a compact
  layout. New **phase 4** clears labels: it **uniformly inflates** the finished layout about its
  centroid (a crossings-invariant transform) and projects labels apart, **escalating** the scale
  (`_OPT_LABEL_SCALE0=2.5`, `×_OPT_LABEL_SCALE_STEP=1.3`, `_OPT_LABEL_PROJECT_CAP=250`/attempt)
  until it reaches zero, keeping the best snapshot.
- **Packing**: `_pack_components` bounding boxes now include label extents, so one component's
  labels can't spill onto another's.
- **Rendering**: the shared draw-only template (`_STATIC_TEMPLATE`) scales label fonts *and*
  offsets with the view **only for the overlap-free variant**, gated by an injected
  `const SCALE_LABELS` (`_render_static_layout` sets it `true` for `overlap-free`, `false` for
  `ringed`). The force template and the ringed layout keep fixed-size labels.

## 2. Interface contract for the next change
- `_separate_overlaps` and `_has_overlap` now take extra positional args `lhw, lh` (per-node label
  half-width/height lists) **before** `n`, and a trailing `include_labels: bool = True`. Callers in
  `_layout_nodes`/`_reduce_crossings_free` pass these. `_reduce_crossings_free`'s signature is
  `(xs, ys, epairs, radii, lhw, lh, n, max_passes, passes)`.
- `_count_overlaps` is **unchanged** — still `(node_node, edge_node)`. Use `_count_label_overlaps`
  for the two label counts. Tests assert both reach `(0, 0)`.
- The static template now contains the placeholder `__SCALE_LABELS__`; any new caller of
  `_render_static_layout` must keep passing a variant so it gets replaced (`"true"`/`"false"`).
  A leftover placeholder would ship broken JS.

## 3. Decisions & rationale
- **Why inflate instead of laying out with label-sized nodes up front?** Spacing the disks for
  label width in phase 1 tangles the force layout and *worsens* crossings badly (largest example
  VPC went 15→70). Uniform scaling of a *finished* layout changes **no** crossing (crossings are
  scale-invariant), so inflating after phases 1-3 gets zero label overlap **and** the original
  crossing counts (example split-by-vpc: 3/8/15/0 — identical to the pre-change disk layout).
- **Why escalate the scale?** The disk layout packs connected nodes ~a radius apart, far tighter
  than a label is wide, so the label projection oscillates without room. More room always helps and
  in the limit every label separates, so escalating the inflation guarantees convergence. ~2.5×
  clears the example VPCs in a handful of passes.
- **Why scale label fonts with the view (and only for this layout)?** The label rectangles are
  world-space but sized to screen pixels. If fonts stayed fixed, `autoCenter` zooming out to fit an
  inflated layout would undo the separation on screen. Scaling fonts with the view (like the disks)
  makes label overlap a **zoom-invariant world-space property** the optimiser controls. The ringed
  and force layouts do **not** separate labels in world space, so for them fixed fonts are better —
  the reader can zoom in to pull two labels apart, which scaled fonts would prevent. Hence the
  `SCALE_LABELS` gate.

## 4. Deviations from the plan
None structural — this is an incremental change to the existing overlap-free layout described in
`docs/02_architecture.md §7`. Docs (README §Overlap-free layout, architecture §7, module docstring,
`OPTIMIZE_PASSES_HELP`) were updated to match.

## 5. Gotchas & surprises
- **Crossing reduction and label projection fight each other.** Clearing labels *before* crossing
  reduction, or re-clearing labels *after* relocation, collapses nodes and the four-way projection
  can only cycle (never certifies zero). The fix was ordering: disk-only through phase 3, labels
  last via inflation. An earlier attempt that ran the full projection during reduction reverted to
  the 72-crossing phase-2 layout every time.
- **A misleading experiment**: capturing the "base" layout at the first full-mode
  `_separate_overlaps` call captures it *after* the SCALE0 inflation, so scale factors measured
  that way are relative, not absolute. Measure inflation via the real `_optimized_view_data`.
- **Dense graphs are spread out and need zoom to read.** With zero label overlap the layout is
  necessarily large (the 57-node VPC spans ~5000 world px); at the fit zoom labels are tiny/hidden
  and you zoom in to read them, cleanly non-overlapping. This is inherent — 57 wide labels can't be
  readable and non-overlapping in one screen at once.
- eni disks (radius 9) don't reach their *own* label (drawn 12px below centre), so a planted
  node-label test needs a third node sitting *inside* the label band (see
  `test_count_label_overlaps_detects_planted_overlaps`).

## 6. Known gaps / TODO
- **Ringed layout labels are not zeroed.** Ringed nodes sit on fixed-radius rings ~92px apart while
  labels are ~130px wide, so ring labels overlap in world space; fully clearing them would mean
  restructuring ring radii and the cluster grid (regression risk against the `<45°` adjacency
  test). Left as-is with fixed-size labels (zoom in to read). The **force** layout is likewise
  unchanged (it has no optimisation pass). If a future change wants ringed label clearance, inflate
  each cluster about its centre and grow the grid cell accordingly.
- The label-width estimate is character-count × a constant; a proportional-font metrics table would
  be tighter but adds data for no correctness gain (over-estimating only adds whitespace).
- Phase 4 over-spreads uniformly (a few tight pairs force a global inflation). A non-uniform
  compaction that pulls over-separated nodes back while keeping labels clear would be tighter.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                         # 236 tests; new label tests in tests/test_convert.py
ruff check . && ruff format --check .

# End-to-end, offline, the change-request acceptance scenario:
cloudbreachgraph-to-html docs/examples/example-graph.json \
    --split-by-vpc --optimize-passes 10000 -o /tmp/cbg-html
# Each graph-<VPC>.html reaches (0,0) label overlaps, (0,0) node/edge overlaps.
# Verify programmatically:
python - <<'PY'
import re, json, glob
import cloudbreachgraph.output.html_export as H
for f in sorted(glob.glob('/tmp/cbg-html/*.html')):
    d = json.loads(re.search(r'const GRAPH = (\{.*?\});\n', open(f).read(), re.S).group(1))
    print(f.split('/')[-1], 'labels', H._count_label_overlaps(d['nodes'], d['edges']),
          'nodeovl', H._count_overlaps(d['nodes'], d['edges']),
          'cross', H._count_crossings(d['nodes'], d['edges']))
PY
```
Relevant tests: `test_optimized_layout_reaches_zero_labels_on_each_split_vpc`,
`test_optimized_layout_reaches_zero_on_example_graph`, `test_optimized_layout_removes_all_overlaps_small`,
`test_count_label_overlaps_detects_planted_overlaps`,
`test_optimized_template_scales_label_fonts_but_ringed_does_not` (all in `tests/test_convert.py`).
```
