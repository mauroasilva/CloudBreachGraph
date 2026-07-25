# Learnings — 2026-07-24 vpc-logs-ip-history

> **Revised twice within the same session** after review feedback. The flow-log *configuration* is
> now a **`flow_logs` attribute on the VPC node** (the `flow_log`/`log_group`/`log_bucket` node types
> and the `logs_to`/`delivers_to` edges were removed). The `flow_peer` node now sits on the **outer
> IP-source ring** and clusters into its ENI's VPC. ENI→ENI edges gained a **temporal guard** (the
> peer ENI must have held the IP at record time). Finally, the per-ENI IP history is now an
> **`ip_history`** dict (`{ip: {start, end}}`), and **both** `ip_history` and the VPC `flow_logs`
> config are **JSON-only** — neither is drawn in the DOT or HTML output (those show only current
> IPs). Sections below reflect the final state.

## 1. What this change delivered

The **`flow_logs` role** (opt-in via `--flow-logs`): IP-allocation history + VPC Flow Log
analysis, folded into the existing graph. This is the "future role" the docs anticipated,
implemented via the documented extension model (registry data + collectors + a mapping module +
new node/edge types — **no** config-grammar or CLI-resolver change).

- `aws/collectors.py`
  - `FLOW_LOG_MAX_LOOKBACK_DAYS = 60` module constant (the flow-log window bound).
  - `collect_flow_logs(profile, region)` → `ec2 describe-flow-logs` → `.FlowLogs[]` (the log
    *configuration*: resource, destination type, log group / S3 ARN).
  - `collect_ip_allocation_events(profile, region)` → `cloudtrail lookup-events` for
    `CreateNetworkInterface`; parses each `CloudTrailEvent` JSON string into
    `{NetworkInterfaceId, PrivateIpAddress, AllocatedAt}`.
  - `collect_flow_log_records(profile, region)` → internally `describe-flow-logs` to find the
    CloudWatch log groups, then `logs filter-log-events --start-time=<now-60d>` per group; parses
    each default-format (version-2) flow record line.
  - Registered `flow_logs` in `ROLE_COLLECTORS` / `ROLE_RESULT_KEYS`
    (`["flow_logs", "ip_allocations", "flow_log_records"]`).
- `model/resources.py` — new dataclasses `FlowLog` (+ a `destination` property = log-group name or
  S3 ARN), `IpAllocation`, `FlowLogRecord`, each with `from_collected`.
- `mapping/flowlogs.py` (**new**) — `map_flow_logs(graph, enis, flow_logs, allocations, records)`:
  attaches an `ip_history` dict to **every** ENI node; records each flow log's destination as a **`flow_logs`
  attribute on the owning VPC node** (resolving subnet-/ENI-scoped flow logs up to their VPC via the
  graph's `in_subnet`/`in_vpc` edges); analyses records into `connects_to` edges — **ENI→ENI** when
  the peer IP is another collected ENI *that already held the IP at record time*, else a `flow_peer`
  node.
- `mapping/builder.py` — `build_graph(..., map_flow_logs=False)`; runs step 7 (flow logs) when set,
  reading the `flow_logs`/`ip_allocations`/`flow_log_records` bundle keys, and sets
  `meta["flow_log_window_days"]`.
- `cli.py` — `--flow-logs` flag; `_active_roles(args)` adds `flow_logs`; roles threaded through
  `_collect_from_cache`/`_collect_live`/`resolve_target`/`_run_all_accounts`; `map_flow_logs` passed
  to `build_graph`.
- `output/dot_export.py` + `output/html_export.py` — `flow_peer` fill colour (distinct from
  `internet`/`cidr`) and `connects_to` edge styling (blue). The VPC `flow_logs` config and the ENI
  `ip_history` are **not** rendered (JSON-only); the writers show only current IPs. In the ringed
  layout `flow_peer` is added to `_REACH_TYPES` (ring 5) and traced to its ENI's VPC via
  `connects_to` in `_vpc_group_of` + angle-aligned in `_ringed_view_data`.
- Fixtures: `ec2_describe-flow-logs.json`, `cloudtrail_lookup-events.json`,
  `logs_filter-log-events.json`. Tests: `tests/test_flowlogs.py` (new) + additions to
  `test_collectors.py` and `test_cli.py`.

## 2. Interface contract for the next session

- **Bundle keys** added by the `flow_logs` role: `flow_logs`, `ip_allocations`, `flow_log_records`
  (lists of normalized dicts; see the dataclasses' `from_collected`).
- **`build_graph`** grew a keyword-only `map_flow_logs: bool = False`. Default off → byte-identical
  to before, so every existing caller/output is unchanged.
- **New node type**: `flow_peer` (external IP seen in flow logs). **New edge relationship**:
  `connects_to` (carries `ports` + `via="flow_log"`). There are **no** `flow_log`/`log_group`/
  `log_bucket` nodes and **no** `logs_to`/`delivers_to` edges — flow-log config is a VPC attribute.
- **VPC node attribute** `flow_logs`: `[{flow_log_id, resource_id, destination_type, destination,
  traffic_type, status}]`, sorted by `flow_log_id` (only under `--flow-logs`). **JSON-only.**
- **ENI node attribute** `ip_history`: `{ip: {"start": iso|None, "end": iso|None}}` on **every** ENI
  under `--flow-logs` (`end=None` = current IP; a superseded IP's `end` = the successor's `start`).
  **JSON-only.** Both writers deliberately omit it — `dot_export._node_lines` and
  `html_export._detail_line` render only the ENI's *current* `private_ips`. (HTML never carried raw
  attributes anyway: `_view_data` ships only id/type/label/color/flags/`detail`.)
- **`connects_to` direction is meaningful**: `peer → ENI` = *what connected to it*; `ENI → peer` =
  *what it connects to*. ENI→ENI edges are the direct form when the peer is another collected ENI
  **and the temporal guard passes**.

## 3. Decisions & rationale

- **`--flag=value` for every value-carrying flag** in the flow_logs collectors (`--log-group-name=`,
  `--start-time=`, `--lookup-attributes=`). The `--from-cache` reader and the runner cache-key both
  key on *positional* args (`a for a in args if not a.startswith("-")`); a bare value like a log-group
  name or an epoch would otherwise be captured as positional and break the fixture filename. The `=`
  form keeps the whole flag `-`-prefixed → positional stays `["logs","filter-log-events"]` →
  fixture `logs_filter-log-events.json`.
- **`collect_flow_log_records` re-runs `describe-flow-logs` internally** to discover log groups.
  Collectors are independent by contract (only `(profile, region)`), so it can't receive the config
  from `collect_flow_logs`. The extra read-only call is cheap; the alternative (passing state
  between collectors) would break the §11.7 driver loop.
- **Allocation-time clamp lives in the mapping layer, not wall-clock filtering of output.** The
  60-day bound is applied at the *collection* query (`--start-time`); the mapping only compares a
  record's `start` epoch against the ENI's earliest allocation epoch — both from data — so the graph
  is **deterministic** regardless of when it's built. (Putting "now" into the output would have made
  fixtures rot over calendar time and broken determinism.)
- **`flow_peer` vs ENI→ENI**: matching the peer IP against a private-IP→ENI index built from the
  collected ENIs. If matched (and not the same ENI) → direct ENI→ENI edge; else an external
  `flow_peer` node. This is exactly the acceptance criterion.
- **Temporal guard on ENI→ENI (the reviewer's ask): keyed on the *IP*, not the ENI.** `ip_alloc_epoch[ip]`
  is the allocation time of whichever current ENI holds that IP. A record is dropped only when that
  epoch is **known** and the record's `start` is **before** it (historic reuse). Unknown → allowed,
  because an ENI that predates the 60-day window has no `CreateNetworkInterface` event in it yet has
  held the IP throughout — so "unknown" almost always means "held it the whole time". Historic
  records are **dropped**, not turned into a `flow_peer`, so an IP that currently belongs to an ENI
  never also appears as an external peer node.
- **Config on the VPC, not nodes (the reviewer's ask).** A flow log's `ResourceId` resolves up to a
  VPC through the graph's own `in_subnet`/`in_vpc` edges (built at that point), and the config is
  appended to that VPC node's `flow_logs` list. No dangling edges to worry about; a flow log whose
  VPC isn't in the ENI-anchored graph is simply skipped.
- **`flow_peer` on the IP-source ring.** Added `flow_peer` to `_REACH_TYPES` (→ ring 5) but gave it
  its **own** `_vpc_group_of` branch (traced via `connects_to`, not `_REACH_RELS`, since its edge can
  point either way) — do **not** just reuse `_vpc_of_source`, which only follows the `can_reach`
  family and would leave `flow_peer` unassigned when it's the *target* of an outbound edge.

## 4. Deviations from the plan

- **Reading flow-log record *contents*.** `docs/05_roadmap.md` originally said "show the destination
  node and whether delivery is active, not parse traffic." This change **does** parse records
  (that's the whole point of "analyse the VPC logs"). Documented in `§5.7` and the roadmap status
  note.
- **Read-only verbs beyond `describe`/`list`/`get`/`head`.** `cloudtrail lookup-events` and
  `logs filter-log-events` are read-only retrievals but don't match the prefix allowlist in the
  hard rules. Treated as compliant with the *intent* (§9: never mutate) and called out in `§3`/`§5.7`.
- **Single-account for now.** The role reads all three commands from its one bound account. Full
  cross-account splitting (config in workload account, records in log-archive) is still future
  (roadmap). The example TOML keeps `flow_logs = "log_archive"` (a `test_config` test pins it) but
  its comment now warns to bind flow_logs to the account owning the flow-log *config*.

## 5. Gotchas, surprises & AWS quirks

- VPC flow-log **default (v2) record format** is space-separated; field 2 = `interface-id`,
  3 = `srcaddr`, 4 = `dstaddr`, 6 = `dstport`, 7 = `protocol`, 10 = `start` (epoch **seconds**),
  12 = `action`. Any field can be `-` (NODATA/skipped) — records with a `-` address are dropped.
- CloudTrail's interesting fields are inside the **`CloudTrailEvent` JSON *string***
  (`responseElements.networkInterface.{networkInterfaceId,privateIpAddress}`), not the top-level
  event — must `json.loads` it.
- `datetime.fromisoformat` handles the `+00:00` offset (and, on 3.11+, a trailing `Z`); fixtures use
  `+00:00` to be safe.
- The `Graph.add_edge` de-dups on `(source, target, relationship)`, so `connects_to` ports must be
  **aggregated before** adding the edge (done in `_map_connections`), exactly like the reachability
  `ports`.

## 6. Known gaps / follow-ups

- **S3-destined flow logs**: the destination is recorded in the VPC's `flow_logs` attribute, but the
  object contents aren't read (would need per-object `s3api get-object` + gunzip). Only the
  CloudWatch path feeds the connection analysis.
- **Full cross-account `flow_logs`** (config vs. records in different accounts) per the roadmap.
- **`AssignPrivateIpAddresses`** isn't consulted — only `CreateNetworkInterface` (the primary IP's
  allocation). Secondary-IP allocation times are unknown, so the temporal guard treats those IPs as
  "held throughout" (allowed).
- **IP-history lookback is bounded to 60 days** (`--start-time = now − FLOW_LOG_MAX_LOOKBACK_DAYS`
  on `cloudtrail lookup-events`), aligned with the flow-log-record window instead of CloudTrail's
  90-day Event-history default. An ENI created before the window has no event → `ip_history` start
  unknown → treated as "held throughout".
- **Temporal guard depends on CloudTrail coverage.** If a reassigned IP's *new* owner ENI has no
  `CreateNetworkInterface` event in the (now 60-day) window, we can't detect the reuse and the edge
  is allowed — the deliberate "unknown → allowed" call. Tightening it would need a fuller IP-history
  source (a longer CloudTrail window — up to its 90-day max — or CloudTrail Lake / an S3 trail).

## 7. How to verify

```bash
pip install -e '.[dev]'
pytest                       # 252 tests, all offline
ruff check . && ruff format --check .

# End-to-end, offline, against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --flow-logs --output-dir /tmp/cbg-out
#   graph.json now has: ip_history on every ENI (JSON only); a `flow_logs` attribute on the VPC (its
#   CloudWatch + S3 destinations); connects_to edges incl. eni-00instance0000001 -> eni-00nlb...003
#   and -> eni-00alb...002 (ENI->ENI) and flow-peer:203.0.113.5 -> eni-00instance0000001;
#   the historic nlb->10.0.1.20 (2026-05-01, before the alb held that IP) is dropped;
#   meta.flow_log_window_days == 60. A plain run (no --flow-logs) is byte-identical to before.
```
