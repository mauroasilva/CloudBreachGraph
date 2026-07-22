# Learnings — 2026-07-22 overlap-free-layout

## 1. What this change delivered
- New **overlap-free HTML layout** for `cloudbreachgraph-to-html`, driven by a new
  `--max-passes N` flag (the "max number of passes for graph optimisation").
- `src/cloudbreachgraph/output/html_export.py`:
  - `_optimize_layout(nodes, edges, max_passes) -> int` — computes deterministic x/y that leave
    **0 node-node overlaps** and **0 edge-over-node overlaps**; returns passes used.
  - `_seg_point_dist(...)` — point-to-segment distance helper (used by projection + counting).
  - `_count_overlaps(nodes, edges) -> (node_node, edge_node)` — strict overlap counter using the
    drawn radii (`_NODE_RADII`); the verification/acceptance oracle.
  - `_optimized_view_data`, `build_optimized_html`, `write_optimized_html` — payload/page/writer,
    mirroring the ringed trio's signatures and size-guard contract.
  - Refactor: the ringed page template `_RINGED_TEMPLATE` became a **shared** draw-only template
    `_STATIC_TEMPLATE` with `__VARIANT__`/`__HINT__` placeholders, filled by the new
    `_render_static_layout(data_json, variant, hint)`. Both ringed and overlap-free layouts render
    through it (ringed keeps variant "ringed"; overlap-free uses "overlap-free"). Hints are
    `_RINGED_HINT` / `_OPTIMIZED_HINT`.
- `src/cloudbreachgraph/convert.py`: added `--max-passes N` (int, default 0). When `>0` it renders
  the overlap-free layout and **takes precedence** over `--ringed`/`--optimize-passes` (warns that
  those are ignored). Negative value → exit 2, like `--optimize-passes`.
- Tests: `tests/test_convert.py` (overlap-free section) + `tests/test_output.py` (self-contained /
  deterministic / size-guard fallbacks). Docs: `README.md`, `docs/02_architecture.md §7`.

## 2. Interface contract for the next change
- `write_optimized_html(graph, path, *, max_nodes=None, max_bytes=None, max_passes=0) -> Path|None`
  — identical contract to `write_ringed_html` (returns `None`, writes nothing, when the graph
  exceeds `MAX_NODES`/`MAX_HTML_BYTES`; caller falls back to `.dot`).
- `_optimized_view_data(graph, max_passes)` returns the same payload shape as `_ringed_view_data`
  but with `clusters: []` (no rings). The shared `_STATIC_TEMPLATE` renders `clusters:[]` fine
  (no labels, view fit to node bounds).
- **"Edge overlap" is defined here as edge-over-node**, not edge crossings — see §3/§5. If a later
  change wants crossing counts, that's a different metric (`test_convert._count_crossings` exists
  for the ringed tests).
- Determinism guarantees rest on: fixed spiral init, fixed PRNG seed `_OPT_SEED` (only jitters
  exactly-coincident nodes), fixed iteration order over already-sorted nodes/edges, and 2-dp
  rounding. Keep all four if you touch the optimizer or output stays byte-unstable.

## 3. Decisions & rationale
- **"Edge overlap" ⇒ edge-over-node, not crossings.** The acceptance criteria asks for *0* edge
  overlap. I proved (networkx `check_planarity`, throwaway analysis only — **not** a runtime dep)
  that the example graph and its largest VPC cluster are **non-planar**, so a straight-line
  drawing with 0 edge *crossings* is mathematically impossible. The only reading of "0 edge
  overlap" that can be met is the one parallel to "0 node overlap": no edge drawn across a
  non-incident node. That is what the optimizer guarantees.
- **Two-phase optimizer (force unfold → hard projection).** A pure force sim freezes with ~61
  crossings and residual overlaps; greedy/annealed crossing search plateaus (14 / 7 crossings) and
  is slow. But *overlap* elimination is easy given room: phase 1 (cooled degree-weighted
  repulsion + springs + weak gravity, capped at `_OPT_FORCE_PASSES=400`) spreads nodes out; phase
  2 projects overlaps away (separate disks, push nodes off edges) until a sweep moves nothing.
  On the example graph this converges in **~402 passes (~1.2s)** — far under the 10000 budget.
- **A sweep that moves nothing = a zero certificate.** Projection only moves when a violation
  exists, so `moved == False` proves `_count_overlaps == (0, 0)`; no separate re-scan needed.
- **`--max-passes` is a distinct layout, precedence over ringed knobs.** Cleaner than overloading
  the ring-constrained `--optimize-passes` (whose barycenter/relocation moves can't leave the
  rings and so can't clear edge-over-node overlaps that radial edges create).
- **Shared template refactor** instead of a second ~230-line template copy — keeps modules small
  per `docs/04_conventions.md`.

## 4. Deviations from the plan
- None structurally. `docs/05_roadmap.md` roles untouched (no new resource type). This is a
  new-CLI-flag + output change, exactly the pattern in the change brief §4.

## 5. Gotchas & surprises
- **Non-planarity is the whole story.** Don't waste a future session trying to reach 0 edge
  *crossings* on real captures — it's impossible (K5/K3,3 minors appear inside a single VPC).
- Phase 2 can in principle oscillate (fixing one overlap creates another); the phase-1 room is
  what makes it converge. If a future graph doesn't converge, increase `_OPT_FORCE_PASSES` or the
  init spiral scale (`30.0 * sqrt(i)`) before touching the projection push.
- Budget split: projection is guaranteed ≥ `_OPT_PROJECT_MIN` passes when affordable, so a small
  `--max-passes` (e.g. 100) still spends some budget projecting rather than only unfolding.
  Small budgets are best-effort (won't reach 0/0) — that's intended.
- Runtime is O(N²) per force pass and O(N·E + N²) per projection sweep; ~1.2s for the 124-node
  example. The `MAX_NODES=1500` size guard keeps worst cases bounded, but a very large in-guard
  graph could be slow — acceptable for a one-off local render.
- networkx was `pip install`ed **only** to settle planarity during analysis; it is **not** in
  `pyproject.toml` and nothing at runtime imports it. Runtime stays stdlib-only.

## 6. Known gaps / follow-ups
- The overlap-free layout ignores edge *crossings* entirely (can't be zeroed). If a future
  session wants "fewest crossings *and* no overlaps", it'd need a crossing term added to phase 1
  and would still not hit zero.
- Not wired into the main `cloudbreachgraph --html` path (that renders a browser-side force sim,
  no Python positions). Kept scope to the HTML-generation command the acceptance uses.
- No perf tuning for near-`MAX_NODES` graphs beyond the existing byte/node guard.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 167 passing; incl. overlap-free tests
ruff check . && ruff format --check .
# Acceptance: reach 0 node + 0 edge overlap on the example graph in <10000 passes.
cloudbreachgraph-to-html docs/examples/example-graph.json --max-passes 10000 -o /tmp/opt.html
python - <<'PY'
import json, re
from cloudbreachgraph.output import html_export as H
html = open("/tmp/opt.html").read()
data = json.loads(re.search(r"const GRAPH = (\{.*?\});", html, re.S).group(1))
print("overlaps (node, edge):", H._count_overlaps(data["nodes"], data["edges"]))  # (0, 0)
PY
```
