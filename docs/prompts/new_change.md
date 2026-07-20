# Generic Change Prompt — CloudBreachGraph

> Paste this whole file into a **fresh** Claude Code session on the CloudBreachGraph repo,
> then fill in the **CHANGE REQUEST** block at the top. Everything below it is standing context
> so the session makes changes that fit the existing codebase, tests, and docs.

---

## CHANGE REQUEST  ← fill this in

**What I want:**
<!-- Describe the change in plain language. Be concrete about the desired behavior/output.
     Examples:
       - "Add a --format csv option that also writes graph.csv (one row per edge)."
       - "ENIs attached to a NAT gateway should render with a distinct color in the DOT output."
       - "Add the `flow_logs` role: collect VPC Flow Log configs and their destinations."
       - "Fix: Classic ELB ENIs in eu-west-1 aren't being attributed to their load balancer." -->

**Acceptance criteria (how we'll know it's done):**
<!-- e.g. "cloudbreachgraph --from-cache tests/fixtures --format csv writes a valid graph.csv",
     or "a new test covers the NAT-gateway coloring", or "the eu-west-1 fixture now attributes correctly". -->

**Out of scope / constraints (optional):**
<!-- Anything you explicitly do NOT want touched, or hard requirements. -->

---

## Standing context (do not change — this describes the project)

You are making a change to **CloudBreachGraph**, a read-only Python CLI that maps an AWS
account's network topology (ENIs → EC2/LB → subnets → VPCs) using the **AWS CLI** (not boto3).
All three build phases are complete and merged; this is an incremental change to a working app.

### 1. Read these first (only what's relevant to the change)
- `README.md` — user-facing behavior, all CLI flags, outputs.
- `docs/02_architecture.md` — design of record. Key sections: §2 layout, §3 AWS commands,
  §4 fields, **§5 relationship-mapping rules**, §6 graph model, §7 output formats,
  §10–§11 accounts/roles/targets (incl. **§11.6 role registry**, §11.7 collection loop).
- `docs/04_conventions.md` — the rules you must follow (below in brief).
- `docs/05_roadmap.md` — the extensibility model; **read this if adding a new resource type/role**.
- `docs/learnings/learnings_phase{1,2,3}.md` — decisions, gotchas, and the real interface
  contracts each layer settled on. Read the one(s) covering the area you're touching.
- The actual source under `src/cloudbreachgraph/` — trust the code over the docs if they disagree,
  and note any doc drift you find.

### 2. Where things live (module map)
```
src/cloudbreachgraph/
├── cli.py               # argparse entrypoint: resolve target → collect → build → write
├── config.py            # account→profile + roles/targets loader & resolver (§10–§11)
├── aws/
│   ├── runner.py        # the ONLY place that shells out to `aws` (mock boundary for tests)
│   └── collectors.py    # collect_* functions + ROLE_COLLECTORS/ROLE_RESULT_KEYS registry (§11.6)
├── model/
│   ├── resources.py     # dataclasses: Eni, Ec2Instance, LoadBalancer, Subnet, Vpc
│   └── graph.py         # Node, Edge, Graph, Graph.to_dict()
├── mapping/builder.py   # build_graph(collected) -> Graph  (relationship rules, §5)
└── output/
    ├── json_export.py   # write_json(graph, path)
    └── dot_export.py    # write_dot(graph, path) + optional dot rendering
tests/                   # pytest, fully offline; fixtures in tests/fixtures/
```
Match this structure; if a change needs a new module, place it consistently and say so in your
summary.

### 3. Hard rules (from docs/04_conventions.md)
- **Python 3.11+, full type hints, `dataclasses` for models.**
- **Zero required third-party runtime dependency** — stdlib only (`subprocess`, `tomllib`, …).
  Any convenience lib (e.g. `graphviz`) stays an **optional** extra in `pyproject.toml`.
- **Read-only by construction:** only ever invoke AWS `describe-*` / `list-*` / `get-*` /
  `head-*` and the existing `sts get-caller-identity`. **Never** a mutating API. All `aws`
  calls go through `aws/runner.py`.
- **Deterministic output:** sort nodes/edges before serializing so JSON/DOT diffs stay stable;
  no timestamps or nondeterministic ordering in outputs.
- Keep modules small and single-purpose; follow existing naming and style. `ruff`-clean.

### 4. Common change patterns
- **New CLI flag / output tweak:** wire it in `cli.py`; update the writer in `output/`; update
  the `--from-cache` path so it works offline; add/adjust a test in `tests/test_cli.py` /
  `tests/test_output.py`; update `README.md` (the flags table + Outputs).
- **New/changed relationship or node/edge attribute:** edit `mapping/builder.py` (and
  `model/` if adding a field); keep §5 rules intact (e.g. an ENI is never attached to both an
  instance and an LB); add builder tests with a fixture that exercises the case.
- **New resource type = a new role** (e.g. `flow_logs`): follow `docs/05_roadmap.md` +
  §11.6 — add collectors in `aws/collectors.py`, register them in `ROLE_COLLECTORS` /
  `ROLE_RESULT_KEYS` (data, not new control flow), map them into the graph in
  `mapping/builder.py`, and let users bind the role in config. **No** config-grammar or CLI
  change should be needed.
- **Bug fix:** reproduce it first with a failing test (add a fixture if needed), then fix.

### 5. Testing & verification (required before you're done)
Tests are **fully offline** — they mock at the `runner` boundary and feed recorded JSON from
`tests/fixtures/`. Never hit the network in a test.
```bash
pip install -e '.[dev]'
pytest                     # must pass
ruff check . && ruff format --check .
# Exercise the real CLI end-to-end offline against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-out
# ...then inspect /tmp/cbg-out/graph.json and graph.dot to confirm the change.
```
Add tests for the behavior you changed. If you add or edit an AWS response shape, add a
matching fixture in `tests/fixtures/` (follow the `service_command[.variant].json` naming).

### 6. Docs to keep in sync
Update whatever your change makes stale — usually one or more of: `README.md` (flags/outputs),
`docs/02_architecture.md` (design/rules), `docs/05_roadmap.md` (if you advanced a future role),
and `docs/examples/cloudbreachgraph.example.toml` (if config changed). If you make a notable
design decision or hit an AWS quirk worth remembering, record it — either extend the relevant
`docs/learnings/` note or add a short `docs/learnings/learnings_<change-slug>.md` using the
template in `docs/04_conventions.md`.

### 7. Git
- Work on the branch you were told to use for this change (create it from the latest `main` if
  it doesn't exist). Commit in logical chunks with clear messages, then push.
- **Do not open a pull request unless explicitly asked.**

### 8. Definition of done
- [ ] Change implemented per the CHANGE REQUEST, matching existing structure and the hard rules.
- [ ] `pytest` passes offline; new/updated tests cover the change.
- [ ] `ruff check` + `ruff format --check` clean.
- [ ] Verified end-to-end via `--from-cache tests/fixtures` (or a live run if you have creds).
- [ ] Read-only guarantee still holds (no mutating AWS calls).
- [ ] Affected docs updated; notable decisions/quirks captured in `docs/learnings/`.
- [ ] Committed and pushed to the working branch.
