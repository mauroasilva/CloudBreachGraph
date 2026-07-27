# Learnings — 2026-07-27 unrecognised ENIs (flagged guesses) + `cloudbreachgraph-merge`

Two connected changes so the `flow_logs` role stops **silently dropping** flow records whose home
ENI can't be reconstructed (ENIs created outside CloudTrail's 90-day retention), plus a read-only,
AWS-free auxiliary CLI to enrich a finished `graph.json` from offline sources. All stdlib-only,
deterministic, `ruff`-clean, tests fully offline.

## 1. What this change delivered

1. **Emit unrecognised ENIs, with guesses flagged** (`mapping/flowlogs.py`). Any flow-record
   `interface-id` in **neither** the current nor the historical inventory becomes an `eni` node
   flagged `unrecognised: true` / `origin: "flow_log"` / `needs_review: true`. Its own IP is
   **inferred** and stored **only** under a new `inferred_private_ips: [{ip, method, confidence}]`
   list — **never** in `private_ips` (stays `[]`, confirmed-only). Its flows are mapped; a peer
   matching its inferred IP forms an ENI↔ENI edge; a peer matching no ENI stays a `flow_peer`.
2. **Shared CloudTrail→ENI parser** (`aws/cloudtrail_enis.py`, **new**). The reconstruction that was
   inline in `collectors.collect_historical_enis` is now the pure function
   `enis_from_events(events: list[dict]) -> list[dict]`, reused by the live collector **and** the
   merge tool. One parser, no divergence.
3. **`cloudbreachgraph-merge`** (`merge.py`, **new** `[project.scripts]` entry point). Read-only,
   AWS-free CLI that merges an existing `graph.json` with a `--data` file (ENI→owner→ASG) and/or a
   `--cloudtrail` file of older events (via `enis_from_events`), producing a new `graph.json`.
   `--template` emits a skeleton data file of the graph's `needs_review` ENIs.

### Files touched
- `aws/cloudtrail_enis.py` (**new**): `enis_from_events`, `cloudtrail_detail`, `EVENT_NAMES`, and the
  helpers moved out of `collectors.py` (`_earliest`, `_iface_ips`, `_iface_groups`, `_tag_items`,
  `_merge_historical`, `_absorb_create_network_interface`, `_absorb_run_instances`,
  `_terminated_instance_ids`, `_HISTORICAL_DEFAULTS`).
- `aws/collectors.py`: `collect_historical_enis` now runs the 4 per-EventName queries, filters each
  event by the name it was queried under, then delegates to `enis_from_events`. Helpers/constants
  removed. `_report_historical_enis` uses `cloudtrail_enis.EVENT_NAMES`.
- `mapping/flowlogs.py`: `map_flow_logs(..., vpcs=None)`; new `_vpc_networks`, `_in_any_network`,
  `_infer_own_ip`, `_add_unrecognised_enis`; `map_flow_logs` adds unrecognised entries to the
  inventory and re-indexes before `_map_connections` (which is otherwise unchanged — its old
  `if home is None: continue` now only fires for a blank `interface_id`).
- `mapping/builder.py`: passes `vpcs=list(vpcs.values())` into `map_flow_logs`.
- `merge.py` (**new**): `main`, `enrich`, `build_template`, `_ResolvedEni`, source parsers.
- `pyproject.toml`: `cloudbreachgraph-merge = "cloudbreachgraph.merge:main"`.
- Tests: `tests/test_merge.py` (new, 17), additions to `tests/test_flowlogs.py` (4). Docs: `§5.7.1`
  point 5 + the shared-parser note, `§6` attributes, `§7` merge-tool bullet.

## 2. Interface contract for the next session

- **New `eni` node attributes** (only under `--flow-logs`, only on unrecognised ENIs):
  `unrecognised: true`, `origin: "flow_log"`, `needs_review: true`, `inferred_private_ips`
  (`[{ip, method, confidence}]`, `method ∈ {"vpc_cidr", "recurring_side"}`); `private_ips` stays `[]`.
- **`aws/cloudtrail_enis.enis_from_events(events)`** is the single reconstruction entry point. Input
  is raw `Events[]` (each with a `CloudTrailEvent` string or dict); it dispatches each event by its
  **own** `eventName`, so a flat mixed list is fine and duplicates merge idempotently. Output is the
  same normalized `HistoricalEni`-shaped dicts as before.
- **`merge.enrich(graph, {id: _ResolvedEni}) -> Graph`** is a pure, rebuild-style view transform
  (mirrors `mapping/collapse.py`): copy nodes/edges into a fresh `Graph`, re-pointing any
  `flow-peer:<ip>` / `cidr:<ip>/32` node an ENI now owns onto that ENI (self-loops dropped, edges
  deduped). No informative input ⇒ the graph object is returned unchanged.
- **Data-file schema**: `{"enis": [{id, private_ips[], owner:{id,type,name}, asg, subnet_id, vpc_id,
  name, description}]}`. Blank/malformed rows and unknown keys (e.g. the template's echoed
  `inferred_private_ips` hint) are ignored. `--template` output is a valid, fill-in-then-`--data`
  round-trip.
- **`map_flow_logs`** grew a keyword-only `vpcs: list[Vpc] | None = None` (defaults empty). The
  builder supplies it; callers without VPCs still work (no `vpc_cidr` method, `recurring_side` only).

## 3. Decisions & rationale

- **Guesses kept out of `private_ips`.** The acceptance criteria are explicit: an inferred IP must be
  in separate, clearly-labelled properties and the node marked `needs_review`. `private_ips` stays
  `[]` so an auditor (or a downstream consumer) never mistakes a guess for a confirmed address; the
  merge tool is what promotes a guess to `private_ips` once confirmed.
- **Own-IP inference = the recurring address, VPC-CIDR-preferred.** Every flow captured on the ENI
  has the ENI's own IP on one side, so it's the address that recurs across the ENI's records; peers
  vary. Preferring a recurring address that also sits inside a **known VPC CIDR** pins the internal
  side with high confidence; with no VPC match we fall back to the bare most-recurring address (low
  confidence). Ties break to the lexically smallest address for determinism.
- **Inferred IP added to the resolver inventory (unbounded lifetime).** That's what makes the
  unrecognised ENI's own flows map *and* lets a peer matching the inferred IP resolve to it (ENI↔ENI)
  rather than becoming a spurious `flow_peer`. It's internal-only state; the audited truth is the node
  attributes.
- **One shared parser via a new module, not a helper in `collectors.py`.** The design asked for
  `aws/cloudtrail_enis.py::enis_from_events`. Moving the pure logic there (a) removes the future
  duplication between the live collector and the merge tool and (b) keeps `merge.py` from importing
  the AWS-touching `collectors.py`. `collect_historical_enis` keeps the per-EventName query loop
  (CloudTrail requires one `lookup-events` per `EventName`) and the "filter each event to the name it
  was queried under" robustness guard, then delegates.
- **`enis_from_events` dispatches by each event's own `eventName`.** The old inline version knew the
  queried name per loop; the shared function can't, so it reads `eventName` from the parsed detail.
  Real events always carry it; the collector's per-query filter means no duplicates reach the parser
  for a distinct-per-query mock, and idempotent merging covers a shared-response mock.
- **Merge is a rebuild transform, not in-place mutation.** Follows `collapse.py`: build a new `Graph`
  with `add_node`/`add_edge` (which already dedup + merge), so peer-absorption re-pointing and
  owner/ASG attachment compose cleanly and determinism/idempotency fall out.
- **`--data` overrides `--cloudtrail`.** Folded cloudtrail first, data second, with "later non-null
  wins" — the operator's file is the authority when both describe an ENI.

## 4. Deviations from the plan
- None. The refactor target (`aws/cloudtrail_enis.py::enis_from_events`), the node shape, and the
  `cloudbreachgraph-merge` CLI all match the change request.

## 5. Gotchas, surprises & AWS quirks
- **`load_json` doesn't wrap `FileNotFoundError`** (only `JSONDecodeError`); `load_graph(p, fmt="json")`
  does. `merge.main` uses `load_graph` for the input so a missing graph returns exit 2, not a
  traceback.
- **Re-index after adding unrecognised entries.** `_build_inventory` calls `inv.index()` at its end;
  the unrecognised entries are added *after* that, so `map_flow_logs` calls `inventory.index()` again
  before `_map_connections`, or their inferred IPs wouldn't resolve.
- **The checked-in flow-log fixtures use only known home ENIs** (`eni-00instance0000001`,
  `eni-00nlb00000000003`), so the change is byte-inert on the existing `flow_bundle` graph — the new
  behaviour only fires on records whose home id isn't in the inventory (covered by dedicated tests).
- **`_report_historical_enis` counts changed meaning slightly** (events matched per query, not items
  absorbed). Nothing asserts the diagnostic text.

## 6. Known gaps / follow-ups
- **Unrecognised ENIs aren't placed** in a subnet/VPC (flow logs don't reveal it), so they land in the
  ringed layout's "unassigned" cluster until `cloudbreachgraph-merge --data` supplies `subnet_id`.
- **Inference is single-IP.** An unrecognised ENI with several own IPs gets only its most-recurring
  one inferred; the rest look like peers until confirmed via merge.
- **Merge trusts the data file.** No validation that a user-supplied `private_ips` is plausible for the
  ENI's subnet; it's operator ground truth by construction.
- **DOT/HTML styling** for `unrecognised` ENIs reuses the plain `eni` style (not dashed/greyed like
  `historical`); a distinct style could be added if the review wants unrecognised ENIs visually
  called out.

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 370 tests, all offline
ruff check . && ruff format --check .

# Unrecognised ENIs (offline, from fixtures) — the fixture homes are all known, so build a tiny
# bundle in a REPL or see tests/test_flowlogs.py::test_unrecognised_eni_* for the shape.

# Merge round-trip (offline):
cloudbreachgraph-merge graph.json --template -o data.json     # skeleton of needs_review ENIs
#   ...fill in data.json (private_ips, owner, asg, subnet_id, vpc_id)...
cloudbreachgraph-merge graph.json --data data.json --cloudtrail older-events.json -o merged.json
#   merged.json: the unrecognised ENIs upgraded (needs_review cleared, inferred_private_ips gone,
#   private_ips confirmed), owners + asg_name attached, any matching flow_peer/cidr guess absorbed.
```
