# Learnings — Phase 3 (Output, Visualization & End-to-End CLI)

> Prior-phase learnings: `docs/learnings/learnings_phase1.md` and
> `docs/learnings/learnings_phase2.md` **both exist and are complete**. Phase 3 coded
> directly against Phase 1's `config`/`collect_all` API and Phase 2's `Graph.to_dict()`
> contract — no reconstruction was needed. This file is the last of the v1 build; there is
> no Phase 4 in scope (see §6 for what a future `flow_logs` phase inherits).

## 1. What this phase delivered

- **`output/json_export.py`** — `write_json(graph, path) -> Path`. Renders
  `Graph.to_dict()` as pretty (`indent=2`, `ensure_ascii=False`) JSON, creates parent
  dirs, returns the path. **No wall-clock timestamp** is added, so output is byte-stable
  and diffable (Phase 2 deliberately kept `generated_at` out of `to_dict()` for the same
  reason — honored here rather than stamped at write time).
- **`output/dot_export.py`** — Graphviz DOT emitter (plain text, zero deps):
  - `write_dot(graph, path) -> Path`. Nodes colored + shaped by type; `synthetic`/
    `unresolved` nodes get a dashed outline; a VPC's *contents* (subnets/ENIs, plus
    instances/LBs when their `vpc_id` is known) are grouped inside `subgraph cluster_<vpc>`,
    while the **VPC itself is a standalone top-level node** the subnets connect up to via
    `in_vpc` edges (it is *not* nested inside its own cluster — see §3); edges labeled by
    `relationship`, with `match_rule` appended on load-balancer attachment edges.
  - `dot_available() -> bool` and `render(dot_path, fmt) -> Path | None`. `render` shells
    out to `dot -T<fmt>` **only if** `dot` is on `PATH`; returns `None` when absent (caller
    warns, `.dot` still written), raises `RuntimeError` (surfacing stderr) if `dot` runs
    but fails.
  - `output/__init__.py` re-exports `write_json`, `write_dot`, `render`, `dot_available`.
- **`cli.py`** — `main(argv=None) -> int`, the argparse entrypoint wiring
  `load_config → resolve_target → verify_target → collect_all → build_graph →
  write_json/write_dot`. Full flag surface in §2. `__main__.py`'s Phase-1 stub now resolves
  to this (`python -m cloudbreachgraph` works).
- **Tests** — `tests/test_output.py` (9) + `tests/test_cli.py` (10). Total suite now
  **81 tests, fully offline** (30 Phase 1 + 35 Phase 2 + 16 Phase 3).
- **`README.md`** — rewritten with real usage: config file, `--target`, `--account`
  (alias & id), `--profile` override, verification, live run, `--cache-dir`/`--from-cache`,
  `--render`, `--all-accounts`, and a note that `flow_logs` is a future role.

Layout matches `docs/02_architecture.md §2` exactly — no structural deviations. `pyproject`
already declared the `cloudbreachgraph = cloudbreachgraph.cli:main` console script in
Phase 1; Phase 3 just supplied the module, so **no `pyproject` change was needed**.

## 2. Interface contract / final CLI surface

`cloudbreachgraph [flags]` (also `python -m cloudbreachgraph`). Exit codes: `0` success,
`1` AWS CLI error (`AwsCliError`), `2` config/resolution/verification error (`ConfigError`).

| Flag | Purpose |
|------|---------|
| `--target NAME` | config target binding roles→accounts (multi-account capable) |
| `--account ALIAS\|ID` | shorthand: a target whose every role is that one account |
| `--profile NAME` | escape hatch: this profile for **all** roles, bypasses the mapping |
| `--config PATH` | explicit config path (else discovery: `./cloudbreachgraph.toml` → XDG) |
| `--verify-account / --no-verify-account` | toggle `sts get-caller-identity` check; default **on when an account id is known** |
| `--all-accounts` | loop every `[accounts.*]`, writing `graph.<alias>.json`/`.dot` |
| `--region REGION` | region override (else per-account default) |
| `--cache-dir DIR` | also dump raw AWS JSON responses (via `runner.configure_cache`) |
| `--from-cache DIR` | rebuild from cached JSON, **no live AWS calls** |
| `--include-orphans` | pass `include_orphans=True` to `build_graph` (Phase 2 §6 flag) |
| `--output-dir DIR` | output location (default `.`) |
| `--render {png,svg}` | rasterize the `.dot` via `dot` (graceful if absent) |

Example invocations:

```bash
cloudbreachgraph --from-cache tests/fixtures --output-dir out/        # offline, no AWS
cloudbreachgraph --account workload_prod --output-dir out/           # one account by alias
cloudbreachgraph --account 111111111111 --region us-west-2           # by id + region override
cloudbreachgraph --target prod --render svg                          # target + render
cloudbreachgraph --account prod --profile other-profile              # --profile wins
cloudbreachgraph --all-accounts --output-dir out/                    # one graph per account
cloudbreachgraph --account prod --cache-dir cap/ && \
  cloudbreachgraph --from-cache cap/ --output-dir out/               # capture then rebuild
```

Output files (all in `--output-dir`): `graph.json`, `graph.dot`, and `graph.<fmt>` with
`--render`. `--all-accounts` uses the `graph.<alias>.*` naming.

## 3. Decisions & rationale

- **`--from-cache` reuses the real collectors by swapping `runner.run_aws`.** Rather than
  re-implementing normalization in the CLI, `_collect_from_cache` temporarily replaces the
  module-level `runner.run_aws` with a disk reader (`_make_cache_reader`) and calls the
  normal `collect_all` (restored in a `finally`). This mirrors the documented smoke-test
  pattern in `learnings_phase2.md §7` and keeps normalization in exactly one place.
- **Cache filename lookup accepts two conventions.** The reader tries `<a>-<b>-<c>.json`
  (the runner's own `--cache-dir` key format from `runner._cache_key`) **and**
  `<service>_<rest>.json` (the `tests/fixtures` naming, e.g.
  `ec2_describe-network-interfaces.json`). This is why `--from-cache tests/fixtures` works
  directly against the committed fixtures with no renaming — the acceptance-criteria smoke
  command. A missing file yields `{}` (empty resource) with a stderr warning, matching the
  "empty load balancers are normal" tolerance from Phase 1.
- **Deterministic JSON = no timestamp.** The definition of done stresses *deterministic*
  JSON; `Graph` already sorts nodes/edges, so `write_json` adds nothing time-varying.
  `test_write_json_is_deterministic` locks this in. (If a run timestamp is ever wanted,
  put it in `meta` at collection time, not in the writer.)
- **Verification default computed from resolved account ids.** `--verify-account` is a
  `BooleanOptionalAction` defaulting to `None`; when unset, verify runs iff **any** role
  resolved a non-`None` `account_id`. This matches "default on when the account id is
  known" and makes verify a silent no-op under `--profile` (which drops `account_id`, per
  Phase 1 §3) — no accidental live `sts` call when there's nothing to check against.
- **`verify_target` is called with `run_aws=runner.run_aws` explicitly.** Phase 1 bound
  `run_aws=runner.run_aws` as a *default argument* (captured at def time), so
  monkeypatching the module attribute wouldn't reach it. Passing it at call time makes the
  boundary swappable for `--from-cache` and tests. (Minor Phase-1 API gotcha — see §5.)
- **DOT clustering keys off the graph's own edges, not just attributes.** `_node_vpc`
  derives each node's VPC from `in_vpc`/`in_subnet` edges first (ENI → subnet → VPC),
  falling back to `attributes["vpc_id"]`. This means even synthetic subnets cluster
  correctly, and it needs no new data from Phase 2. Nodes with no resolvable VPC render at
  the top level rather than being dropped.
- **Node labels are id-first: `<aws-id> [<name>]`, or just `<aws-id>` when unnamed.**
  `_node_lines` surfaces the AWS id on every node and appends the `Name` tag in brackets
  when there is one (`node.label != node.id` ⇒ named). ENIs (no `Name` tag) and synthetic
  placeholders show the bare id. This keeps nodes self-identifying — a reader always sees
  the `i-…`/`subnet-…`/`vpc-…`/ARN, not just a friendly name. (Load-balancer ids are ARNs,
  so their identity line is long but unambiguous.) The `subgraph` header stays `VPC <name>`
  (name-only) since the standalone VPC node already carries the id-annotated label.
- **A VPC is its own top-level node, not swallowed by its cluster.** `_node_vpc` returns
  `None` for `vpc` nodes, so a VPC is drawn as a standalone node and each subnet's `in_vpc`
  edge visibly connects the subnet (inside the VPC's `cluster_*`) *up to* that VPC node.
  The cluster groups only the VPC's **contents** and is labeled `VPC <name>` for context —
  distinct from the VPC node's own `[vpc] / <name> / <cidr>` label, so the name isn't
  confusingly duplicated. (Initial cut nested the VPC node inside its own cluster, which
  read as a container box rather than a node subnets connect to; corrected after review.)
  The graph *model* was already correct all along — VPCs have always been real nodes with
  `in_vpc` edges (`docs/02_architecture.md §5.2`); this was purely a DOT-layout fix.
- **DOT determinism.** Clusters are emitted in `sorted` VPC-id order and members follow the
  graph's already-sorted node order, so `graph.dot` is byte-stable across runs like the JSON.
- **`--all-accounts` loops `resolve_target(account=alias)` per `[accounts.*]`.** It stays
  within the single-account-per-graph model (§10.4) — no merged cross-account graph, just N
  independent runs writing `graph.<alias>.*`. Built (not skipped) since it was cheap given
  the resolver already existed.

## 4. Deviations from the plan

- **`--include-orphans` added** (not in the Phase 3 flag list) because
  `learnings_phase2.md §6` explicitly asked Phase 3 to surface `build_graph`'s
  `include_orphans` keyword. Default off = the documented ENI-anchored view; on = also emit
  unreferenced subnets/VPCs/instances/LBs. Harmless and requested.
- **No `--roles` flag.** v1 is `network`-only per the docs; `_ROLES = ("network",)` is a
  module constant threaded through `resolve_target`/`collect_all`. Adding `flow_logs` later
  is a config binding + registry entry, not a CLI change — so a flag would be premature.
  Wiring already goes through `resolve_target`, satisfying the "no CLI change for
  `flow_logs`" requirement.
- **`pyproject.toml` unchanged** — the console script entry was already declared in Phase 1
  (documented there as an intentional early declaration). Nothing to add.
- Otherwise no deviations. `write_json`/`write_dot`/`render` signatures and the graph
  contract match `docs/02_architecture.md §7` and `docs/03_phase_plan.md` Phase 3.

## 5. Gotchas, surprises & AWS quirks

- **`config.verify_target`'s `run_aws` default is bound at import time.** `def
  verify_target(..., run_aws=runner.run_aws)` captures the *original* function; later
  `monkeypatch.setattr(runner, "run_aws", ...)` does **not** change that default. The CLI
  works around it by passing `run_aws=runner.run_aws` explicitly (resolves the current
  attribute at call time). Any future caller that wants an injectable boundary must do the
  same, or the default will silently shell out to real `aws`.
- **`dot` is genuinely optional and was absent in the build environment.** The graceful
  path (`render` → `None` → warn, still write `.dot`, exit 0) is the *default* experience
  here, not an edge case — `test_render_without_dot_degrades_gracefully` covers it. When
  `dot` *is* present, `dot -Tsvg graph.dot -o graph.svg` renders the committed sample.
- **LB node ids are ARNs** (contain `:` and `/`). DOT node/edge ids are double-quoted and
  escaped (`_esc`), and cluster ids are sanitized to `[A-Za-z0-9_]` (`_cluster_id`), so the
  ARNs don't break the DOT grammar. Verified in the fixture smoke output.
- **DOT label line breaks** are the two literal characters `\` + `n` in the source (a real
  newline would break the quoted string). `_label` joins escaped lines with `"\\n"`.
- **`--from-cache` truly makes zero live calls** — `test_from_cache_makes_no_live_calls`
  points `runner.run_aws` at a landmine and asserts the run still succeeds, proving the
  disk reader is used. This is the read-only guarantee at its strongest: from-cache can't
  touch AWS even if credentials exist.

## 6. Known gaps / TODO for later phases

- **`flow_logs` role (future Phase 4).** The CLI is already role-aware via `_ROLES` +
  `resolve_target`. Landing `flow_logs` means: add its `ROLE_COLLECTORS`/`ROLE_RESULT_KEYS`
  entries + collectors (Phase-1 seam), map its nodes/edges in `build_graph`, and either
  extend `_ROLES` or add a `--roles` flag. New node types (`flow_log`, `log_group`,
  `log_bucket`) will need color/shape entries in `dot_export._TYPE_STYLE` (they currently
  fall back to the white/box default) and possibly cluster rules for cross-account
  destinations. See `docs/05_roadmap.md`.
- **`--all-regions`** (`docs/02_architecture.md §8`) still not implemented — single region
  per run. A future flag would iterate `aws ec2 describe-regions` and tag nodes by region.
- **Richer output formats** — ideas: an interactive HTML/D3 view, GraphML/CSV export, or a
  Mermaid emitter for inline docs. All are additive next to `write_json`/`write_dot`.
- **Real-account ELB captures** still outstanding from Phase 2 (Classic-ELB and GWLB ENIs
  are unit-tested but unverified against a live capture). Phase 3 didn't change this.
- **`--render` picks one format at a time.** Rendering both PNG and SVG needs two runs; a
  future `--render png,svg` could accept a list.

## 7. How to verify this phase

```bash
pip install -e '.[dev]'          # or: pip install pytest ruff
python -m pytest -q              # 81 tests, fully offline via tests/fixtures/
python -m pytest tests/test_output.py tests/test_cli.py -q   # Phase 3 only (19 tests)
ruff check . && ruff format --check .
```

Produce a sample map with no AWS account (uses the committed fixtures):

```bash
cloudbreachgraph --from-cache tests/fixtures --output-dir out/
cat out/graph.json      # deterministic graph
cat out/graph.dot       # Graphviz DOT (VPC clusters, colored nodes, labeled edges)
dot -Tsvg out/graph.dot -o out/graph.svg   # only if Graphviz `dot` is installed
```

Against a live account (read-only; needs AWS CLI v2 + a configured profile):

```bash
cloudbreachgraph --account <alias> --output-dir out/ --render svg
```
