# Learnings — 2026-07-22 split-graph-by-vpc

## 1. What this change delivered
- **`cloudbreachgraph-to-html --split-by-vpc`**: writes one self-contained HTML per VPC,
  named `graph-<VPC ID>.html`, instead of a single combined page. On the shipped 4-VPC
  example graph it produces exactly 4 files (the acceptance criterion).
- **`html_export.split_by_vpc(graph) -> dict[str, Graph]`** (new public function in
  `src/cloudbreachgraph/output/html_export.py`): partitions a `Graph` into one sub-`Graph`
  per VPC. Reuses the existing `_vpc_group_of(graph)` node→VPC tracing (the same mapping the
  ringed layout clusters by). Each sub-graph = the nodes that resolve to that VPC + every edge
  whose *both* endpoints resolve to the same VPC. Unassigned nodes (`_UNASSIGNED`) and cross-VPC
  edges are dropped. `meta` is copied onto each sub-graph. Result is ordered by VPC id for
  deterministic file emission; empty dict when the graph has no `vpc` nodes.
- **`convert.py`** refactor: extracted the "write one HTML, else fall back to `.dot`" logic
  out of `main()` into a module-level helper `_emit(graph, out_path, *, ringed, optimize_passes,
  protect)`. `protect` is the input path to avoid clobbering with the `.dot` fallback (None in
  split mode, since `graph-<id>.html/.dot` never collide with the input). Added the
  `--split-by-vpc` flag and a split branch in `main()`.
- Docs: `README.md` (examples + new "Splitting per VPC" subsection), `docs/02_architecture.md`
  §7 converter paragraph. Tests in `tests/test_convert.py`.

## 2. Interface contract for the next session
- `html_export.split_by_vpc(graph: Graph) -> dict[str, Graph]` — VPC id → sub-graph, sorted by
  id, `meta` copied. This is the reusable primitive; any future "per-VPC anything" (e.g. per-VPC
  JSON/DOT export) should build on it rather than re-deriving VPC membership.
- All three layouts split because split mode calls the same `write_layout_html(sub, path,
  ringed=…, optimize_passes=…)` per sub-graph — the single three-way layout selector. Nothing
  layout-specific was added; a future 4th layout wired into `write_layout_html` splits for free.
- CLI contract: `--split-by-vpc` treats `-o` as a **directory** (default: the input file's
  directory), `mkdir(parents=True, exist_ok=True)`. Returns rc 2 with a message when the graph
  has no VPCs. Per-file size guard / `.dot` fallback are unchanged, just applied per sub-graph.

## 3. Decisions & rationale
- **Reused `_vpc_group_of` instead of writing new grouping.** It already traces
  subnet→vpc, eni→subnet, ec2/lb←eni, eni→sg, and reach-source→sg/eni, and handles the
  shared-source-fans-into-many-VPCs case deterministically (grouped with the first). Splitting is
  exactly that grouping + edge filtering, so duplicating it would risk drift.
- **`split_by_vpc` lives in `html_export.py`**, next to `_vpc_group_of`, rather than in a new
  module — it's a thin consumer of a private function in that module and the only caller is the
  HTML converter. Moving the grouping to a shared home would be a larger refactor for no current
  benefit. If a non-HTML consumer ever needs it, promote both together.
- **Edge inclusion = both endpoints in the same VPC.** Keeps each file a clean stand-alone
  picture. A shared reachability source assigned to VPC A keeps its edge to A's ENI; its edges
  to other VPCs' ENIs are dropped (the source isn't in those files). Accepted, deterministic.
- **`protect` parameter** on `_emit` generalises the old "don't overwrite the input `.dot`"
  guard so the single-file path keeps it and the split path opts out.

## 4. Deviations from the plan
- None. No config-grammar, model, or AWS/collector changes; purely local file I/O, read-only
  guarantee untouched. `split_by_vpc` constructs new `Node`/`Edge` objects (copying attrs) so
  sub-graphs never alias the source graph's mutable dicts.

## 5. Gotchas / surprises
- The shipped `docs/examples/example-graph.json` has exactly **4 VPCs**, so it doubles as the
  acceptance-criteria fixture — no new fixture needed. The recorded `tests/fixtures` build is
  single-VPC, so multi-VPC split tests load the example graph instead.
- `ruff format` reformats the new test file's long `sorted(...)` list comprehension — run
  `ruff format` before `--check`, not after.

## 6. Known gaps / follow-ups
- Only the HTML converter splits. There's no `cloudbreachgraph --html --split-by-vpc` on the
  main collection CLI and no per-VPC `graph.json`/`.dot` split — `split_by_vpc` would support
  both if wanted.
- The per-VPC `<title>` is still the generic "CloudBreachGraph"; the VPC id isn't shown in the
  page chrome (it is in the filename and the VPC node). Could inject it if desired.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest -q                                   # 233 passed
ruff check . && ruff format --check .       # clean
# End-to-end on the 4-VPC example graph:
cloudbreachgraph-to-html docs/examples/example-graph.json --split-by-vpc -o /tmp/cbg-split
ls /tmp/cbg-split   # graph-vpc-0a31....html + 3 more, one per VPC
# Layouts compose:
cloudbreachgraph-to-html docs/examples/example-graph.json --split-by-vpc --ringed -o /tmp/cbg-r
cloudbreachgraph-to-html docs/examples/example-graph.json --split-by-vpc --optimize-passes 50 -o /tmp/cbg-o
```
