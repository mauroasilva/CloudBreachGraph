# Learnings — 2026-07-21 example-graph

## 1. What this change delivered
- Added a shipped, fully **anonymised** example graph at `docs/examples/example-graph.json`
  (a real-shaped multi-VPC topology: 4 VPCs, 28 subnets, 60 ENIs, 19 load balancers, 13 EC2
  instances; 124 nodes / 138 edges). It's a verbatim `graph.json` (the `Graph.to_dict()`
  shape) so it loads losslessly via `graph_io.load_graph` and feeds every consumer
  (`cloudbreachgraph-to-html`, `--ringed`, `--optimize-passes`).
- `README.md`: pointed the "Converting an existing graph to HTML" section at it so users can
  try the tool with no AWS account.

## 2. Interface contract for the next session
- None — pure data/doc addition. No code changed.

## 3. Decisions & rationale
- **Filename `example-graph.json`, NOT `graph.example.json`.** `.gitignore` line 26 is
  `graph*.json` (to drop generated `graph.json` outputs), which also matches
  `graph.example.json` → `git add` silently skips it (`git check-ignore` confirms). Any example
  graph committed under `docs/examples/` must avoid the `graph*.json` glob; `example-graph.json`
  does. (If you ever *must* keep a `graph*.json` name, `git add -f` works but is fragile.)
- Kept it a raw `graph.json` (not a `.dot`) so the JSON path — the lossless one — is exercised.

## 4. Deviations from the plan
- None.

## 5. Gotchas
- The `graph*.json` ignore rule is the one to remember here (see §3).
- The file is already anonymised (randomised names/IDs/IPs); safe to commit. Do not regenerate
  it from a real account.

## 6. Known gaps / follow-ups
- None. If more examples are added, consider a short `docs/examples/README.md` describing each.

## 7. Git note (this session)
- Branched from `origin/main` (`a1ae292`, which has the anonymize tool from PR #16). The ringed
  **optimizer** improvements (min-gap / cooling / crossing-reduction) are in the still-open
  PR #15 and are NOT on this branch — this example change is independent of them.

## 8. How to verify
```bash
pip install -e '.[dev]'
pytest && ruff check . && ruff format --check .
cloudbreachgraph-to-html docs/examples/example-graph.json --ringed -o /tmp/example.html
python -c "from cloudbreachgraph.graph_io import load_graph; print(len(load_graph('docs/examples/example-graph.json').nodes), 'nodes')"
```
