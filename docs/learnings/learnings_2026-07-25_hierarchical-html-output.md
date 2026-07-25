# Learnings — 2026-07-25 hierarchical-html-output

## 1. What this change delivered
- A **fourth HTML layout**, `--hierarchical`, alongside force / ringed / overlap-free. It follows
  the ringed layout's rules but "unrolls" the concentric rings into **left/right columns**: each
  VPC is the center of its own cell, and the layers (subnet · ENI · EC2/LB/NAT/VPCE · security
  group · IP source) fan out horizontally as columns to the **left and right** of the VPC.
- New public API in `src/cloudbreachgraph/output/html_export.py`:
  - `build_hierarchical_html(graph)` / `write_hierarchical_html(graph, path, *, max_nodes, max_bytes)`
    — same contract + size guard + `.dot` fallback as `write_ringed_html`.
  - `HIERARCHICAL_HELP` (flag help fragment, shared by both CLIs).
  - `_hierarchical_view_data(graph)` — the payload builder (the analogue of `_ringed_view_data`).
  - `write_layout_html(...)` gained a `hierarchical: bool = False` kwarg; **`--hierarchical` takes
    precedence over `--ringed` and `--optimize-passes`**.
- New internal helpers: `_hier_column_x`, `_partition_sides`, `_place_column`, `_mean_target`,
  `_place_hier_cluster`, `_hier_extent`; constants `_HIER_COL1`, `_HIER_COL_GAP`, `_HIER_ROW`,
  `_HIERARCHICAL_HINT`.
- **Refactor:** the ENI-alignment maps (`enis_of_subnet`, `enis_of_lb`, `enis_of_sg`,
  `subnet_of_eni`, `enis_of_source`) that the ringed layout built inline are extracted into
  `_eni_anchor_maps(graph, by_id)` and now **shared** by ringed and hierarchical. This is a pure
  extraction — ringed output is byte-for-byte unchanged (`test_ringed_passes_zero_is_unchanged`
  and all ringed byte tests still pass).
- CLI wiring: `--hierarchical` flag on both `cloudbreachgraph` (`cli.py`) and
  `cloudbreachgraph-to-html` (`convert.py`); "only affects --html" warning; composes with
  `--split-by-vpc` and `--no-security-groups`.
- Docs: `README.md` (flags table + new "Hierarchical layout" section), `docs/02_architecture.md §7`
  (the `--html`/converter/split-by-vpc descriptions), and the module docstring.

## 2. Interface contract for the next change
- The layout selector is **one place**: `html_export.write_layout_html(graph, path, *, ringed,
  hierarchical, optimize_passes)`. Both CLIs call it; precedence is
  `hierarchical > ringed > optimize_passes>0 > force`. Add future layouts there, and add a matching
  `<layout>_HELP` fragment.
- The static (draw-only) HTML template (`_STATIC_TEMPLATE` via `_render_static_layout`) is
  **reused unchanged**. A `clusters[]` entry is `{cx, cy, rings:[...], label}`; the template draws
  the cluster label at `cy - max(rings) - 6` and uses `rings` only for the label offset and
  autoCenter bounds. The hierarchical layout passes a **single-element `rings=[half_height]`** so
  the VPC label floats above the cluster — no JS change was needed. If you add a layout, you can do
  the same trick rather than touching the template.
- `_eni_anchor_maps(graph, by_id)` returns the 5-tuple
  `(enis_of_subnet, enis_of_lb, enis_of_sg, subnet_of_eni, enis_of_source)`; reuse it for any layout
  that ENI-anchors its other layers.

## 3. Decisions & rationale
- **Sides = connected components of the VPC subgraph with the VPC center removed**
  (`_partition_sides`). This is exactly the unit that guarantees "connected nodes share a side" and
  "no left-right edge": a load balancer / SG / source that spans several subnets stitches them into
  one component, so they land on the same side together. The only center-crossing edges are
  subnet→VPC, which terminate *at* the center (not on the far side), so they don't violate the rule.
- **Balance = greedy largest-first bin-packing** (each component to the currently-smaller side).
  Simple, deterministic, and "as balanced as possible" given components can't be split. A single
  giant component (everything transitively connected) unavoidably lands on one side — that's correct
  behaviour: splitting it would create a left-right edge, which the request forbids "at all cost".
- **Column x is shared across both sides** (`_hier_column_x` collapses empty layers, like
  `_ring_radii`), so the VPC stays centered and the columns line up symmetrically. Layer→column and
  ENI-anchor→y is the direct transliteration of the ringed layout's layer→radius and
  ENI-anchor→angle.
- **`_place_column` reuses `_isotonic_l2`** (the same min-gap machinery as the ring's
  `_place_min_gap`), but on a line instead of a circle — no angular wrap, so it's the simpler linear
  case. ENIs are placed first (evenly spread, ordered by subnet so a subnet's ENIs are contiguous)
  and every other column aligns to the mean y of its ENIs.
- **Fixed-size labels (`SCALE_LABELS=false`)**, matching the *default* ringed layout: the reader
  zooms in to read labels. Vertical spacing (`_HIER_ROW = 52`) is sized to clear a disk + its label
  height; horizontal column gap (`_HIER_COL_GAP = 200`) is generous. I did **not** build an
  optimize/overlap-free variant for hierarchical — `--optimize-passes` is ignored when
  `--hierarchical` is set (documented). If a future change wants guaranteed zero label overlap here,
  the label-aware inflation machinery (`_clear_cluster_overlaps`, `_separate_overlaps`) is available
  to borrow.

## 4. Deviations from the plan
- None from `docs/` conventions. The change request only asked for the `--hierarchical` flag and a
  `graph.html` that renders the hierarchical layout; `--optimize-passes` interaction was left
  unimplemented on purpose (hierarchical ignores it) — noted in §6.

## 5. Gotchas & surprises
- The `rings` field in cluster metadata is overloaded: the ringed layout puts the actual ring radii
  there, but the template only reads `max(rings)` for the label offset / autoCenter. Reusing it as a
  single half-height value is why no template change was needed — but don't assume `rings` is
  always ring radii if you read cluster metadata elsewhere.
- `_place_column` and `_place_hier_cluster` set **raw (unrounded)** x/y; rounding happens once in
  `_hierarchical_view_data` after translating each cluster into its grid cell (same pattern as the
  ringed optimize path). Keep that ordering or extents/byte-stability drift.
- Verified on the shipped example graph: **0 left-right (non-center) edges**, columns strictly
  increase outward by layer on every side, and the 4 VPCs balance to L/R of 12/13, 16/16, 29/27,
  5/2 (the 5/2 VPC has an odd, mostly-single-component shape — expected).

## 6. Known gaps / TODO
- `--hierarchical` + `--optimize-passes` does nothing extra (passes ignored). If crossing/label
  optimisation is wanted for the hierarchical layout, wire a `passes` param through
  `_hierarchical_view_data` and add an in-column barycenter/label pass (mirror `_optimize_cluster`).
- Very wide labels (long ENI IP detail lines) can still overlap the next column horizontally at high
  zoom, because labels are fixed-size and centered. A label-aware column gap (size `_HIER_COL_GAP`
  from the inner column's max label half-width) would fix it if it ever bites.

## 7. How to verify
```bash
pip install -e '.[dev]'
python -m pytest -q                       # full suite (adds ~15 hierarchical tests)
ruff check src tests && ruff format --check src tests
# eyeball it:
cloudbreachgraph-to-html docs/examples/example-graph.json --hierarchical -o /tmp/hier.html
cloudbreachgraph --from-cache tests/fixtures --html --hierarchical --output-dir /tmp/out
```
Key tests: `tests/test_convert.py::test_hierarchical_*` (structure: VPC-at-center, columns
increase outward, **no left-right edges**, sides balanced, connected-nodes-share-a-side),
`tests/test_output.py::test_write_hierarchical_html_*`, `tests/test_cli.py::test_html_hierarchical_*`.
