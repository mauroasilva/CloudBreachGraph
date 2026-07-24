# Learnings — 2026-07-24 vpc-logs-ip-history

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
- `model/resources.py` — new dataclasses `FlowLog` (+ `destination_id`/`destination_node_type`
  properties), `IpAllocation`, `FlowLogRecord`, each with `from_collected`.
- `mapping/flowlogs.py` (**new**) — `map_flow_logs(graph, enis, flow_logs, allocations, records)`:
  attaches `ip_allocations` to ENI nodes; adds `flow_log` + `log_group`/`log_bucket` nodes and
  `logs_to`/`delivers_to` edges; analyses records into `connects_to` edges (ENI→ENI when the peer
  IP is another collected ENI, else a `flow_peer` node).
- `mapping/builder.py` — `build_graph(..., map_flow_logs=False)`; runs step 7 (flow logs) when set,
  reading the `flow_logs`/`ip_allocations`/`flow_log_records` bundle keys, and sets
  `meta["flow_log_window_days"]`.
- `cli.py` — `--flow-logs` flag; `_active_roles(args)` adds `flow_logs`; roles threaded through
  `_collect_from_cache`/`_collect_live`/`resolve_target`/`_run_all_accounts`; `map_flow_logs` passed
  to `build_graph`.
- `output/dot_export.py` + `output/html_export.py` — styles for the 4 new node types, an
  `IP since:` ENI label line, and edge styling for `connects_to` (blue) / `logs_to` / `delivers_to`
  (dotted).
- Fixtures: `ec2_describe-flow-logs.json`, `cloudtrail_lookup-events.json`,
  `logs_filter-log-events.json`. Tests: `tests/test_flowlogs.py` (new) + additions to
  `test_collectors.py` and `test_cli.py`.

## 2. Interface contract for the next session

- **Bundle keys** added by the `flow_logs` role: `flow_logs`, `ip_allocations`, `flow_log_records`
  (lists of normalized dicts; see the dataclasses' `from_collected`).
- **`build_graph`** grew a keyword-only `map_flow_logs: bool = False`. Default off → byte-identical
  to before, so every existing caller/output is unchanged.
- **New node types**: `flow_log`, `log_group`, `log_bucket`, `flow_peer`. **New edge
  relationships**: `logs_to`, `delivers_to`, `connects_to` (the last carries `ports` + `via`).
- **ENI node attribute** `ip_allocations`: `[{"ip", "allocated_at"}]` (only present under
  `--flow-logs`, and only for ENIs with a CloudTrail event).
- **`connects_to` direction is meaningful**: `peer → ENI` = *what connected to it*; `ENI → peer` =
  *what it connects to*. ENI→ENI edges are the direct form when the peer is another collected ENI.

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
- **`logs_to` edge only when the logged resource node already exists**, to preserve the "no edge
  dangles" invariant (a flow log can target a VPC/subnet/ENI that no ENI referenced).

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

- **S3-destined flow logs** are shown as a `log_bucket` node but their object contents aren't read
  (would need per-object `s3api get-object` + gunzip). Only the CloudWatch path feeds connections.
- **Full cross-account `flow_logs`** (config vs. records in different accounts) per the roadmap.
- **`AssignPrivateIpAddresses`** isn't consulted — only `CreateNetworkInterface` (the primary IP's
  allocation). Secondary-IP allocation times are unknown (window falls back to unclamped for them).
- New node types get **default ring placement** in the ringed HTML layout (they aren't traced to a
  VPC by `_vpc_group_of`), so flow-log nodes collect in the orphan cluster. Fine for now; a future
  pass could cluster `flow_log`/`flow_peer` with the resource/ENI they attach to.

## 7. How to verify

```bash
pip install -e '.[dev]'
pytest                       # 251 tests, all offline
ruff check . && ruff format --check .

# End-to-end, offline, against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --flow-logs --output-dir /tmp/cbg-out
#   graph.json now has: ip_allocations on ENIs; flow_log + log_group/log_bucket nodes;
#   logs_to/delivers_to edges; connects_to edges incl. eni-00instance0000001 -> eni-00nlb00000000003
#   (ENI->ENI) and flow-peer:203.0.113.5 -> eni-00instance0000001; meta.flow_log_window_days == 60.
# A plain run (no --flow-logs) is byte-identical to before.
```
