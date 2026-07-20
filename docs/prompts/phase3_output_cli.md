# Phase 3 Session Prompt — Output, Visualization & End-to-End CLI

> Paste everything below into a **fresh** Claude Code session opened on the CloudBreachGraph
> repo. Do not reuse the Phase 1 or Phase 2 session.

---

You are implementing the final phase, **Phase 3 of 3**, of CloudBreachGraph, a Python CLI
that uses the **AWS CLI** (not boto3) to map an AWS account's network topology. Phases 1
(collection) and 2 (models + graph + mapping) are already committed.

**Before writing any code, read these — they are the source of truth:**
- `docs/README.md`
- `docs/01_overview.md`
- `docs/02_architecture.md`  (**§7 output formats**, §6 graph model)
- `docs/03_phase_plan.md`    (your scope is the **Phase 3** section)
- `docs/04_conventions.md`   (coding rules + the mandatory learnings-file template)
- **`docs/learnings/learnings_phase1.md`** and **`docs/learnings/learnings_phase2.md`** — the
  collector contract and the `Graph.to_dict()` structure/attribute keys you render against. If
  either is missing or thin, reconstruct from the committed code in `src/cloudbreachgraph/` and
  note the gap at the top of your own learnings file.

## Your scope (Phase 3 — make it usable end to end)

1. `output/json_export.py` — `write_json(graph, path)` writing pretty, deterministic JSON
   from `Graph.to_dict()`.
2. `output/dot_export.py` — `write_dot(graph, path)` producing Graphviz DOT: nodes colored by
   type (`eni`, `ec2_instance`, `load_balancer`, `subnet`, `vpc`), edges labeled by
   relationship (show `match_rule` on load-balancer edges when useful), and — following the
   requested build order — group subnets/ENIs inside their VPC using `subgraph cluster_*`.
   Add an optional `render(dot_path, fmt)` that shells out to `dot -T<fmt>` **only if** the
   `dot` binary is on PATH; degrade gracefully (still write the `.dot`, warn on render) when
   it is absent.
3. `cli.py` — an `argparse` entrypoint wiring the Phase 1 `config.resolve_profile` →
   `collect_all` → `build_graph` → writers. This is where the operator's **"for account X use
   profile Y"** requirement is surfaced. Flags:
   - `--account <alias|id>` + `--config <path>` — select an account from the account→profile
     mapping (`docs/02_architecture.md §10`); the resolved profile is passed to every AWS call.
   - `--profile <name>` — direct override that bypasses the mapping.
   - `--verify-account / --no-verify-account` — toggle the `sts get-caller-identity` check
     (default on when the account id is known).
   - `--region`, `--cache-dir`, `--output-dir`, `--render {png,svg}`, `--from-cache <dir>`
     (build from previously cached JSON with **no** live AWS calls).
   - optional `--all-accounts` — loop over every configured account, writing one graph each
     (`graph.<alias>.json` / `.dot`); §10.4 stretch goal, skip if time-boxed and note in learnings.
   Use the config `load_config`/`resolve_profile` API exactly as Phase 1 documented it in
   `docs/learnings/learnings_phase1.md`. Register the console script
   `cloudbreachgraph = cloudbreachgraph.cli:main` in `pyproject.toml`.
4. End-to-end test: fixtures/cached JSON → CLI with `--from-cache` → assert `graph.json` and
   `graph.dot` are produced and well-formed. Add a test that `--account <alias>` resolves the
   mapped profile (mock the AWS boundary) and that `--profile` overrides it. Tests run offline.
5. Update the user-facing `README.md` with real usage examples: the account→profile config
   file, `--account <alias>`, `--profile` override, a live run, and `--from-cache`.

## Definition of done
- `cloudbreachgraph --from-cache <fixtures> --output-dir out/` produces `graph.json` +
  `graph.dot` offline.
- `cloudbreachgraph --account <alias>` resolves the mapped profile; `--profile` overrides it.
- Missing `dot` binary degrades gracefully.
- Read-only guarantee still holds across the whole tool (no mutating AWS calls anywhere).
- `pytest` passes offline.

## Constraints
- Python 3.11+, full type hints, standard library only for runtime (DOT is emitted as text;
  any `graphviz` package must remain an optional extra).

## REQUIRED final step — write `docs/learnings/learnings_phase3.md`
Before finishing, create **`docs/learnings/learnings_phase3.md`** using the
`docs/04_conventions.md` template. Capture especially:
- The final CLI surface (all flags) and example invocations.
- The output file formats/locations and any rendering caveats.
- Any deviation from the docs and why.
- Follow-up ideas (e.g. `--all-regions`, extra resource types, HTML output).
- Exact commands to run the tests and to produce a sample map.

Commit `docs/learnings/learnings_phase3.md` with the Phase 3 code, then push. No pull request
unless asked.
