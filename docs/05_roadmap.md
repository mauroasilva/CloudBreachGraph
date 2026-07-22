# 05 — Extensibility & Roadmap

CloudBreachGraph v1 maps the **network** role (ENIs → EC2/LB → subnets → VPCs) for a single
account. This document records how the design stays open to future features that pull
**different resource types from different accounts**, so later sessions can add them without
reworking the config grammar, the CLI, or the graph model.

Read `02_architecture.md §11` (resource roles & multi-account targets) first — this doc is the
forward-looking companion to it.

## The extension model in one paragraph

Every collectable resource type belongs to a **role**. A role is fetched from exactly one
account per run. Users bind roles to accounts inside a **target** (`02_architecture.md §11.2`),
and a role resolves to actual `aws` commands through the explicit `ROLE_COLLECTORS` registry
(`02_architecture.md §11.6`). Adding a feature that reads a new resource type = (1) add a new
entry to `ROLE_COLLECTORS`/`ROLE_RESULT_KEYS`, (2) write that role's `collect_x(profile, region)`
collectors, (3) map its nodes/edges into the existing graph model, (4) let users bind the role to
an account in config. Steps 1 and 4 are pure data; the config grammar, CLI, resolver, and driver
loop (`§11.7`) don't change.

## First planned future feature: VPC Flow Logs (`flow_logs` role)

### Why it's cross-account
VPC Flow Logs are frequently centralized: a VPC in a **workload account** publishes its flow
logs to **CloudWatch Logs** or an **S3 bucket** in a separate **log-archive / logging account**.
So mapping "which VPC/subnet/ENI has flow logs, and where they go" needs:
- the **workload** account's profile to read the *flow-log configuration* (see below), and
- the **log-archive** account's profile to read the *actual log destinations/records*.

This is precisely why config binds roles to accounts rather than assuming one account per run.

### Data sources (all read-only)
- Flow-log *configuration* (which resources log, and to where) lives with the resource's
  account: `aws ec2 describe-flow-logs` → `.FlowLogs[]` (fields: `ResourceId` (vpc-/subnet-/eni-),
  `LogDestinationType` (`cloud-watch-logs` | `s3`), `LogGroupName` / `LogDestination` (ARN),
  `DeliverLogsStatus`, `TrafficType`). This is part of the workload account and could even be
  folded into the `network` role later; keep it in `flow_logs` for a clean account boundary.
- Log *destinations* live in the logging account: CloudWatch Logs
  (`aws logs describe-log-groups`) or S3 (`aws s3api head-bucket` / `list-objects-v2` on the
  destination bucket). Reading actual log *contents* is out of scope for the mapping tool; the
  map should show the destination node and whether delivery is active, not parse traffic.

### How it lands in the graph (no model change)
Reuse the existing `Node`/`Edge` model from `02_architecture.md §6`:
- New node types: `flow_log` (the config), `log_group` or `log_bucket` (the destination).
- New edges: `logs_to` (VPC/subnet/ENI → flow_log), `delivers_to` (flow_log → destination).
- Tag destination nodes with the account they were read from (`meta`/attributes), so the map
  visibly spans accounts.

### Required IAM (future)
Workload profile: `ec2:DescribeFlowLogs`. Logging profile: `logs:DescribeLogGroups` and/or
`s3:ListBucket` on the destination bucket. Keep everything read-only.

### Config example (future)
```toml
[targets.prod]
default_account = "workload_prod"
[targets.prod.roles]
flow_logs = "log_archive"
```
`cloudbreachgraph --target prod` would then read `network` from `workload_prod` and `flow_logs`
from `log_archive`, in one run.

## Other candidate roles (not yet designed)
- `dns` — Route 53 / Resolver, often centralized in a networking account.
- `cloudtrail` — organization trail in a management/audit account.

**Shipped since v1** (all part of the `network` role, not separate roles — the resources live in
the same account as the ENIs they govern):
- **Security groups** — `collect_security_groups` feeds the ENI *reachability* mapping (source
  `internet`/`cidr`/`security_group` nodes + `*_can_reach` edges, `02_architecture.md §5.5`). See
  `learnings_2026-07-22_eni-reachability-mapping.md`.
- **Route tables** — `collect_route_tables` + `mapping/routing.py` add *routability*: each
  reachability edge is split into `routable_can_reach` / `not_routable_can_reach` (or plain
  `can_reach` when undetermined), so the map distinguishes an ENI merely *allowed* by a `0.0.0.0/0`
  rule from one *actually routable* (`02_architecture.md §5.6`). See
  `learnings_2026-07-22_routable-reachability.md`.
- **NAT gateways & VPC endpoints (ENI owners)** — `collect_nat_gateways` / `collect_vpc_endpoints`
  give previously-ownerless service ENIs a real owner node (`nat_gateway` / `vpc_endpoint`),
  attributed from each resource's authoritative ENI list (`NatGatewayAddresses[].NetworkInterfaceId`
  / `NetworkInterfaceIds[]`), not description parsing (`02_architecture.md §5.4`). They share the
  load balancer's role class / layout ring. See `learnings_2026-07-22_eni-ownership-nat-gateways.md`.

### Remaining ENI owners (follow-up)
Some service-managed ENIs still resolve to no owner because they lack a clean authoritative
`describe-*` ENI list and are only identifiable by `Description`/`RequesterId`: **RDS** /
**ElastiCache** databases, **Lambda** VPC ENIs, **EFS** mount targets, **Transit Gateway**
attachments. Each follows the §5.4 recipe once a reliable ENI-ownership signal is found (a
`describe-*` that lists the ENIs, or a stable description pattern) — add a collector + registry
entry + attribution branch, mapping to a new owner node type. Prefer authoritative ENI-id lists
over description parsing wherever the API offers one.

Deeper routing (NACLs, TGW/VPN route propagation, cross-VPC peering path validation) remains future
work — the current model is a documented approximation, not a full route simulator.

Each follows the same recipe. When any of these is picked up, add a short section here and, per
the build process, do it in its **own** phase with its **own** `learnings_phaseX.md`.

## Guardrails to preserve while extending
- Read-only always (`describe-*`, `get-*`, `list-*`, `head-*` only).
- No new **required** third-party runtime dependency.
- Config grammar (`accounts` + `targets.<name>.roles`) is stable — extend by adding role names,
  not by changing structure.
- The graph stays a single `Graph` of typed nodes/edges; cross-account data is distinguished by
  node attributes, not by a separate model.
