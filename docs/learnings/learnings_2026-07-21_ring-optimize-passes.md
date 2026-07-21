# Learnings — 2026-07-21 ring-optimize-passes

## 1. What this change delivered
- New **`--optimize-passes N`** flag on `cloudbreachgraph-to-html` (ringed layout only). It runs
  up to N crossing-reduction passes that **reorder nodes within their rings** to pull connected
  nodes together (so an LB whose ENIs span far-apart subnets stops flinging edges across the
  circle) and then **nudge apart** any overlapping nodes. Rings are preserved; only angular
  order and tiny overlap offsets change. `N=0` (default) is byte-identical to the previous
  ENI-aligned placement.
- `src/cloudbreachgraph/output/html_export.py`:
  - `_optimize_cluster(bucket, cx, cy, rs, adj, passes)` — barycenter (mean-of-neighbours)
    reordering, sweeping rings `1→2→3→2` per pass, converging early when no ring's order
    changes.
  - `_place_even_anchored(members, cx, cy, radius, target)` — after sorting a ring by target
    angle, place evenly (overlap-free) and rotate the whole ring by the single offset
    (`_circular_mean` of slot→target deltas) that best matches the targets.
  - `_nudge_overlaps(nodes, iterations, pad)` — bounded, early-exiting O(n²) push-apart safety
    net for residual disk overlaps (deterministic: coincident nodes split along +x).
  - `_adjacency(graph)`, `_node_radius(node)`, and `_NODE_RADII` (mirrors the page's
    `radiusFor`).
  - `passes` threaded through `_ringed_view_data` → `build_ringed_html` → `write_ringed_html`.
- `src/cloudbreachgraph/convert.py`: `--optimize-passes` (int, default 0); negative → rc 2;
  passing it without `--ringed` warns and is ignored.

## 2. Interface contract for the next session
- `build_ringed_html(graph, passes=0)`, `write_ringed_html(..., passes=0)`,
  `_ringed_view_data(graph, passes=0)` — the extra arg is keyword/positional with default 0,
  so all existing callers and the `passes=0` output are unchanged.
- The optimizer mutates the node dicts' `x`/`y` (and reorders the per-ring lists in `bucket`)
  **in place**, after the initial placement. It does **not** touch the `clusters` metadata
  (centres/radii/labels stay the nominal ring radii).

## 3. Decisions & rationale
- **Reorder + even-anchored placement** rather than free force relaxation (the chosen option
  was "rings + overlap nudge"). Even spacing on each ring makes intra-ring overlap impossible,
  and the ≥150px ring gap makes cross-ring overlap impossible — so the *ordering* is what cuts
  crossings and overlaps are essentially gone before the nudge even runs. Verified: on a
  crafted crossing-heavy graph total edge length dropped ~26% (3363→2475) with min node gap
  unchanged at 150.
- **Barycenter heuristic** is the standard, cheap, deterministic circular crossing reducer and
  fits the near-tree topology (VPC→subnet→ENI→EC2/LB). It naturally migrates subnets that share
  an LB next to each other.
- **Opt-in, default 0** so the exact ENI-aligned placement (and every existing byte-stable
  test) is preserved; only users who want it pay the reordering cost / behaviour change.
- **Applies to ringed only**; on the force layout the flag is a no-op with a warning (the force
  sim already self-distributes).

## 4. Deviations from the plan
- None.

## 5. Gotchas
- `_circular_mean([])` is `atan2(0,0) == 0` (degenerate). Every ring-1/2/3 node has ≥1 non-centre
  neighbour except orphans (`--include-orphans`) and unassigned-cluster ENIs, which have none —
  those keep their current angle (guarded by `if neigh else angle[m]`).
- The centre VPC (ring 0) has no angle (radius 0); it is excluded from every barycenter
  neighbour set (`ring_of.get(nid, 0) != 0`).
- `argparse` accepts `--optimize-passes -1` as a value (all options are long, so `-1` isn't
  mistaken for a flag); the `>= 0` check lives in `convert.main`, not argparse.
- `_nudge_overlaps` is O(n²) per iteration but early-exits after one clean scan, so on real
  data it's a single ~n²/2 distance sweep. If a future giant single cluster makes that slow,
  add a spatial grid.

## 6. Known gaps / follow-ups
- With optimization on, a subnet's ENIs are no longer guaranteed contiguous (an ENI is pulled
  toward both its subnet and its LB), so the "ENIs grouped by subnet" property from the earlier
  change is relaxed in favour of fewer crossings — an intentional trade for `passes > 0`.
- Barycenter is a heuristic (global crossing minimisation is NP-hard); it cuts crossings
  sharply but doesn't guarantee the optimum.

## 7. Git note (this session)
- PR #13 was merged into `main`. This work is on branch
  `claude/cloudbreachgraph-no-ring-circles` (which also carried the "stop drawing ring circles"
  change) — that earlier change has NOT yet been merged, so both changes ride this branch.

## 8. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 132 tests, all offline
ruff check . && ruff format --check .
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
cloudbreachgraph-to-html /tmp/cbg-out/graph.json --ringed --optimize-passes 20 -o /tmp/opt.html
# Compare with an unoptimized ringed render to see the reordering.
```
