# 02 — Architecture

This is the technical reference every phase relies on. The **relationship-mapping rules**
in section 5 are the core of the application — read them carefully.

## 1. Technology choices

- **Language:** Python 3.11+ (use standard library where possible).
- **Data source:** AWS CLI v2, invoked via `subprocess` with `--output json`. **Not boto3.**
- **Packaging:** `pyproject.toml` (PEP 621), console entry point `cloudbreachgraph`.
- **Runtime dependencies:** aim for **zero required** third-party packages. Graphviz DOT is
  emitted as plain text (no library needed). The `dot` binary is optional and only used to
  rasterize. If a phase wants the `graphviz` Python package for convenience, it must be an
  **optional** extra, never a hard dependency.
- **Testing:** `pytest`. AWS CLI calls are mocked with recorded JSON fixtures so tests run
  offline. `pytest` is a dev dependency only.
- **Style:** type hints everywhere, `dataclasses` for models, `ruff`-clean formatting.
- **Config file format:** TOML, parsed with the stdlib `tomllib` (Python 3.11+, read-only) so
  the account→profile mapping needs **no** third-party dependency. Do not use YAML (that would
  pull in PyYAML). JSON may be accepted as a secondary format since it's also stdlib.

## 2. Suggested project layout

```
CloudBreachGraph/
├── pyproject.toml
├── README.md                     # user-facing, short (docs/ holds the build plan)
├── src/
│   └── cloudbreachgraph/
│       ├── __init__.py
│       ├── __main__.py           # enables `python -m cloudbreachgraph`
│       ├── cli.py                # argparse entrypoint  (Phase 3)
│       ├── config.py             # account -> profile mapping loader/resolver  (Phase 1)
│       ├── aws/
│       │   ├── __init__.py
│       │   ├── runner.py         # subprocess wrapper around `aws ...`  (Phase 1)
│       │   └── collectors.py     # describe_* functions -> normalized dicts  (Phase 1)
│       ├── model/
│       │   ├── __init__.py
│       │   ├── resources.py      # dataclasses: Eni, Ec2Instance, LoadBalancer, Subnet, Vpc  (Phase 2)
│       │   └── graph.py          # Node, Edge, Graph  (Phase 2)
│       ├── mapping/
│       │   ├── __init__.py
│       │   └── builder.py        # build_graph(collected) -> Graph, relationship rules  (Phase 2)
│       └── output/
│           ├── __init__.py
│           ├── json_export.py    # Graph -> JSON  (Phase 3)
│           └── dot_export.py     # Graph -> Graphviz DOT  (Phase 3)
├── tests/
│   ├── fixtures/                 # recorded AWS CLI JSON responses
│   └── test_*.py
└── docs/                         # this plan (already present)
```

Phases may adjust this layout, but **if they do, they must record the final layout in their
`learnings_phaseX.md`** so the next session isn't surprised.

## 3. AWS CLI commands used

All commands run with `--output json`. Region and profile are threaded through from the CLI
flags. The AWS CLI auto-paginates by default, returning the full result set.

| Resource | Command | Key output path |
|----------|---------|-----------------|
| Network Interfaces | `aws ec2 describe-network-interfaces --region <r>` | `.NetworkInterfaces[]` |
| EC2 Instances | `aws ec2 describe-instances --region <r>` | `.Reservations[].Instances[]` |
| Load Balancers (v2: ALB/NLB) | `aws elbv2 describe-load-balancers --region <r>` | `.LoadBalancers[]` |
| Load Balancers (v1: Classic) | `aws elb describe-load-balancers --region <r>` | `.LoadBalancerDescriptions[]` |
| Subnets | `aws ec2 describe-subnets --region <r>` | `.Subnets[]` |
| VPCs | `aws ec2 describe-vpcs --region <r>` | `.Vpcs[]` |
| Caller identity (account check) | `aws sts get-caller-identity` | `.Account`, `.Arn` |

Notes for the collection layer (Phase 1):

- Add `--no-cli-pager` to avoid the interactive pager blocking a subprocess.
- Respect an optional `--profile <name>` by passing it through to **every** `aws` call. The
  profile is resolved from the account→profile mapping (see §10) or from an explicit override.
- Treat a non-zero exit code as a hard error with the captured stderr surfaced to the user
  (common causes: expired creds, missing permissions, wrong region).
- `elb`/`elbv2` may be absent or return empty in accounts with no load balancers — handle
  the empty case gracefully; do not treat "no load balancers" as an error.
- Consider a `--cache-dir` option that writes each raw JSON response to disk, so Phase 2/3
  and tests can replay real captures. Optional but recommended.

## 4. Fields we depend on (record any schema surprises in learnings)

**Network Interface** (`.NetworkInterfaces[]`):
- `NetworkInterfaceId` — node id, e.g. `eni-0abc...`
- `SubnetId` — always present → subnet edge
- `VpcId` — present (redundant with subnet's VPC, but useful as a cross-check)
- `InterfaceType` — e.g. `interface`, `network_load_balancer`, `nat_gateway`, `vpc_endpoint`, `lambda`, `gateway_load_balancer`
- `Attachment.InstanceId` — present when attached to an EC2 instance
- `Attachment.InstanceOwnerId` — for service-managed ENIs this is an AWS service principal (e.g. `amazon-elb`, `amazon-aws`)
- `Description` — free-text; **critical** for load balancer attribution (see §5)
- `RequesterId`, `RequesterManaged` — service-managed ENIs (ELB, NAT, RDS, etc.)
- `PrivateIpAddresses[]`, `Groups[]` (security groups) — useful node metadata

**EC2 Instance** (`.Reservations[].Instances[]`):
- `InstanceId`, `State.Name`, `InstanceType`, `Tags[]` (Name), `VpcId`, `SubnetId`

**ELBv2 Load Balancer** (`.LoadBalancers[]`):
- `LoadBalancerArn` — contains `:loadbalancer/app/<name>/<id>` (ALB) or `.../net/<name>/<id>` (NLB)
- `LoadBalancerName`, `Type` (`application` | `network` | `gateway`), `VpcId`, `DNSName`

**Classic ELB** (`.LoadBalancerDescriptions[]`):
- `LoadBalancerName`, `VPCId`, `DNSName`, `Subnets[]`

**Subnet** (`.Subnets[]`): `SubnetId`, `VpcId`, `CidrBlock`, `AvailabilityZone`, `Tags[]`

**VPC** (`.Vpcs[]`): `VpcId`, `CidrBlock`, `IsDefault`, `Tags[]`

## 5. Relationship-mapping rules (THE CORE — Phase 2)

For each ENI, resolve **at most one** compute/LB attachment, plus its subnet and VPC.

### 5.1 ENI → Subnet  (always)
Edge `in_subnet` from ENI node to the subnet named by `NetworkInterface.SubnetId`.
Every ENI has a `SubnetId`. If the subnet isn't in the collected set, still create the edge
and mark the subnet node as `synthetic` / `unresolved` (metadata flag) — don't drop it.

### 5.2 Subnet → VPC  (always)
Edge `in_vpc` from subnet node to `Subnet.VpcId`. Same synthetic-node rule if a VPC is
missing from the collected set.

### 5.3 ENI → EC2 Instance
If `Attachment.InstanceId` is present **and** non-empty → edge `attached_to` from ENI to
that EC2 instance node. This is the unambiguous, preferred signal. When it's present, the
ENI is instance-attached and you do **not** also attribute it to a load balancer.

### 5.4 ENI → Load Balancer  (the tricky one)
Service-managed ENIs (no `Attachment.InstanceId`) may belong to a load balancer. Resolve in
this priority order and record which rule fired in edge metadata (`match_rule`):

1. **ELBv2 (ALB/NLB/GWLB) via Description prefix.** ELBv2-owned ENIs have a `Description`
   shaped like:
   - ALB: `ELB app/<lb-name>/<lb-id>`
   - NLB: `ELB net/<lb-name>/<lb-id>`
   - GWLB: `ELB gwy/<lb-name>/<lb-id>`
   Extract the `app/<name>/<id>` (or `net/`, `gwy/`) token after `ELB `. Match it against the
   suffix of each ELBv2 `LoadBalancerArn` (the ARN ends with `:loadbalancer/app/<name>/<id>`).
   On match → edge `attached_to` (ENI → that load balancer), `match_rule = "elbv2_description"`.

2. **Classic ELB via Description.** Classic-ELB ENIs have `Description = "ELB <lb-name>"`
   (no `app/`/`net/` segment). Match `<lb-name>` against Classic `LoadBalancerName`.
   On match → edge `attached_to`, `match_rule = "classic_elb_description"`.

3. **InterfaceType fallback.** If `InterfaceType == "network_load_balancer"` or
   `"gateway_load_balancer"` but the description didn't resolve to a known LB, still create
   the LB-type attachment to an `unresolved` load balancer node keyed by the parsed name,
   and flag it. Record `match_rule = "interface_type_fallback"`.

If none of these fire, the ENI has **no** compute/LB attachment (e.g. NAT gateway, VPC
endpoint, RDS, Lambda ENI). That's expected — leave it attached only to its subnet/VPC and
tag the ENI node with its `InterfaceType` so the map still explains what it is. Do **not**
invent an attachment.

> **Edge-case guidance to capture in `learnings_phase2.md`:** the `ELB ` description format
> is the documented, stable way to attribute ELB ENIs; verify against a real capture and note
> any account where it didn't hold. Never attribute an ENI to both an instance and an LB.

## 6. Graph data model (Phase 2 defines, Phase 3 consumes)

A minimal, serialization-friendly model:

```
Node:
  id:    str            # eni-..., i-..., subnet-..., vpc-..., or LB arn/name
  type:  str            # "eni" | "ec2_instance" | "load_balancer" | "subnet" | "vpc"
  label: str            # human-friendly (Name tag or id)
  attributes: dict      # type-specific metadata (state, cidr, interface_type, synthetic, ...)

Edge:
  source: str           # node id
  target: str           # node id
  relationship: str     # "attached_to" | "in_subnet" | "in_vpc"
  attributes: dict      # e.g. {"match_rule": "elbv2_description"}

Graph:
  nodes: list[Node]     # unique by id
  edges: list[Edge]
  meta:  dict           # account id, region(s), generated_at, tool version
```

Requirements:
- Node ids are unique; adding a node that already exists merges attributes rather than
  duplicating.
- The graph must be deterministic (stable ordering) so JSON/DOT diffs are meaningful and
  tests are stable — sort nodes and edges before export.
- `Graph.to_dict()` returns a plain JSON-serializable structure; this is the **contract**
  Phase 3 depends on.

## 7. Output formats (Phase 3)

- **JSON** (`graph.json`): `Graph.to_dict()`, pretty-printed, stable ordering.
- **Graphviz DOT** (`graph.dot`): nodes grouped/colored by type; edge labels show the
  relationship (and `match_rule` for LB edges when useful). Consider `subgraph cluster_*`
  per VPC so the layout groups subnets/ENIs inside their VPC visually.
- **Optional render:** if the `dot` binary is on PATH, offer `--render png|svg` that shells
  out to `dot -T<fmt>`. Absence of `dot` must degrade gracefully (still write the `.dot`).

## 8. Regions

- Default: the single region from CLI config or `--region`.
- Stretch (only if cheap): `--all-regions` iterates `aws ec2 describe-regions` and tags each
  node with its region. If not implemented in v1, note it as future work in learnings.

## 9. Error handling & safety

- Read-only: the app must never call a mutating AWS API. Collectors only run `describe-*` and
  the read-only `sts get-caller-identity`.
- Fail loudly on auth/permission errors with the AWS CLI's stderr shown to the user.
- Partial data: if one collector fails but others succeed, prefer building a partial graph
  and clearly flagging what's missing over aborting — but make that behavior explicit and
  documented in learnings.

## 10. Account → profile mapping (how to target an account)

The operator keeps **one named AWS CLI profile per account**. CloudBreachGraph must let them
say "for account X, use profile Y" so they select an account without memorizing which profile
maps to it. There are two inputs, resolved in this precedence order (first match wins):

1. **`--profile <name>` (explicit CLI override).** Skips the mapping entirely and uses that
   profile directly. Always available as an escape hatch.
2. **`--account <id-or-alias>` resolved against the config file.** Looks up the account in the
   mapping and uses its `profile`.
3. **Neither given:** fall back to the AWS CLI's own default profile/credentials (no
   `--profile` flag passed), so the tool still works for someone with a single default account.

### 10.1 Config file

- **Format:** TOML (parsed with stdlib `tomllib`). Optional JSON support may mirror it.
- **Discovery order** when `--config` is not given: `./cloudbreachgraph.toml`, then
  `$XDG_CONFIG_HOME/cloudbreachgraph/config.toml` (default `~/.config/cloudbreachgraph/config.toml`).
  A missing config file is **not** an error unless `--account` was requested and can't be resolved.
- **Shape:** each account has a human alias (the table key), an `account_id`, a `profile`, and
  an optional default `region`:

```toml
# cloudbreachgraph.toml
default_account = "prod"        # optional: used when --account is omitted but a config exists

[accounts.prod]
account_id = "111111111111"
profile    = "prod-audit"
region     = "us-east-1"        # optional per-account default region

[accounts.staging]
account_id = "222222222222"
profile    = "staging-audit"

[accounts.sandbox]
account_id = "333333333333"
profile    = "sandbox-ro"
```

- `--account` accepts **either** an alias (`prod`) **or** a raw 12-digit account id
  (`111111111111`); resolve by matching either the table key or the `account_id` field.
- A canonical example ships at `docs/examples/cloudbreachgraph.example.toml`.

### 10.2 Resolution API (Phase 1 owns this; Phase 3 CLI consumes it)

`config.py` should expose roughly:

```python
def load_config(path: str | None) -> AccountConfig            # discovery + parse; empty if none
def resolve_profile(cfg: AccountConfig, *, account: str | None,
                    profile_override: str | None) -> Resolved  # -> {profile, account_id, region}
```

Where `Resolved.profile` may be `None` (meaning "use the CLI default"). The resolver applies
the precedence above and raises a clear error if `--account` was given but matches nothing in
the config.

### 10.3 Account verification (recommended)

After resolving a profile, run `aws sts get-caller-identity --profile <Y>` once and compare the
returned `.Account` to the expected `account_id` from the mapping. On mismatch, **stop** with a
clear error ("profile `prod-audit` resolves to account 999… but config says 111…") — this
prevents mapping a graph while unknowingly pointed at the wrong account. Make this a
`--verify-account/--no-verify-account` toggle (default on when an `account_id` is known). Record
the resolved/verified account id in `Graph.meta`.

### 10.4 Optional: map several accounts in one run

Because the operator has many per-account profiles, a `--all-accounts` flag may iterate every
account in the config, running the full collect→build→write pipeline per account and writing
per-account outputs (e.g. `graph.<alias>.json` / `graph.<alias>.dot`). This stays within the
single-account-per-graph model (no merged cross-account graph); it just loops. Treat it as a
Phase 3 stretch goal — if not built in v1, note it as future work in learnings.
