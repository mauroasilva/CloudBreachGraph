# Learnings — 2026-07-22 unify-optimize-passes-main-cli

## 1. What this change delivered
- Wired the **overlap-free layout** into the main `cloudbreachgraph` CLI: `cloudbreachgraph
  --html --optimize-passes N` now writes the deterministic overlap-free `graph.html` (no
  overlapping nodes, no edge crossing a node, fewer crossings, clusters kept apart) instead of
  the in-browser force layout. Works on live, `--from-cache`, and `--all-accounts` runs (all go
  through `cli._write_outputs`).
- **Unified `--optimize-passes` and `--max-passes` into one flag** (`--optimize-passes`) on the
  auxiliary `cloudbreachgraph-to-html`. `--max-passes` is **removed**.

## 2. Interface contract for the next change
- **One pass flag, layout-dependent meaning** (both CLIs):
  - `--optimize-passes 0` (default) → base layout (in-browser force, or plain ringed with
    `--ringed`).
  - `cloudbreachgraph-to-html --ringed --optimize-passes N` → ringed in-ring crossing reduction
    (unchanged; `html_export.write_ringed_html(passes=N)`).
  - `cloudbreachgraph-to-html --optimize-passes N` (no `--ringed`) → overlap-free layout
    (`write_optimized_html(max_passes=N)`).
  - `cloudbreachgraph --html --optimize-passes N` → overlap-free `graph.html`; `--optimize-passes`
    without `--html` prints a warning and is ignored.
- Negative `--optimize-passes` → exit code 2 on both CLIs.
- **No `--max-passes` anywhere** — don't reintroduce it.
- Internal `html_export.write_optimized_html(..., max_passes=...)` keyword is **unchanged**; only
  the CLI surface was renamed. The CLI maps `args.optimize_passes` → `max_passes=`.
- The main CLI still has **no `--ringed`** (out of scope); `--optimize-passes` there always means
  the overlap-free layout.

## 3. Decisions & rationale
- **Kept the name `--optimize-passes`** (dropped `--max-passes`) because it's the general term for
  "optimisation passes" and already existed for the ringed layout; the unified meaning is "run up
  to N optimisation passes; the kind depends on the active layout, 0 = none". If a future request
  prefers `--max-passes`, it's a pure rename of the CLI arg (+ help/docs/tests).
- **`--optimize-passes` requires `--html` on the main CLI** (warns + ignores otherwise) rather than
  implying `--html` — `--html` is opt-in and produces an extra file; silently turning it on from
  another flag would surprise. Mirrors the converter's old "only affects …" warning style.
- Selection order in `convert.main`: `--ringed` first (ringed reduction), else `optimize_passes > 0`
  (overlap-free), else plain force. Clean three-way, no precedence warnings needed anymore.

## 4. Deviations from the plan
- None. Pure CLI-surface + wiring change; the layout algorithms in `html_export.py` were not
  touched.

## 5. Gotchas & surprises
- Removing `--max-passes` broke three converter tests that encoded the *old* precedence semantics
  (`--max-passes` overriding `--ringed`, and `--optimize-passes` being "ringed only"). They were
  rewritten/removed to the unified behaviour — grep tests for a flag name before renaming it.
- The main-CLI `_write_outputs` is shared by live / `--from-cache` / `--all-accounts`, so wiring
  the branch there covered all three at once (and the size-guard `.dot` fallback still applies
  because `write_optimized_html` returns `None` over budget just like `write_html`).

## 6. Known gaps / follow-ups
- The main `cloudbreachgraph` CLI still exposes only the force / overlap-free layouts, **not**
  `--ringed`. If someone wants the ringed layout straight from a collection run, that'd be a
  further wiring change (add `--ringed` to `cli.py` and branch in `_write_outputs`).
- `--optimize-passes` help text now appears in both CLIs; keep them in sync if the semantics change.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 172 passing
ruff check . && ruff format --check .
# Main CLI, overlap-free straight from a collection run:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg --html --optimize-passes 10000
grep -c overlap-free /tmp/cbg/graph.html    # 1 (variant marker present)
# Converter, unified flag:
cloudbreachgraph-to-html docs/examples/example-graph.json --optimize-passes 10000 -o /tmp/opt.html
cloudbreachgraph-to-html docs/examples/example-graph.json --ringed --optimize-passes 20 -o /tmp/ring.html
```
