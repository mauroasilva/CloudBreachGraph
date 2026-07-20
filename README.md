# CloudBreachGraph

A read-only command-line tool that maps an AWS account's network topology — Network
Interfaces (ENIs) → EC2 instances / load balancers → subnets → VPCs — using the **AWS
CLI** (not boto3) as its data source. Output is a graph you can serialize to JSON and
render with Graphviz.

> **Status:** Phase 1 (foundation + AWS CLI collection layer). The domain model, graph
> construction, and the end-to-end CLI arrive in Phases 2 and 3. See `docs/` for the full
> build plan.

## Install

```bash
pip install -e .
```

No required third-party runtime dependencies (stdlib `subprocess` + `tomllib`). The AWS
CLI v2 must be installed and on `PATH`, with a read-only profile per account.

## What Phase 1 provides

- `cloudbreachgraph.aws.runner.run_aws(...)` — subprocess wrapper around
  `aws <args> --output json --no-cli-pager`, threading through `--region`/`--profile` and
  surfacing stderr on failure.
- `cloudbreachgraph.aws.collectors` — role-agnostic `collect_*` functions, the
  `ROLE_COLLECTORS` / `ROLE_RESULT_KEYS` registry, and the `collect_all(resolved)` driver.
- `cloudbreachgraph.config` — the account→profile + role/target loader/resolver
  (`load_config`, `resolve_target`, `resolve_profile`, `verify_target`).

## Configuration

CloudBreachGraph maps each AWS account to one named AWS CLI profile. Copy
`docs/examples/cloudbreachgraph.example.toml` to `./cloudbreachgraph.toml` (or
`~/.config/cloudbreachgraph/config.toml`) and edit it. See `docs/02_architecture.md`
§10–§11 for the precedence rules and multi-account targets.

## Development

```bash
pip install -e '.[dev]'
pytest        # runs fully offline against tests/fixtures/
ruff check .
```
