# Learnings — 2026-07-27 historical ENIs, configurable window, ASG collapse

## 1. What this change delivered

Four connected extensions to the `flow_logs` role so it copes with **churning Auto Scaling groups**,
where 60 days of flow logs are full of records captured on **terminated** ENIs and traffic to/from
**reused** IPs. All read-only, stdlib-only, deterministic, `ruff`-clean, tests fully offline.

1. **Configurable flow-log-record window.** `--flow-log-days N` (default
   `FLOW_LOG_MAX_LOOKBACK_DAYS = 60`). Threaded via a module-level setter
   `collectors.set_flow_log_window(days)` (mirroring `configure_cache`/`set_verbose`) so the
   `collect_x(profile, region)` contract is untouched. Read by `collect_flow_log_records` (record
   window).
2. **90-day CloudTrail history.** New `CLOUDTRAIL_MAX_LOOKBACK_DAYS = 90`. Both CloudTrail collectors
   query `_cloudtrail_lookback_days() == min(90, max(days, 90))` — i.e. **always 90**, never shorter
   than the record window. `graph.meta` now carries `flow_log_window_days` (configured) **and**
   `cloudtrail_window_days` (90), set in `collect_all` when the `flow_logs` role runs.
3. **Historical-ENI reconstruction + time-aware resolution.** New collector
   `collect_historical_enis` (registry key `historical_enis`), new model `HistoricalEni`, a
   **combined current ∪ historical inventory** + a **time-indexed IP→ENI resolver** in
   `mapping/flowlogs.py`, and historical ENI **nodes** (flagged `historical`/`terminated`) created in
   `mapping/builder._map_historical_enis`.
4. **ASG collapse.** `--collapse-asgs` → `mapping/collapse.collapse_autoscaling_groups(graph)`, a
   view transform (same shape as `collapse_security_groups`) applied after the graph is built.

### Files touched
- `aws/collectors.py`: `CLOUDTRAIL_MAX_LOOKBACK_DAYS`, `_flow_log_window_days`/`_historical_enabled`
  module knobs + `set_flow_log_window`/`get_flow_log_window`/`set_historical_enis`/
  `_cloudtrail_lookback_days`; `collect_historical_enis` (+ CloudTrail parse helpers
  `_cloudtrail_detail`, `_iface_ips`, `_iface_groups`, `_tag_items`, `_merge_historical`,
  `_absorb_create_network_interface`, `_absorb_run_instances`, `_terminated_instance_ids`,
  `_report_historical_enis`); `collect_ip_allocation_events` now uses the 90-day lookback;
  registry entries for `historical_enis`; `collect_all` records both windows in `meta`.
- `model/resources.py`: `HistoricalEni` dataclass + `from_collected`; `Ec2Instance.asg_name`
  (from the `aws:autoscaling:groupName` tag via a new generic `_tag` helper).
- `mapping/flowlogs.py`: `_Inventory` (combined inventory + `resolve(ip, t)`/`ever_internal`/
  `alive_at`) and `_Entry`; `_build_inventory`; `map_flow_logs(..., historical=None)`; `_map_connections`
  rewritten to resolve home by `interface-id` and peer by time-indexed resolution.
- `mapping/builder.py`: `_map_historical_enis` + `_ensure_historical_instance_node`; `_instance_node`
  carries `asg_name`; step 7 wires historical ENIs + records both meta windows.
- `mapping/collapse.py`: `collapse_autoscaling_groups` + `_asg_node`.
- `cli.py`: `--flow-log-days`, `--historical-enis`/`--no-historical-enis`, `--collapse-asgs`; sets the
  collector knobs in `main`; applies the collapse in `_write_outputs`; `--from-cache` reader gains a
  per-EventName variant (`_cache_variant`).
- `output/dot_export.py` + `output/html_export.py`: `autoscaling_group` node style + label;
  `historical` nodes drawn **dashed/greyed**.
- Fixtures: `cloudtrail_lookup-events.{runinstances,terminateinstances,deletenetworkinterface}.json`.
  Tests: `test_collapse_asgs.py` (new) + additions to `test_collectors.py`, `test_flowlogs.py`,
  `test_cli.py`. `conftest.py` gained an autouse fixture resetting the collector knobs per test.

## 2. Interface contract for the next session

- **New bundle key** `historical_enis` (from `collect_historical_enis`): normalized dicts
  `{NetworkInterfaceId, PrivateIpAddresses[], SubnetId, VpcId, Groups[], Description, InterfaceType,
  RequesterId, InstanceId, AsgName, Name, CreatedAt, DeletedAt}`. Consumed by `HistoricalEni`.
- **`build_graph`** signature is unchanged (`map_flow_logs=False`); historical ENIs flow through the
  `historical_enis` bundle key, so callers that don't set `--flow-logs` are byte-identical to before.
- **New node type** `autoscaling_group` (id `asg:<group-name>`). **New node attributes**:
  `historical: true` + `status: "terminated"` + `terminated_at`/`created_at` on reconstructed ENIs
  and instances; `asg_name` on current ASG instances. **New `meta` keys**: `cloudtrail_window_days`
  (always 90) alongside `flow_log_window_days` (configured).
- **`collapse_autoscaling_groups(graph) -> Graph`** is a pure view transform: no ASG members ⇒ returns
  the graph **unchanged** (same object); idempotent; deterministic.

## 3. Decisions & rationale

- **Kept `collect_ip_allocation_events` *and* added `collect_historical_enis`.** The registry maps one
  collector → one bundle key, and the design explicitly asked for `historical_enis` as a *new* key, so
  consolidating into a single collector wasn't possible without reshaping the registry. The
  "consolidate rather than double-fetch" guidance is honoured *inside* the historical collector: it
  fetches `CreateNetworkInterface` once and derives per-ENI `ip_history` (created/deleted per IP) from
  the reconstruction, rather than a second history pass. The remaining minor redundancy (both
  collectors query `CreateNetworkInterface`) is the price of the 1-collector-1-key registry; the
  current-ENI `ip_history` path (`_map_ip_history`) is left exactly as it was so its tests stay green.
- **`collect_ip_allocation_events` now reaches 90 days** (was 60), because it is a CloudTrail history
  collector and the design says CloudTrail always reaches its retention max. The record window
  (`collect_flow_log_records`) is what `--flow-log-days` controls. One existing test's window
  assertion was updated from `FLOW_LOG_MAX_LOOKBACK_DAYS` to `CLOUDTRAIL_MAX_LOOKBACK_DAYS`.
- **Combined inventory + time-indexed resolver subsumes the old temporal guard.** The old
  `ip_to_eni` dict + `ip_alloc_epoch` guard could only reject "current ENI holds this IP but was
  allocated it after the record". The resolver generalises: an `(ip, t)` returns the ENI whose
  lifetime `[created, deleted]` contains `t` and that held the IP, tie-breaking to the latest
  `created ≤ t`. The old test cases fall out of it unchanged (current-ENI created times come from the
  `ip_allocations`), plus reused ASG IPs now attribute correctly.
- **Drop vs. `flow_peer`.** When no ENI held the peer IP at `t`: if the IP is *otherwise internal*
  (some inventory ENI held it at another time) the record is **dropped** — never invent an external
  peer for an address that is really an ENI's (preserves the pre-existing "historic reuse" behaviour);
  only a truly external IP becomes a `flow_peer`.
- **Home resolution by `interface-id` against the combined inventory** is what lets a flow on a
  **terminated** home ENI be analysed (it used to be dropped because the home wasn't in the current
  set). The home is still clamped to its lifetime (`alive_at`), so pre-creation traffic is dropped.
- **Historical nodes created in the *builder*, not the mapping.** The builder already has
  `_ensure_subnet_node`/`_ensure_vpc_node`/`_instance_node` and the collected subnet/VPC dicts for
  labels; the mapping (`flowlogs`) only needs the inventory for resolution. Keeps node-creation in one
  place and avoids a circular import (`flowlogs` importing `builder`).
- **ASG collapse is a post-build view transform**, matching `collapse_security_groups`. Non-member
  edges pass through **verbatim** (no `ports` re-serialisation) so only member-touching edges change —
  keeps the output stable and the "byte-identical without the flag" property crisp. Membership: the
  `aws:autoscaling:groupName` tag — current instances via `describe-instances`, current ENIs via their
  `attached_to` instance, historical via the `RunInstances` `tagSet`.

## 4. Gotchas, surprises & AWS quirks

- **`RunInstances` is essential.** Most instance ENIs have **no** standalone `CreateNetworkInterface`
  event — they're born inside the `RunInstances` `responseElements.instancesSet.items[].networkInterfaceSet`.
  That's also the only place the `aws:autoscaling:groupName` tag appears for historical members.
- **One CloudTrail query per EventName** (auto-paginated, can be heavy over 90 days). The mock in the
  runner-boundary tests keys on `args[:2]` and returns the *same* response for every EventName query,
  so the collector **filters each event by its own `eventName`** against the EventName it was queried
  under — a shared fixture can't be misread. The `--from-cache` reader disambiguates by an
  `EventName`-suffixed fixture (`cloudtrail_lookup-events.<eventname>.json`), falling back to the
  un-suffixed file (so `collect_ip_allocation_events` keeps reading `cloudtrail_lookup-events.json`).
- **Module-knob state leaks across tests.** `set_flow_log_window`/`set_historical_enis` are module
  globals; a `--flow-log-days 45` CLI test would otherwise poison a later collector test. Fixed with an
  **autouse conftest fixture** resetting them to defaults around every test.
- **Determinism under collapse.** Re-serialising a member edge's `ports` set would reorder a
  passthrough edge's port string; so non-member edges are copied verbatim and only member-touching
  edges get the union+sort. `resolve`'s IP index is sorted so ties break deterministically.
- **CloudTrail camelCase.** Inside the `CloudTrailEvent` **string**, keys are camelCase
  (`networkInterfaceId`, `privateIpAddressesSet.items[]`, `groupSet.items[].groupId`,
  `tagSet.items[].{key,value}`) — distinct from the PascalCase of `describe-*` responses.

## 5. Known gaps / follow-ups

- **Reachability isn't reconstructed for historical ENIs** — they get placement + attachment + flow
  edges, but no `secured_by`/`can_reach` (their SGs may be gone; out of scope).
- **Authoritative ASG membership.** v1 uses the tag; a read-only `autoscaling
  describe-auto-scaling-groups` collector could confirm current membership and names (noted, not built).
- **`AssignPrivateIpAddresses`** still isn't consulted — secondary-IP reassignments mid-life aren't
  reconstructed, so a secondary IP is treated as held for the whole `[created, deleted]` lifetime.
- **A reconstructed ENI with no delete event** is flagged `terminated` with `terminated_at = null`
  (it's simply absent from the current `describe-network-interfaces`); we can't always distinguish
  "terminated, delete not in window" from "still alive but filtered out".

## 6. How to verify

```bash
pip install -e '.[dev]'
pytest                       # 349 tests, all offline
ruff check . && ruff format --check .

# End-to-end, offline, against the checked-in fixtures:
cloudbreachgraph --from-cache tests/fixtures --flow-logs --flow-log-days 45 --collapse-asgs \
  --output-dir /tmp/cbg-out
#   graph.json: meta.flow_log_window_days == 45, meta.cloudtrail_window_days == 90;
#   one asg:web-asg autoscaling_group node absorbing eni-0asg…001/002 + i-0asg…001/002
#   (its member ENIs/instances are gone, in_subnet edges to both AZ subnets kept).
# Drop --collapse-asgs to see the historical ENIs as individual dashed/greyed terminated nodes.
```
