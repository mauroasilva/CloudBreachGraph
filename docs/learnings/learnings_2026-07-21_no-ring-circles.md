# Learnings — 2026-07-21 no-ring-circles

## 1. What this change delivered
- Removed the **drawn ring guide circles** from the ringed HTML view
  (`cloudbreachgraph-to-html --ringed`). The concentric-ring *layout* is unchanged — nodes are
  still positioned on rings (VPC center → subnets → ENIs → everything else, with subnets/outer
  nodes angle-aligned to their ENIs). Only the faint stroked circles that traced each ring are
  gone; the rings are now conveyed purely by node position, as requested.
- Files: `src/cloudbreachgraph/output/html_export.py` (the `_RINGED_TEMPLATE` `draw()`
  function) plus doc/comment updates in `README.md`, `docs/02_architecture.md` was already
  accurate. Test comment + a guard assertion in `tests/test_output.py`.

## 2. Interface contract for the next session
- **No payload change.** `_ringed_view_data` still emits `clusters: [{cx, cy, rings, label}]`.
  The `rings` list (radii of rings 1..3) is **retained** and still used — for the cluster
  label's vertical offset and for `autoCenter`'s fit-to-view bounds — it is just no longer
  stroked as circles. So `build_ringed_html` / `write_ringed_html` signatures and the
  size-guard/`.dot`-fallback contract are all unchanged.
- The cluster **label** (VPC name above each cluster) is kept.

## 3. Decisions & rationale
- Kept the `rings` data rather than ripping it out: it's layout metadata (label placement +
  view fit), not itself a drawn ring, so keeping it is the minimal, low-churn change and keeps
  the existing `cluster["rings"]` layout tests (which validate the radii logic) meaningful.
- Kept the per-cluster VPC label — the user asked only to stop drawing the *rings*, not the
  labels, and without ring circles the label is the remaining cluster affordance.

## 4. Deviations from the plan
- None.

## 5. Gotchas
- The node disks are also drawn with `ctx.arc(p.x, p.y, r, ...)`; the removed ring circles used
  `ctx.arc(p.x, p.y, rr * scale, ...)`. The regression guard in
  `test_write_ringed_html_is_self_contained` asserts `"ctx.arc(p.x, p.y, rr" not in text`,
  which is specific to the ring loop (`rr`) and won't false-match the node arc (`r`).

## 6. Known gaps / follow-ups
- If a future change wants to drop the `rings` array from the payload entirely, `autoCenter`
  and the label offset would need to derive a per-cluster radius from node positions instead.

## 7. Git note (this session)
- PR #13 (subnet/outer angular alignment) was **merged** into `main`. This change starts a
  **fresh branch** `claude/cloudbreachgraph-no-ring-circles` from the post-merge `main`.

## 8. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 124 tests, all offline
ruff check . && ruff format --check .
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
cloudbreachgraph-to-html /tmp/cbg-out/graph.json --ringed -o /tmp/r.html
# Open /tmp/r.html: nodes sit on concentric rings, but no ring circles are drawn.
```
