## CHANGE REQUEST — Surface unrecognised ENIs (flagged guesses) + `cloudbreachgraph-merge` (data file AND CloudTrail file)

**Context**
ENIs created **>90 days ago** (outside CloudTrail retention) appear in flow logs but can't be
reconstructed automatically — long-lived ASG/instance ENIs especially. Today, flow records whose home
`interface-id` we can't resolve are silently dropped. Two changes: (1) surface **every** ENI the flow
logs reference as a node, and where we had to **guess** its IP, flag that clearly for review; (2) add
an auxiliary tool that enriches an existing `graph.json` from **either or both** a user data file and a
file of **older CloudTrail logs**, producing a new merged `graph.json`.

**What I want**
1. **Emit unrecognised ENIs, with guesses flagged.** Stop dropping records with an unknown home
   `interface-id`; add that ENI as a node. Any **inferred** private IP must be recorded in **separate,
   clearly-labelled node properties** (not mixed with confirmed IPs) with the inference method, and the
   node marked `needs_review`, so the user can audit every guess in `graph.json`.
2. **`cloudbreachgraph-merge`** — a read-only, AWS-free auxiliary CLI that merges into an existing
   `graph.json`: **(a)** a user **data file** (ENI → owner node → ASG), and/or **(b)** a file of
   **CloudTrail logs from further in the past** belonging to those ENIs. Both may be given **in tandem**.

**Acceptance criteria**
- With `--flow-logs`, an ENI seen only as a flow-log `interface-id` appears as an `eni` node flagged
  `unrecognised: true` / `origin: "flow_log"` / `needs_review: true`, with any guessed IP under
  `inferred_private_ips` (with `method` + `confidence`) — **never** silently in `private_ips`. Its flows
  are mapped; peers matching its inferred IP form ENI↔ENI edges; IPs matching no ENI stay `flow_peer`.
- `cloudbreachgraph-merge graph.json --data data.json --cloudtrail ct.json -o merged.json` enriches the
  graph from both sources deterministically: data-file and CloudTrail-reconstructed ENIs are added/merged,
  matching `unrecognised`/`flow_peer`/`cidr` nodes are upgraded (guesses replaced by confirmed values,
  `needs_review` cleared), owners + ASG membership attached. Either input may be omitted. `--template`
  emits a skeleton data file of the graph's `needs_review` ENIs.
- Read-only; stdlib only; deterministic output; `ruff`-clean; tests fully offline.

---

## Read first
- `docs/02_architecture.md` §3 (CloudTrail event shapes), §5.7 (flow logs), §6 (graph model), §7 (aux
  tools: `convert.py`, `anonymize.py`). `docs/04_conventions.md`. Relevant `docs/learnings/*`.
- Source: `mapping/flowlogs.py` (the drop point), `aws/collectors.py` (CloudTrail→ENI parsing:
  `_normalize_allocation_event` and, if present, the historical-ENI reconstruction — reuse it),
  `model/graph.py`, `output/graph_io.py` (`load_json`/`graph_from_dict`), `output/json_export.py`
  (`write_json`), `convert.py` + `anonymize.py` (aux-tool + entry-point pattern), `pyproject.toml`.
  Tests: `tests/test_flowlogs.py`, `tests/test_convert.py`, `tests/test_anonymize.py`, `tests/test_collectors.py`.

## Current seams
- `mapping/flowlogs.py::_map_connections`: `if not home or home not in eni_ips: continue` — the drop that
  erases unrecognised ENIs; also builds `ip_to_eni`/`eni_ips` and emits `connects_to`/`flow_peer`.
- CloudTrail parsing lives in `aws/collectors.py` (`_normalize_allocation_event` parses the
  `responseElements.networkInterface` out of a `CloudTrailEvent` string). **Refactor the CloudTrail →
  ENI-record reconstruction into a shared pure function** (e.g. `aws/cloudtrail_enis.py`:
  `enis_from_events(events: list[dict]) -> list[dict]`) used by BOTH the live collector and the merge
  tool, so there's one parser.
- Aux tools are `[project.scripts]` entries (`...convert:main`, `...anonymize:main`); each `main()`
  loads a graph via `graph_io` and writes via `write_json`.

## Design guidance

### Part 1 — Emit unrecognised ENIs with guesses flagged (`mapping/flowlogs.py`)
- Collect every distinct flow-record `interface-id`; any not in the known inventory is an
  **unrecognised ENI**. Remove the silent `continue`.
- **Own-IP inference:** its IP is the record address inside a **known VPC CIDR** (from collected `vpcs`);
  the other side is the peer. Else fall back to the recurring-side address. Track how it was derived.
- **Node shape — guesses clearly separated for review:**
  ```json
  { "id": "eni-xxxx", "type": "eni", "attributes": {
      "unrecognised": true, "origin": "flow_log", "needs_review": true,
      "private_ips": [],                                  // confirmed only — stays empty here
      "inferred_private_ips": [
        { "ip": "10.0.1.5", "method": "vpc_cidr", "confidence": "high" }
      ] } }
