# Learnings — 2026-07-21 recompute-from-current

> Bug-fix follow-up to the "Recompute layout" button (added in PR #10, merged). User report:
> after hand-untangling the graph, clicking Recompute threw the arrangement away and re-solved
> into a fresh tangle. Branch `claude/recompute-from-current`, cut from `origin/main`.

## 1. What this change delivered
All inside the inline JS template of **`src/cloudbreachgraph/output/html_export.py`** (no
Python API change). The `recompute()` button now **refines the layout from the current
positions** instead of re-solving globally:
- **Root cause of the bug:** `recompute()` did `alpha = 1.0` (full reheat). At high alpha the
  integrator gives nodes large velocities that overshoot and wander into a *far-away* energy
  minimum — a different, tangled layout — discarding the user's hand arrangement.
- **Fix = make the current arrangement the equilibrium, then only relieve local crowding:**
  1. **Position anchors.** On recompute, each node stores `n.ax/n.ay = n.x/n.y`; the integrate
     loop adds a tether force `(n.ax - n.x) * ANCHOR` (ANCHOR = 0.08) that is **not**
     alpha-scaled, so as the sim cools every node returns to where it was except where
     decluttering moved it.
  2. **Neutralize springs.** Each edge's rest length `e.len` is re-anchored to its *current*
     length, so springs don't contract the arrangement back toward the default layout.
  3. **Drop centering gravity.** A new `gravityScale` (1 normally) is set to 0 on recompute, so
     nodes aren't dragged toward the origin. Gravity term is now `GRAVITY * gravityScale * alpha`.
  4. **Gentle reheat.** `alpha = RECOMPUTE_ALPHA` (0.1), not 1.0 — only repulsion, collision,
     angular spread and cross-component separation act, at low energy.
  - The viewport is left untouched (no `centered = false`) so the picture doesn't jump.
- **Drag-after-recompute fix.** Once anchored, releasing a dragged node would spring it back to
  its stale anchor. The mousemove drag handler now also updates `dragNode.ax/ay`, so a dragged
  node keeps its new spot.

## 2. Interface contract for the next change
- Public API unchanged: `html_export.build_html/write_html`. All behavior is client-side JS.
- New runtime JS state in the template: `ANCHOR` (const), `RECOMPUTE_ALPHA` (const),
  `gravityScale` (let, 0 after first recompute), `anchored` (let), per-node `n.ax/n.ay`. The
  integrate loop reads `gravityScale` and (when `anchored`) the anchor tether.
- **Semantics to preserve:** Recompute must stay a *local* refine (anchored, low alpha, no
  gravity, neutral springs). Do not reintroduce a high-energy reheat — that is the exact bug
  this fixes. If you want a "full re-layout from scratch" action, add it as a *separate*
  control; don't overload Recompute.

## 3. Decisions & rationale
- **Position anchoring over "just lower alpha".** A low alpha alone still drifts a lot: springs
  keep pulling edges to the default rest length and gravity keeps pulling to center. Measured
  ~190px mean drift on a spread test arrangement with low-alpha-only; anchors + neutral springs
  + gravity-off cut it to **~4px** (see §7). Anchoring is what actually means "from current
  positions."
- **Anchor tether not alpha-scaled**, while repulsion/cross-component are. So early in the
  settle the (alpha-scaled) declutter forces can push a crowded node out; as alpha → 0 those
  vanish and the constant anchor returns the node to its spot. Equilibrium ≈ original position,
  displaced only where collision/repulsion genuinely required it.
- **ANCHOR = 0.08, RECOMPUTE_ALPHA = 0.1** chosen empirically: strong enough tethering that a
  tidy arrangement barely moves, weak enough that overlapping piles still declutter (45→~1) and
  overlapping clusters still ease apart. Both are named consts at the top of the sim — tune
  ANCHOR up for stickier preservation, down for more aggressive tidying.
- **gravityScale stays 0 after the first recompute** (not restored). Once the user has taken
  manual control, re-centering on later interactions would fight them. The initial auto-layout
  already happened with gravity on.

## 4. Deviations from the plan
None vs. docs. This corrects the previous session's `recompute()` (documented in
`docs/02_architecture.md §7`, updated here). No change to §5 rules, the model, or collectors.

## 5. Gotchas, surprises & quirks
- **Degenerate input:** if *every* node is piled on the exact same pixel and all anchors
  coincide there, collision can't fully separate them against the anchors (test left 1 residual
  overlap out of 45). Not reachable from real manual dragging; acceptable.
- **Anchors persist**, so after a Recompute the graph is "sticky": dragging one node no longer
  reflows its neighbors (they're anchored). This is intended (stable layout) but is a behavior
  change from pre-recompute dragging — note it if a future request wants neighbors to follow.
- Determinism preserved: anchors/`gravityScale`/`anchored` are runtime state, never written to
  the file. Two `--html` runs are byte-identical (re-verified), so the JSON→HTML lossless
  round-trip and all existing tests still hold.
- Verified in headless Chromium (`/opt/pw-browsers`, Playwright `executable_path`); do **not**
  run `playwright install` here.

## 6. Known gaps / TODO for later
- No separate "re-layout from scratch" button (deliberately). If users want the old behavior
  too, add it as its own control rather than changing Recompute.
- ANCHOR/RECOMPUTE_ALPHA are fixed consts — no live slider to trade preservation vs. tidying.
- All-piled-on-one-point declutter leaves a small residual overlap (see §5).

## 7. How to verify this change
```bash
pip install -e '.[dev]'
pytest                     # 108 pass (incl. the updated recompute assertions)
ruff check . && ruff format --check .

cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out --html
open /tmp/cbg-out/graph.html
# Drag nodes into a tidy arrangement, then click "↻ Recompute layout": it should stay put
# (only overlaps/crowding get relieved), NOT snap back to a tangle.
```
Headless measurement used during development (drift + declutter): impose a hand arrangement,
pin, call `recompute()` via the page, wait, then compare positions — mean drift on a tidy
arrangement ≈ 4px (was ~190px with a plain low-alpha reheat), overlapping piles resolve, and
`n.fixed`/anchor state produce no console errors.
