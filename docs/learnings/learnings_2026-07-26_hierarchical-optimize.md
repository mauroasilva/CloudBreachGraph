# Learnings — 2026-07-26 hierarchical-optimize

Follow-up to `learnings_2026-07-25_hierarchical-html-output.md`, which added the `--hierarchical`
layout and listed "`--hierarchical` + `--optimize-passes` does nothing" as a known gap. **That gap
is now closed.**

## 1. What this change delivered
- `--hierarchical` now honours `--optimize-passes N` (previously ignored). With `N > 0` the
  hierarchical layout is refined to **minimise edge crossings**, and to have **zero node overlap**
  and **zero label overlap**. `N = 0` (default) is byte-for-byte unchanged.
- New in `src/cloudbreachgraph/output/html_export.py`:
  - `_optimize_hier_cluster(bucket, col_x, adj, passes, row_gap)` — cooled **barycenter sweeps**
    (the layered-graph crossing-reduction heuristic): each node is aimed at the mean y of its
    neighbours (VPC center skipped) and re-placed with `_place_column`; the two sides are optimised
    independently; `_OPT_COOLING` freezes it so a big `N` converges.
  - `_hier_column_x_labeled(layers_present, eff_hw, vpc_hw)` — **label-aware** column x spacing
    (linear analogue of `_label_ring_radii`).
  - `_hier_row_gap(members)` — label-aware vertical row gap.
  - `_hier_eff_hw(node)` — a node's horizontal half-extent = `max(label half-width, disk radius)`.
  - Constant `_HIER_LABEL_PAD = 12.0`.
- Threaded `passes` through `_hierarchical_view_data`, `build_hierarchical_html`,
  `write_hierarchical_html`, and `write_layout_html` (which now calls
  `write_hierarchical_html(graph, path, passes=optimize_passes)`). `SCALE_LABELS` is set on when
  `passes > 0` (labels are separated in world space, so fonts scale with the view).
- Docs: README "Reducing crossings (`--optimize-passes N`)" under the Hierarchical section,
  architecture §7, `OPTIMIZE_PASSES_HELP`, and the module/section docstrings. New tests in
  `tests/test_convert.py`, `tests/test_output.py`, `tests/test_cli.py`.

## 2. Interface contract
- `build_hierarchical_html(graph, passes=0)` / `write_hierarchical_html(..., passes=0)` /
  `_hierarchical_view_data(graph, passes=0)`. `passes` is the max barycenter-sweep budget.
- `write_layout_html(..., hierarchical=True, optimize_passes=N)` → hierarchical layout with `N`
  sweeps. `--hierarchical` still takes precedence over `--ringed`.

## 3. Decisions & rationale
- **Guarantees by construction, not by projection.** The ringed optimizer clears overlaps with an
  inflate-about-centre + free geometric projection (`_clear_cluster_overlaps`), which nudges nodes
  off their rings. The column layout doesn't need that: if the **columns are spaced label-aware**
  (no two columns' label rectangles can overlap horizontally) and the **rows are label-aware** (a
  node's label clears the disk below it), then no two label rectangles overlap → no disk sits on a
  label → no two disks overlap. These hold *regardless* of how the barycenter step reorders nodes,
  so the crossing-reduction sweep can run freely without ever re-introducing an overlap, and the
  columns stay crisp (nodes never leave their column). This is cleaner and cheaper than porting the
  ringed projection, and it keeps the hierarchical look sharp.
- **Barycenter (not the ringed greedy relocation) for crossings.** A column layout is a layered
  (Sugiyama-style) graph, and iterated barycenter is the standard crossing-reduction heuristic for
  it. The key win over the base placement: the base fixes ENI order by subnet and only barycenters
  the *outer* layers once; the optimizer lets the ENIs move too and iterates, which is what actually
  cuts crossings (45 → 27 on the example graph).
- **Cooling reused (`_OPT_COOLING`).** Same reason as ringed: without it the barycenter iteration
  limit-cycles on dense graphs and the emitted bytes would depend on the exact pass count. It
  converges by ~100 passes on the 124-node example (see the convergence test, which pins
  `build(200) == build(600)`).

## 4. Deviations from the plan
- None. This is exactly the "add optimization to minimize crossings, ensure zero node overlap and
  no label overlap" request.

## 5. Gotchas & surprises
- **Convergence is ~100 passes on the example**, not ~20 like the tiny ringed crossing fixture.
  `_OPT_COOLING = 0.9` means alpha ≈ 0.9^n; the 2-dp rounding freezes once alpha·(typical Δy) <
  0.005, i.e. n ≳ 95. The convergence test uses `build(200) == build(600)` — don't tighten it to a
  low pass count.
- **`_count_overlaps` returns `(node_node, edge_node)`; only assert the first is 0.** The
  hierarchical optimizer does **not** guarantee zero *edge-over-node* overlap — a column-skipping
  edge (e.g. a source reaching an ENI directly under `--no-security-groups`, or an ENI→SG edge that
  skips the EC2/LB column) can pass over a node in the skipped column. On the example whole-graph
  and each split VPC it happens to be 0, but on the small fixture it's 2 (SGs shown) / 1 (hidden).
  The tests assert `_count_overlaps(...)[0] == 0` (node-node) and `_count_label_overlaps == (0, 0)`
  (both label kinds), never the full `(0, 0)` tuple. If a future change wants edge-over-node cleared
  too, that needs either edge routing or the ringed-style free projection (giving up column
  crispness).
- `_place_column` **reorders its members list in place** (sorts by target y). In
  `_optimize_hier_cluster` the per-side/per-layer lists are private copies, so this is fine, but keep
  it in mind if you reuse those lists elsewhere.

## 6. Known gaps / TODO
- Zero *edge-over-node* overlap is not guaranteed (see §5). Left intentionally — it wasn't in the
  request and the by-construction node/label guarantees are cleaner without it.

## 7. How to verify
```bash
python -m pytest -q                       # full suite
ruff check src tests && ruff format --check src tests
cloudbreachgraph-to-html docs/examples/example-graph.json --hierarchical --optimize-passes 200 -o /tmp/h.html
```
Key tests (all in `tests/test_convert.py` unless noted):
`test_hierarchical_optimize_reaches_zero_node_and_label_overlap_on_example`,
`test_hierarchical_optimize_reaches_zero_overlap_on_each_split_vpc`,
`test_hierarchical_optimize_reduces_crossings` (45 → 27),
`test_hierarchical_optimize_keeps_nodes_on_one_side`,
`test_hierarchical_optimize_converges`, `test_hierarchical_passes_zero_is_unchanged`,
`test_hierarchical_optimize_scales_label_fonts_but_default_does_not`,
`tests/test_cli.py::test_html_hierarchical_optimize_passes_scales_labels`.
