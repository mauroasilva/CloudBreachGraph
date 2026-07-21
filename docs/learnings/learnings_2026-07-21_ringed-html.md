# Learnings — 2026-07-21 ringed-html

## 1. What this change delivered
- A new **ringed** HTML output mode for the auxiliary `cloudbreachgraph-to-html` tool,
  selected by a new `--ringed` CLI flag. Each VPC is drawn at the center of its own cluster,
  its subnets form the **inner ring**, and everything else under that VPC (ENIs, EC2
  instances, load balancers) forms the **outer ring**. Multiple VPCs are tiled in a grid;
  resources that resolve to no VPC collect into a trailing "unassigned" ring-cluster with an
  empty center.
- `src/cloudbreachgraph/output/html_export.py`:
  - `build_ringed_html(graph) -> str` and `write_ringed_html(graph, path, *, max_nodes, max_bytes) -> Path | None`
    — mirror `build_html`/`write_html` exactly (same `MAX_NODES`/`MAX_HTML_BYTES` size guard,
    same `None`-means-fall-back-to-`.dot` contract).
  - Layout helpers: `_vpc_group_of`, `_ring_of`, `_ring_radii`, `_place_on_ring`,
    `_ringed_view_data`, plus the `_RINGED_TEMPLATE` page and the `_UNASSIGNED` group key.
- `src/cloudbreachgraph/convert.py`: added `--ringed` (store_true); `main` now picks
  `write_ringed_html` vs `write_html` and keeps the identical `.dot` fallback path.
- Tests: `tests/test_output.py` (5 ringed writer tests) and `tests/test_convert.py`
  (grouping/ring-membership, unassigned cluster, determinism, CLI, size-guard fallback).
- Docs: `README.md` (new "Ringed layout" subsection + example), `docs/02_architecture.md §7`.

## 2. Interface contract for the next session
- `html_export.write_ringed_html` has the **same signature and return contract** as
  `write_html`: returns the written `Path`, or `None` when over the node/byte cap (writing
  nothing). Callers fall back to `dot_export.write_dot`. `convert.main` already does this.
- The ringed view payload (`_ringed_view_data`) extends `_view_data` with:
  - per-node precomputed `x`/`y` (floats, rounded to 2dp),
  - a top-level `clusters: [{cx, cy, r1, r2, label}]` list (ring guide circles + VPC label).
  The force-view `_view_data` is unchanged, so `write_html`/`build_html` are untouched.
- Ring assignment is by node **type**: `vpc`→center(0), `subnet`→inner(1), everything
  else→outer(2). VPC membership is traced through the model's edges
  (`subnet→in_vpc→vpc`, `eni→in_subnet→subnet`, `ec2/lb←attached_to←eni`).

## 3. Decisions & rationale
- **Positions computed in Python, not a JS force sim.** The ringed layout is inherently
  deterministic, so computing `x`/`y` server-side keeps the page byte-stable with no PRNG and
  makes the layout unit-testable in Python. The `_RINGED_TEMPLATE` JS therefore only draws +
  handles pan/zoom/drag (no `requestAnimationFrame` loop, no Recompute button) — tests assert
  `REPULSION`/`requestAnimationFrame` are absent to catch accidental reuse of the force page.
- **Ring radii scale with node count** (`_RING_ARC` arc spacing) so a busy inner/outer ring
  grows rather than colliding; clusters are tiled on a uniform grid cell sized to the largest
  cluster for a clean grid.
- **Scoped to the auxiliary tool only** (per the change request wording "a new feature in the
  auxiliary tool cloudbreachgraph-to-html"). The main `cloudbreachgraph --html` path is
  deliberately left unchanged; a follow-up could expose `--html-ringed` there cheaply by
  reusing `write_ringed_html`.

## 4. Deviations from the plan
- None structural. The change request said "first ring … subnets, the **third** ring is
  everything else" — read as three concentric layers (center VPC, inner subnets, outer
  everything-else); implemented as rings 0/1/2 with no separate second ring.

## 5. Gotchas / surprises
- Edge **directions** matter for grouping: `attached_to` runs eni→ec2/lb, so to find an
  ec2/lb's VPC you reverse it (map target→eni), then eni→subnet→vpc. Got this from
  `mapping/builder.py`, which is authoritative over the docs.
- Orphan/synthetic nodes (an ENI in no subnet) correctly land in `_UNASSIGNED`; covered by a
  test using a subnet-less ENI fixture inline (no new `tests/fixtures/` file needed).
- `ruff format` reflows long assert expressions in the new tests — run it, don't hand-wrap.

## 6. Known gaps / follow-ups
- Outer-ring nodes are placed around the whole VPC, not clustered under their specific
  subnet; a future refinement could sub-group the outer ring by subnet angle.
- No `--ringed` equivalent on the main CLI yet (see §3); trivial to add if wanted.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 118 tests (10 new ringed tests), all offline
ruff check . && ruff format --check .
# End-to-end, offline, against checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
cloudbreachgraph-to-html /tmp/cbg-out/graph.json --ringed -o /tmp/cbg-out/graph.ringed.html
# Open graph.ringed.html in any browser: VPC centered, subnets inner ring, rest outer ring.
```
