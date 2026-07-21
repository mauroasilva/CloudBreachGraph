# Learnings — 2026-07-21 recompute-layout-button

> Follow-on change after the HTML force-layout work (merged in PR #9). Same-day, different
> slug. Branch: `claude/recalc-layout-button`, cut fresh from `origin/main` (the prior
> branch's PR was already merged — do not stack on merged history).

## 1. What this change delivered
All within the inline JS/CSS template in **`src/cloudbreachgraph/output/html_export.py`**
(no Python API change, no new module):
- **Cluster separation.** Connected components are computed once (BFS over the existing
  `n.adj` adjacency) and stamped as `n.comp`; `componentCount` holds the total. In the
  pairwise repulsion loop, nodes in *different* components repel `CROSS_COMPONENT` (= 4) ×
  harder, so disconnected clusters (separate VPCs, `--include-orphans` orphans) drift apart
  instead of mingling. Single-component graphs (the common ENIs→subnets→VPC case) are
  unaffected — the multiplier only ever applies across components.
- **"↻ Recompute layout" button** in the HUD (`#controls > button#recompute`, themed for
  light/dark). Its handler `recompute()` releases every manual pin (`n.fixed = false`), zeroes
  velocities, reheats the sim (`alpha = 1.0`), and clears `centered` so `autoCenter` re-fits
  the viewport. The running `frame()` loop then animates the re-layout; cross-component
  repulsion re-separates the clusters. This is the answer to "recompute node positions after I
  made manual adjustments so segregated clusters sit apart."
- **Test:** `tests/test_output.py::test_write_html_has_recompute_button_and_cluster_separation`
  asserts the button markup, the `recompute()` handler + click wiring, and the
  `CROSS_COMPONENT` / `a.comp !== b.comp` / `componentCount` layout logic are present.

## 2. Interface contract for the next change
- Still one public entry: `html_export.build_html(graph) -> str` / `write_html(...) -> Path |
  None`. **No signature change.** All new behavior is client-side JS inside the emitted page.
- New runtime JS symbols in the template if you touch it: `CROSS_COMPONENT` (const),
  `componentCount` + `n.comp` (component id per node), `recompute()` (button handler). The
  HUD now has a `#controls` block and a `#recompute` button — the crossing/overlap and
  determinism guarantees are unchanged.
- The converter (`graph_io` / `cloudbreachgraph-to-html`) is unaffected: it feeds the same
  `write_html`, so converted pages get the button + cluster separation for free.

## 3. Decisions & rationale
- **Recompute releases all pins** (rather than keeping them as anchors). "Recalculate node
  positions" reads as "let the auto-layout take over again"; keeping pins would fight the
  re-separation the user asked for. Pinning is one drag away if they want it back.
- **Reheat, don't re-seed.** `recompute()` keeps current positions and just reheats `alpha`,
  so the re-layout animates smoothly from where things are (visible feedback) instead of
  snapping to a fresh spiral. Cross-component repulsion does the separating.
- **Component repulsion as a pairwise multiplier**, not a separate centroid-repulsion pass —
  it rides the existing O(n²) repulsion loop (zero extra passes) and is trivially correct.
  `CROSS_COMPONENT = 4` sets the equilibrium separation (where cross-cluster repulsion
  balances the global gravity that centers everything); raise it if clusters still sit close.
- **BFS over `n.adj`** reuses the adjacency already built for the angular-resolution force;
  components are computed once at load, not per tick.

## 4. Deviations from the plan
None. Additive change to the HTML output documented in `docs/02_architecture.md §7`; no change
to §5 rules, the graph model, collectors, or the Python output API.

## 5. Gotchas, surprises & quirks
- Global gravity pulls all components toward center, so cluster separation is an *equilibrium*
  (gravity vs. cross-component repulsion), not infinite drift — clusters arrange *around* the
  center, well-spaced, and stay on-screen. Tuning `CROSS_COMPONENT` vs. `GRAVITY` sets the gap.
- Determinism is preserved: the button and component logic are runtime-only; the emitted HTML
  is still byte-stable (verified: two `--html` runs diff-identical), so all existing tests and
  the lossless JSON→HTML round-trip still hold.
- Verified in headless Chromium (`/opt/pw-browsers`, Playwright `executable_path`) on a
  2-VPC graph: 2 components detected, ~298px centroid separation on load; after jamming all
  nodes to the origin and pinning them (sep → 16px), clicking `#recompute` released the pins
  and re-separated the clusters to ~310px with **0** node overlaps and no console errors. Do
  **not** run `playwright install` here.

## 6. Known gaps / TODO for later
- No per-cluster convex-hull tint or labeled cluster boxes; separation is positional only.
- `recompute()` is all-or-nothing on pins; a "keep pinned, reflow the rest" variant could be a
  future toggle if users want to anchor a few nodes and only re-separate the rest.
- `CROSS_COMPONENT` is a fixed const in the template — no CLI/JS control to tune it live.

## 7. How to verify this change
```bash
pip install -e '.[dev]'
pytest                     # 108 pass (incl. the new recompute/cluster-separation assertion)
ruff check . && ruff format --check .

# Generate a page and open it; drag nodes around, then click "↻ Recompute layout".
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out --html
open /tmp/cbg-out/graph.html
# A multi-VPC / --include-orphans graph best shows the cluster separation (separate stars
# settle apart, and Recompute re-separates them after you pile them together).
```
