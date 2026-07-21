# Learnings — 2026-07-21 html-force-layout

## 1. What this change delivered
- **New output module `src/cloudbreachgraph/output/html_export.py`.** Renders a `Graph` as a
  single, fully **self-contained** interactive HTML page (inline CSS + JS, no CDN, no network,
  no third-party runtime dep — honours `docs/04_conventions.md`). The page draws the graph on
  an HTML5 canvas and runs a small vanilla-JS **force simulation** that self-distributes nodes
  (pairwise repulsion + edge springs + hard collision separation), with drag-to-pin, wheel
  zoom, and background pan. Nodes are colored by type (palette mirrors `dot_export._TYPE_STYLE`),
  synthetic nodes are dashed, and ENIs with a public IP get a red "exposed" outline + legend
  entry.
  - `build_html(graph) -> str` — builds the document (data injected via `str.replace` on
    `__GRAPH_DATA__`/`__NODE_COUNT__`/`__EDGE_COUNT__` sentinels, **not** `.format`/`%`, so the
    CSS/JS braces need no escaping).
  - `write_html(graph, path, *, max_nodes=None, max_bytes=None) -> Path | None` — writes the
    page, or returns **`None` without writing anything** when the graph is too large
    (`> MAX_NODES` nodes or the rendered page `> MAX_HTML_BYTES`). `None` is the signal to fall
    back to `.dot`.
  - `_view_data(graph)` ships a **trimmed** payload (id/type/label/color/synthetic/public/detail
    per node; source/target/rel per edge) rather than the full `to_dict()`, to keep the inlined
    JSON — and thus the page — small.
  - Module constants `MAX_NODES = 1500`, `MAX_HTML_BYTES = 8 MiB`. Resolved **at call time**
    inside `write_html` (defaults are `None` params → module global) so tests/callers can
    monkeypatch the constant and have it take effect.
- **CLI: new `--html` flag** (`cli.py`, output group). Opt-in only — JSON+DOT remain the
  defaults. In `_write_outputs`, when `--html` is set we call `write_html`; on `None` we print a
  warning to stderr and rely on the always-written `.dot` as the fallback. Works for
  `--from-cache` and `--all-accounts` (per-account `graph.<alias>.html`) with no extra wiring,
  since it lives in the shared `_write_outputs`.
- **Tests:** `tests/test_output.py` (self-contained/no-external-refs, public-exposure flag,
  determinism, byte + node-count fallback, small-graph default-allow) and `tests/test_cli.py`
  (not-written-by-default, `--html` end-to-end alongside json/dot, fallback-to-dot warning via
  monkeypatched `MAX_NODES`).
- **Docs:** `README.md` (flags table + Outputs), `docs/02_architecture.md §7`.

## 1a. Follow-up in the same session: edge-crossing reduction
The canvas force sim in the HTML template was extended to actively minimise **edge
crossings** (the initial version left visible crossings on the fixture graph):
- **Degree-weighted repulsion** — each node gets `charge = REPULSION*(1+0.7*sqrt(deg))` and
  the pairwise force uses `sqrt(charge_a*charge_b)`, so hubs push their neighbors apart and
  star clusters unfold. Biggest single lever for these hub-and-spoke topologies.
- **Angular-resolution force** — for every node with `deg>=3`, its spokes are sorted by
  angle and adjacent pairs closer than the ideal even gap are nudged apart *tangentially*
  (rotated around the hub). Uses a precomputed per-node adjacency list (`n.adj`, built once)
  so the pass is **O(edges)/tick**, not O(nodes*edges).
- **Per-edge rest length** — `e.len = 70 + 9*sqrt(max degree)` gives hub spokes room.
- **Synchronous warm-up** before first paint — `WARMUP` iterations, bounded by a work budget
  `clamp(60, 500, 3e7/n²)` so a near-`MAX_NODES` graph still loads fast; the page opens
  already relaxed instead of visibly untangling from an early (tangled) local minimum.
- Result on the checked-in fixtures: crossings **2 → 0** (measured in headless Chromium with
  a segment-intersection counter), node-disk overlaps still 0, output still deterministic.
- **Caveat (told to the user):** minimizing crossings globally is NP-hard; these are
  heuristics. Dense/large graphs still show crossings — that regime is where the `.dot`
  fallback (Graphviz's hierarchical/orthogonal layouts) is the better view anyway.
- New tunables live as JS consts in the template: `REPULSION`, `SPRING`, `ANGULAR`,
  `GRAVITY`, `DAMPING`, `WARMUP`. Adjust `ANGULAR` (spoke spreading) / `REPULSION` first.

## 1b. Follow-up in the same session: graph.json / graph.dot → HTML converter
Added an auxiliary tool to render the HTML view from an *already-written* graph file (no
AWS collection), for people who ran without `--html` or only have a `--from-cache`/colleague
capture.
- **`src/cloudbreachgraph/graph_io.py`** — the inverse of the output writers:
  - `graph_from_dict(dict) -> Graph` and `load_json(path) -> Graph`: **lossless** inverse of
    `Graph.to_dict()` (rebuilds every node/edge attribute). Round-trip is byte-identical.
  - `load_dot(path) -> Graph`: **best-effort** parser for *this tool's own* DOT only (not
    general Graphviz). Regex-matches node/edge statements, splits the label on `\n`, and
    recovers id, type (from the `[type]` first label line), name (`<id> [<name>]`),
    `synthetic` (from `style="...dashed"` or the `(unresolved)` line), `private_ips`/
    `public_ips` (from the `Private IP:`/`Public IP:` lines), and the single per-type bare
    attribute (`interface_type`/`lb_type`/`cidr`/`state`). The DOT-only `Internet` node +
    its `public_ip` edges are folded back into `public_ips` on the source ENI (inverse of
    what `dot_export` adds), so the reconstructed graph matches the *model*, not the drawing.
  - `load_graph(path, fmt="auto")` dispatches by extension (`.json` / `.dot`|`.gv`) or an
    explicit `fmt`. Errors raise `GraphLoadError`.
- **`src/cloudbreachgraph/convert.py`** — new console entry point
  `cloudbreachgraph-to-html = "cloudbreachgraph.convert:main"` (added to `pyproject.toml`).
  Loads the file, calls the same `html_export.write_html`, and on the too-big path writes a
  `.dot` fallback via `dot_export` (skipping the write when the input already *is* that
  `.dot`, so the source is never clobbered). `main(argv)` returns `2` on load errors, `0`
  otherwise — mirrors `cli.py` conventions.
- **Verified:** JSON→HTML is **byte-identical** to `--html`'s direct output; DOT→HTML
  recovers all 10 fixture nodes, 9 edges, the public flag, and every per-type detail line
  (incl. colon-heavy ELB ARNs). Both render error-free in headless Chromium. Tests in
  `tests/test_convert.py` (17). Separate entry point (not a subcommand) keeps the main CLI's
  parser untouched.
- **Known gap:** `load_dot` is coupled to `dot_export`'s exact label format — if that emitter
  changes (label line order, new attribute lines), update the parser + its tests together.
  JSON is the safe, lossless path; prefer it when both files exist.

## 2. Interface contract for the next change
- Output writers live in `src/cloudbreachgraph/output/`. The HTML writer signature is
  `write_html(graph, path, *, max_nodes=None, max_bytes=None) -> Path | None`. **`None` means
  "too big, nothing written" — callers must handle it** (the CLI falls back to `.dot`). This is
  a deliberate deviation from the json/dot writers, which always return a `Path`.
- `write_html` reads the module globals `html_export.MAX_NODES` / `html_export.MAX_HTML_BYTES`
  at call time. Monkeypatch the module attribute (not a default arg) to change the threshold in
  a test.
- Node color palette is duplicated between `dot_export._TYPE_STYLE` and
  `html_export._TYPE_COLORS` (HTML uses slightly more saturated fills for canvas legibility).
  If you add a node **type**, update **both**.

## 3. Decisions & rationale
- **Vanilla JS + canvas, data inlined as JSON** — the only way to meet "zero required
  third-party runtime dependency" and "self-contained / offline". No D3/vis.js/CDN. Canvas
  (not SVG/DOM nodes) keeps it responsive into the low thousands of nodes.
- **Size guard on node count first, bytes second.** Browser force-layout cost is O(n²) per
  tick, so node count — not file size — is what actually makes a page "unreasonable to load";
  the byte cap is a secondary backstop for pathological attribute payloads. `1500` is a
  conservative "still smooth" ceiling; tune if needed.
- **Fallback = the existing `.dot`.** `graph.dot` is always written, and Graphviz lays out
  arbitrarily large graphs offline, so "warn + fall back to .dot" needed no new output path —
  just a stderr warning and *not* writing the HTML.
- **Determinism kept** (a hard rule): nodes/edges are already sorted by `Graph`; JSON dumped in
  that order; the in-browser layout is seeded from a fixed mulberry32 PRNG so the page is
  byte-stable and the graph relaxes the same way every load. No timestamps.
- **Injection via `str.replace` sentinels**, not `.format`/`%`, so the large CSS/JS block
  (full of `{`/`}`) needs no escaping.

## 4. Deviations from the plan
- `docs/02_architecture.md §7` previously listed only JSON, DOT, and optional `--render`. HTML
  is a new, additive output format; §7 and the README were updated to document it. No change to
  §5 relationship rules, the graph model, or the collectors.

## 5. Gotchas, surprises & quirks
- A `write_html` that returns `None` on "too big" **must not create the file** — tests assert
  the path does not exist on fallback. Build the HTML in memory, check size, then write.
- Because `MAX_NODES` is read at call time, the CLI warning string interpolates
  `html_export.MAX_NODES`; a monkeypatched value shows up in the message (fine — tests assert on
  "too large"/"graph.dot", not the number).
- Verified the page in the pre-installed Chromium (`/opt/pw-browsers`, via Playwright with
  `executable_path`): **no** runtime/console errors and **0** overlapping node pairs after the
  sim settles (self-distribution works). Do **not** run `playwright install` in this env.

## 6. Known gaps / TODO for later
- O(n²) repulsion has no Barnes–Hut/quadtree approximation; that (plus a higher `MAX_NODES`)
  is the obvious follow-up if larger interactive graphs are wanted.
- The type→color palette is duplicated across the DOT and HTML writers; a shared constant would
  remove the "update both" footgun.
- No node search/filter or click-to-focus in the page yet; label decluttering is just a
  zoom threshold.
- If a future "Internet" synthetic node is desired in the HTML (the DOT adds one for public
  ENIs), it would need replicating here; today public exposure is shown via a red node outline
  + legend instead.

## 7. How to verify this change
```bash
pip install -e '.[dev]'
pytest                     # 107 tests pass (HTML output, CLI, converter round-trips)
ruff check . && ruff format --check .

# End-to-end, offline, against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out --html
#   -> writes graph.json, graph.dot, graph.html
open /tmp/cbg-out/graph.html      # (or any browser) — self-contained, works offline

# Convert existing files to HTML (no AWS calls):
cloudbreachgraph-to-html /tmp/cbg-out/graph.json   # lossless -> /tmp/cbg-out/graph.html
cloudbreachgraph-to-html /tmp/cbg-out/graph.dot -o /tmp/cbg-out/from_dot.html  # best-effort

# Exercise the size-guard fallback (skips HTML, keeps .dot, warns on stderr):
python - <<'PY'
from cloudbreachgraph.output import html_export
html_export.MAX_NODES = 0
from cloudbreachgraph import cli
cli.main(["--from-cache", "tests/fixtures", "--output-dir", "/tmp/cbg-fallback", "--html"])
PY
```
