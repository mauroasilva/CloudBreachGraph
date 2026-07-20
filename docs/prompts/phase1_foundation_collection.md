# Phase 1 Session Prompt — Foundation & AWS CLI Collection

> Paste everything below into a **fresh** Claude Code session opened on the CloudBreachGraph
> repo. Do not reuse a session that ran another phase.

---

You are implementing **Phase 1 of 3** of CloudBreachGraph, a Python CLI that uses the **AWS
CLI** (not boto3) to map an AWS account's network topology.

**Before writing any code, read these files in the repo — they are the source of truth:**
- `docs/README.md`
- `docs/01_overview.md`
- `docs/02_architecture.md`  (§3 AWS commands, §4 fields, and **§10–§11** accounts/roles/targets)
- `docs/03_phase_plan.md`    (your scope is the **Phase 1** section and its interface contract)
- `docs/04_conventions.md`   (coding rules + the mandatory learnings-file template)
- `docs/05_roadmap.md`       (so the role-aware config you build cleanly admits future roles)

There is no earlier phase, so there is no prior learnings file to read.

## Your scope (Phase 1 only — do not build models, graph, or CLI output)

Deliver the project scaffolding and the **AWS CLI data-collection layer**:

1. `pyproject.toml` (PEP 621), package at `src/cloudbreachgraph/`, a short `README.md` stub,
   and `__init__.py` / `__main__.py`.
2. `aws/runner.py` — a subprocess wrapper that runs
   `aws <args> --output json --no-cli-pager`, threads through `--region` and optional
   `--profile`, parses JSON stdout, and raises a clear error (surfacing stderr) on non-zero exit.
3. `config.py` — the **account→profile + role/target** loader/resolver from
   `docs/02_architecture.md §10–§11`. The operator keeps one AWS CLI profile per account and
   wants to say "for account X use profile Y" — and, importantly, some resources for one
   environment live in **different** accounts (e.g. VPC flow logs in a central logging account,
   separate from the VPCs). So resolution must be **role-aware**:
   - `load_config(path)` (TOML via stdlib `tomllib`, documented discovery order) parsing both
     `[accounts.*]` and `[targets.*]` (a target binds resource roles → accounts).
   - `resolve_target(cfg, target=..., account=..., profile_override=...) -> ResolvedTarget`
     whose `.roles` maps each role → `{profile, account_id, region}`; keep
     `resolve_profile(...)` as a thin wrapper for the single-account/`network`-role case.
   - precedence `--profile` override → `--target`/`--account` binding / `default` → CLI default.
   - a verify helper running `aws sts get-caller-identity` **once per distinct resolved account**.
   **v1 only activates the `network` role**, but build the resolver/registry so `flow_logs` and
   other roles can be bound in config with **no** grammar change (see `docs/05_roadmap.md`).
   Do **not** build the CLI here (that's Phase 3) — just the loader/resolver API.
4. `aws/collectors.py` — the collector functions, the **explicit role registry**, and the
   collection driver:
   - Each collector `collect_x(profile, region) -> list[dict]` runs one `aws` command via
     `runner.py` and normalizes it, preserving the fields from `docs/02_architecture.md §4`
     (flatten EC2 instances out of `Reservations`). Collectors stay **role-agnostic** — they know
     nothing about targets/accounts. Handle empty load-balancer results gracefully.
   - `ROLE_COLLECTORS` / `ROLE_RESULT_KEYS` — the explicit registry from `docs/02_architecture.md
     §11.6` binding role `network` to its six collectors and result keys. This is the seam future
     roles extend; make adding a role a matter of adding a registry entry, not editing the loop.
   - `collect_all(resolved, roles=["network"]) -> dict` — the driver loop from `§11.7`: for each
     role, run its registry collectors with that role's resolved profile/region and bundle results
     under the fixed keys, recording per-role account provenance in `meta`. Return the exact shape
     in the Phase 1 **interface contract** in `docs/03_phase_plan.md`.
   - Optionally support a `--cache-dir` raw-JSON dump.
5. `tests/fixtures/` with a representative recorded JSON sample for **each** AWS command, and
   `pytest` tests that mock the subprocess boundary (the runner) and assert the collectors
   normalize correctly, **plus** tests for `config.py` (alias lookup, account-id lookup,
   `--profile` override precedence, missing-config fallback, unresolvable `--account`/`--target`
   error, a multi-account target resolving `network` vs `flow_logs` roles to different
   accounts/profiles, and parsing `docs/examples/cloudbreachgraph.example.toml`). Tests must run
   **offline**.

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
- The **role registry** (`ROLE_COLLECTORS`/`ROLE_RESULT_KEYS`) and the `collector(profile, region)`
  signature — so the future `flow_logs` role knows exactly how to register itself.
- The **exact `config.py` signatures** (`load_config`, `resolve_target`/`resolve_profile`, the
  verify helper), the `ResolvedTarget`/`ResolvedAccount` shapes, and the config schema
  (`accounts` + `targets.<name>.roles`) you settled on — the contract Phase 3's CLI wires into,
  and the base the future `flow_logs` role builds on.
- Any AWS CLI schema surprises, empty-result behavior, and pagination notes.
- Any deviation from `docs/02_architecture.md §2` layout.
- Exact commands to run the tests.

Commit `docs/learnings/learnings_phase1.md` together with the Phase 1 code, then push. Do not
open a pull request unless asked.
