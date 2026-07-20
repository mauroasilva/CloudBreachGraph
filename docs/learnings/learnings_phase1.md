# Learnings — Phase 1 (Foundation & AWS CLI Collection)

> Prior-phase learnings: none exist (Phase 1 is first). Contracts here are authored, not
> reconstructed.

## 1. What this phase delivered

- **Packaging** — `pyproject.toml` (PEP 621, setuptools, `src/` layout, zero required
  runtime deps), `README.md` stub, `src/cloudbreachgraph/__init__.py` (`__version__`),
  `__main__.py` (`python -m cloudbreachgraph`; prints a "CLI arrives in Phase 3" notice
  until `cli.py` exists), and a `py.typed` marker.
- **`aws/runner.py`** — `run_aws(args, *, profile=None, region=None, cache_dir=None)`:
  the single subprocess boundary. Builds `aws <args> --output json --no-cli-pager`,
  appends `--region`/`--profile` only when given, parses JSON stdout, raises
  `AwsCliError` (surfacing stderr) on non-zero exit or unparseable JSON. Optional raw-JSON
  cache via `configure_cache(path)` (module-level) or the per-call `cache_dir` arg.
- **`config.py`** — account/target loader + role-aware resolver + verify helper (details
  in §2).
- **`aws/collectors.py`** — six role-agnostic `collect_*` collectors, the
  `ROLE_COLLECTORS` / `ROLE_RESULT_KEYS` registry, and the `collect_all` driver.
- **`model/`, `mapping/`, `output/`** — created as empty packages (docstring-only
  `__init__.py`) so the layout is ready for Phases 2–3. **Do not** assume any code there.
- **Tests** — `tests/fixtures/*.json` (one recorded sample per command, plus an empty-ELB
  fixture and an `sts` fixture) and `tests/test_{runner,collectors,config}.py` (30 tests,
  fully offline, mocking at the `runner`/`subprocess` boundary).

## 2. Interface contract for the next phase

### 2a. `collect_all()` — the shape Phase 2 codes against

`collect_all(resolved, *, roles=("network",), cache_dir=None) -> dict` returns:

```python
{
  "meta": {"target": str | None, "region": str | None,
           "accounts": {"network": str | None}},   # role -> account_id (provenance)
  "network_interfaces":     [ <ENI dict>, ... ],
  "ec2_instances":          [ <instance dict>, ... ],
  "load_balancers_v2":      [ <elbv2 dict>, ... ],
  "load_balancers_classic": [ <classic-elb dict>, ... ],
  "subnets":                [ <subnet dict>, ... ],
  "vpcs":                   [ <vpc dict>, ... ],
}
```

The resource keys are exactly `ROLE_RESULT_KEYS["network"]`. `meta.region` is the region
of the first requested role (network in v1). `resolved` is a `config.ResolvedTarget`.

### 2b. Exact normalized dict shapes (keys are the original AWS names)

Normalization keeps the AWS key names and nested structure for the fields Phase 2 needs,
so mapping reads e.g. `eni["Attachment"]["InstanceId"]` directly. Every field uses
`.get(...)` with a default, so missing keys are `None`/`[]`, never a `KeyError`.

**ENI** (`network_interfaces[]`):
```python
{"NetworkInterfaceId", "SubnetId", "VpcId", "InterfaceType",
 "Description" (str, "" if absent), "Status", "AvailabilityZone",
 "RequesterId", "RequesterManaged",
 "Attachment": {"AttachmentId","InstanceId","InstanceOwnerId","DeviceIndex","Status"},
 "PrivateIpAddresses": [...], "Groups": [...]}
```
`Attachment` is **always a dict** (its inner values are `None` when the ENI is detached /
service-managed) — Phase 2 can test `eni["Attachment"]["InstanceId"]` without a None-check
on `Attachment` itself. This is the key signal for §5.3 (instance attach) vs §5.4 (LB).

**EC2 instance** (`ec2_instances[]`) — flattened out of `Reservations[].Instances[]`:
```python
{"InstanceId", "State": {"Name": str|None}, "InstanceType",
 "VpcId", "SubnetId", "Tags": [{"Key","Value"}, ...]}
```
`State` is kept as a dict `{"Name": ...}` (mirrors §4's `State.Name`). Name tag is **not**
pre-extracted — Phase 2 derives the label from `Tags`.

**ELBv2 LB** (`load_balancers_v2[]`):
```python
{"LoadBalancerArn", "LoadBalancerName", "Type", "Scheme", "VpcId",
 "DNSName", "State": {...}}
```
`LoadBalancerArn` ends with `:loadbalancer/app/<name>/<id>` (or `net/`, `gwy/`) — the
suffix Phase 2 matches ENI `Description` tokens against (§5.4 rule 1).

**Classic ELB** (`load_balancers_classic[]`):
```python
{"LoadBalancerName", "VPCId", "DNSName", "Scheme",
 "Subnets": [...], "SecurityGroups": [...]}
```
⚠️ Classic ELB spells the VPC key **`VPCId`** (capital P-C), unlike every other resource's
`VpcId`. Preserved verbatim — do not "fix" it in Phase 2.

**Subnet** (`subnets[]`): `{"SubnetId","VpcId","CidrBlock","AvailabilityZone","Tags"}`
**VPC** (`vpcs[]`): `{"VpcId","CidrBlock","IsDefault","Tags"}`

### 2c. The role registry (how `flow_logs` etc. plug in)

In `aws/collectors.py`:
```python
Collector = Callable[[str | None, str | None], list[dict]]  # collect_x(profile, region)
ROLE_COLLECTORS: dict[str, list[Collector]]   # "network" -> [6 collectors]
ROLE_RESULT_KEYS: dict[str, list[str]]        # "network" -> [6 bundle keys], parallel
```
`ROLE_COLLECTORS[role][i]`'s result is stored under `ROLE_RESULT_KEYS[role][i]`. Adding a
role = add one entry to each dict + write its `collect_x(profile, region)` collectors.
`collect_all`'s loop, the config grammar, and the resolver do **not** change. Each
collector takes only `(profile, region)` and knows nothing about roles/accounts.

### 2d. `config.py` signatures (the contract Phase 3's CLI wires into)

```python
load_config(path: str | None) -> AccountConfig
resolve_target(cfg, *, target=None, account=None, profile_override=None,
               region=None, roles=("network",)) -> ResolvedTarget
resolve_profile(cfg, *, account=None, profile_override=None, region=None) -> ResolvedAccount
verify_target(resolved, *, enabled=True, run_aws=runner.run_aws) -> dict[str|None, str|None]
```
Dataclasses:
```python
Account(alias, account_id, profile, region)
Target(name, default_account, roles: dict[role->account_alias])
AccountConfig(accounts, targets, default_target, default_account, path)  # .is_empty
ResolvedAccount(profile: str|None, account_id: str|None, region: str|None)
ResolvedTarget(target: str|None, roles: dict[role -> ResolvedAccount])   # .network shortcut
```
- `profile is None` ⇒ "use the AWS CLI default" (pass no `--profile`).
- `account_id is None` ⇒ expected account unknown ⇒ verification skipped for that role.
- `resolve_profile` is the thin `network`-role wrapper over `resolve_target`.
- `verify_target` runs `sts get-caller-identity` **once per distinct profile** and raises
  `ConfigError` on any mismatch vs an expected `account_id`. `run_aws` is injectable for
  offline tests.
- All resolution/verification errors raise `config.ConfigError`; runner errors raise
  `runner.AwsCliError`.

### 2e. Config schema settled on

```toml
default_target = "prod"      # optional top-level
default_account = "..."      # optional top-level (used if no default_target)

[accounts.<alias>]
account_id = "111111111111"  # optional but needed for verification
profile    = "prod-audit"    # optional; omit -> CLI default
region     = "us-east-1"     # optional per-account default

[targets.<name>]
default_account = "<alias>"        # role fallback
[targets.<name>.roles]
flow_logs = "<alias>"              # per-role override (future roles; safe to set now)
```
Precedence per role: `--profile` override → `--target`/`--account` binding (or the
target's `default_account`) → config `default_target`/`default_account` → CLI default.

## 3. Decisions & rationale

- **Normalization = pick-a-subset, keep AWS names & nesting.** Rather than renaming to a
  house schema, collectors keep the original AWS keys for the fields §4 lists. This makes
  Phase 2 mapping a direct read of the documented field names and keeps the diff between
  "raw AWS JSON" and "normalized" reviewable. Fields outside §4 are dropped to keep the
  bundle small and deterministic.
- **`Attachment` always a dict.** AWS omits `Attachment` entirely for detached ENIs; we
  emit `{"InstanceId": None, ...}` so Phase 2's §5.3 check is a single, safe lookup.
- **Role knowledge lives only in the driver.** Collectors are `(profile, region)` pure
  functions; `collect_all` is the one place that maps role→account→profile. This is the
  §11 seam and keeps future roles to pure-data registry edits.
- **Resolver independent of collectors.** `config.py` imports `runner` (for verify) but
  **not** `collectors`; it has its own `DEFAULT_ROLES = ("network",)`. Avoids a config↔
  collectors import cycle and lets `resolve_target(roles=...)` resolve any role name.
- **`--profile` override drops `account_id`.** The escape hatch skips the mapping, so we
  have no trustworthy expected account → `account_id=None` → verification is skipped for
  it. Documented; Phase 3 should surface that verify is a no-op under `--profile`.
- **Cache via module-global + per-call arg.** The fixed `collect_x(profile, region)`
  signature has no room for a cache path, so `collect_all` calls `runner.configure_cache`
  and `run_aws` also accepts a per-call `cache_dir`. Phase 3 wires `--cache-dir` to either.

## 4. Deviations from the plan

- **`__main__.py` is a stub**, not a working CLI — the CLI is explicitly Phase 3. It
  imports `cli.py` lazily and prints a friendly notice if absent, so `python -m
  cloudbreachgraph` never crashes with an ImportError.
- **`pyproject.toml` declares the `cloudbreachgraph = cloudbreachgraph.cli:main` console
  script now** though `cli.py` lands in Phase 3. Entry points are resolved at invocation,
  not install, so `pip install -e .` succeeds today; Phase 3 just adds the module.
- **`collect_all` signature** follows `§11.7` (`collect_all(resolved, *, roles=...)`), not
  the looser `collect_all(region=..., profile=...)` mentioned in the Phase 3 contract
  prose. Phase 3 should build a `ResolvedTarget` (via `resolve_target`) and pass it.
- Layout otherwise matches `02_architecture.md §2`. `model/mapping/output` exist as empty
  packages only.

## 5. Gotchas, surprises & AWS quirks

- **Classic ELB `VPCId`** (capital P-C) vs everything else's `VpcId` — preserved as-is.
- **ELBv2 owns no `Reservations`; only `describe-instances` nests** results in
  `Reservations[].Instances[]` — that's the only collector that flattens.
- **Empty load balancers are normal, not an error.** `elb`/`elbv2` return `{"...": []}` in
  accounts with none; collectors `.get(key, [])` so an empty list flows through. (A missing
  `elb` service or a permissions error would still raise `AwsCliError` from the runner —
  that is intended; only *empty results* are tolerated, not failures.)
- **ELB ENI description formats** (verify in Phase 2 against a real capture): ALB
  `ELB app/<name>/<id>`, NLB `ELB net/<name>/<id>`, GWLB `ELB gwy/<name>/<id>`, Classic
  `ELB <name>`. The fixtures encode ALB (`InterfaceType: interface`) and NLB
  (`InterfaceType: network_load_balancer`) cases, plus an instance ENI and a
  `nat_gateway` ENI (no attachment → should map to subnet/VPC only).
- **Pagination:** the AWS CLI auto-paginates with `--output json`, returning the full set
  in one JSON document, so collectors do no manual paging. `--no-cli-pager` only disables
  the interactive *output* pager (which would otherwise block a subprocess); it does not
  affect API pagination. Accounts may still be large — no result-set size is assumed.
- **`sts get-caller-identity` returns `.Account` as a string** (`"111111111111"`); compare
  as strings (config `account_id` is a string too).

## 6. Known gaps / TODO for later phases

- Phase 2: build `model/` + `mapping/` from the shapes in §2b; implement §5 rules;
  re-verify the ELB description formats against a real capture and record quirks.
- Phase 3: build `cli.py`, wire flags → `load_config`/`resolve_target`/`verify_target`/
  `collect_all`; implement `--cache-dir`, `--from-cache`, `--verify-account/--no-...`,
  `--all-accounts` (stretch). Note verify is a no-op under `--profile` (no expected id).
- `flow_logs` role (future phase): add `ROLE_COLLECTORS["flow_logs"]` +
  `ROLE_RESULT_KEYS["flow_logs"]` and its `collect_x(profile, region)` collectors — no
  changes needed to `collect_all`, the resolver, or the config grammar.
- `--all-regions` (`02_architecture.md §8`) not implemented; single region per run.

## 7. How to verify this phase

```bash
pip install -e '.[dev]'          # or: pip install pytest ruff
python -c "import cloudbreachgraph; print(cloudbreachgraph.__version__)"
python -m pytest -q              # 30 tests, fully offline via tests/fixtures/
ruff check . && ruff format --check .
```
