# Phase 1 Session Prompt — Foundation & AWS CLI Collection

> Paste everything below into a **fresh** Claude Code session opened on the CloudBreachGraph
> repo. Do not reuse a session that ran another phase.

---

You are implementing **Phase 1 of 3** of CloudBreachGraph, a Python CLI that uses the **AWS
CLI** (not boto3) to map an AWS account's network topology.

**Before writing any code, read these files in the repo — they are the source of truth:**
- `docs/README.md`
- `docs/01_overview.md`
- `docs/02_architecture.md`  (especially §3 AWS commands and §4 fields)
- `docs/03_phase_plan.md`    (your scope is the **Phase 1** section and its interface contract)
- `docs/04_conventions.md`   (coding rules + the mandatory learnings-file template)

There is no earlier phase, so there is no prior learnings file to read.

## Your scope (Phase 1 only — do not build models, graph, or CLI output)

Deliver the project scaffolding and the **AWS CLI data-collection layer**:

1. `pyproject.toml` (PEP 621), package at `src/cloudbreachgraph/`, a short `README.md` stub,
   and `__init__.py` / `__main__.py`.
2. `aws/runner.py` — a subprocess wrapper that runs
   `aws <args> --output json --no-cli-pager`, threads through `--region` and optional
   `--profile`, parses JSON stdout, and raises a clear error (surfacing stderr) on non-zero exit.
3. `config.py` — the **account→profile mapping** loader/resolver from `docs/02_architecture.md §10`.
   The operator keeps one AWS CLI profile per account and wants to say "for account X use
   profile Y". Implement `load_config(path)` (TOML via stdlib `tomllib`, with the documented
   discovery order) and `resolve_profile(cfg, account=..., profile_override=...)` applying the
   precedence (`--profile` override → `--account` alias/id lookup → CLI default), plus a helper
   that runs `aws sts get-caller-identity` to verify the resolved profile matches the expected
   account id. Do **not** build the CLI here (that's Phase 3) — just the loader/resolver API.
4. `aws/collectors.py` — the collector functions and `collect_all(...)` returning the exact
   dict shape defined in the Phase 1 **interface contract** in `docs/03_phase_plan.md`.
   Normalize each resource to preserve the fields from `docs/02_architecture.md §4`
   (flatten EC2 instances out of `Reservations`). Handle empty load-balancer results
   gracefully (accounts may have none). Optionally support a `--cache-dir` raw-JSON dump.
5. `tests/fixtures/` with a representative recorded JSON sample for **each** AWS command, and
   `pytest` tests that mock the subprocess boundary (the runner) and assert the collectors
   normalize correctly, **plus** tests for `config.py` (alias lookup, account-id lookup,
   `--profile` override precedence, missing-config fallback, unresolvable `--account` error,
   and parsing `docs/examples/cloudbreachgraph.example.toml`). Tests must run **offline**.

Stay strictly within scope. Do **not** implement `model/`, `mapping/`, or `output/` — those
are Phases 2 and 3. Leave the layout ready for them.

## Constraints
- Python 3.11+, full type hints, standard library only for runtime (no required 3rd-party dep).
- Read-only: collectors may only run AWS `describe-*` calls. Never a mutating API.
- Deterministic, testable, `ruff`-clean.

## Definition of done
- `pip install -e .` and `import cloudbreachgraph` both work.
- `pytest` passes offline using the fixtures.
- Code committed to the branch specified for this repo.

## REQUIRED final step — write `docs/learnings/learnings_phase1.md`
Before you finish, create **`docs/learnings/learnings_phase1.md`** using the template in
`docs/04_conventions.md`. It must capture everything Phase 2 needs, especially:
- The **exact normalized dict shape** each collector returns and the keys of `collect_all()`
  (this is the contract Phase 2 codes against).
- The **exact `config.py` signatures** (`load_config`, `resolve_profile`, the verify helper)
  and the config file schema you settled on — this is the contract Phase 3's CLI wires into.
- Any AWS CLI schema surprises, empty-result behavior, and pagination notes.
- Any deviation from `docs/02_architecture.md §2` layout.
- Exact commands to run the tests.

Commit `docs/learnings/learnings_phase1.md` together with the Phase 1 code, then push. Do not
open a pull request unless asked.
