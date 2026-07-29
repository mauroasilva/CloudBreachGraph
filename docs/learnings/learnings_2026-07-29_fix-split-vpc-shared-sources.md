# Learnings — 2026-07-29 fix-split-vpc-shared-sources

## 1. What this phase delivered
- **Bug fix** in `src/cloudbreachgraph/output/html_export.py`: `--split-by-vpc`
  (`cloudbreachgraph-to-html`) no longer drops a node that reaches several VPCs from every
  per-VPC file but one.
- New helper `_edge_vpc(edge, group, node_type) -> str`: decides the single VPC an edge belongs to
  by the edge's **semantics** (relationship), not by either endpoint's node type.
- Rewrote `split_by_vpc(graph)`: it now builds a per-node **set** of VPCs (`node_vpcs`) — seeded
  from each node's single home group, then extended by every VPC an incident edge is placed in —
  so a shared source appears (with its edge) in *every* VPC it reaches.
- Tests (`tests/test_convert.py`): the two reproductions from the change request (shared CIDR;
  security-group-as-source across a peering) plus a shared `flow_peer` case, and a `_base_two_vpcs`
  helper. Existing `test_split_by_vpc_partitions_nodes_and_edges` still holds unchanged.
- Docs: README "Splitting per VPC" subsection and `docs/02_architecture.md §7` split bullet.

## 2. Interface contract for the next phase
- `split_by_vpc(graph: Graph) -> dict[str, Graph]` — signature and return shape unchanged (ordered
  by VPC id, `meta` copied, empty when no VPC nodes). Only the *contents* of each sub-graph changed:
  a shared source now appears in multiple sub-graphs. Every edge is still placed in **exactly one**
  VPC; only **nodes** are duplicated across VPCs.
- `_edge_vpc(edge, group, node_type) -> str` — module-private. `group` is the `_vpc_group_of(graph)`
  map (node id → single home VPC or `_UNASSIGNED`); `node_type` is `{node id: type}`. Returns
  `_UNASSIGNED` for edges that resolve to no real VPC (they are dropped by the caller).
- **Invariant guaranteed by the caller**: for every edge placed in VPC `v`, both its endpoints are
  added to `v` (the placement loop adds `v` to `node_vpcs[source]` and `node_vpcs[target]`), so no
  edge references a missing node.
- `_vpc_group_of` is unchanged and still **single-assigns** every node — that is correct and
  required for the single-page ringed / hierarchical / overlap-free layouts (each node drawn once).
  Do **not** make `_vpc_group_of` multi-valued; the multi-assignment lives only in `split_by_vpc`.

## 3. Decisions & rationale
- **Fix follows edge semantics, not node type.** The naive fix ("also duplicate cidr/internet/
  flow_peer nodes") leaves the cross-VPC **security-group-as-source** case broken, because an SG has
  a real home VPC yet can still be a `can_reach` *source* into other VPCs. `_edge_vpc` keys off the
  relationship instead:
  - `can_reach` / `routable_can_reach` / `not_routable_can_reach` (`_REACH_RELS`) → VPC of the
    **target** (the reached ENI/SG, which always resolves to a real VPC). Source→target direction is
    guaranteed by the builder (`mapping/builder.py`, `Edge(source=sid, target=eni_id, ...)`).
  - `connects_to` → VPC of the **ENI end** (the non-`flow_peer` endpoint; falls back to `source`
    when both ends are ENIs, e.g. a cross-VPC flow, anchoring on the initiator). `connects_to` is
    directional and can be either flow_peer↔ENI or ENI↔ENI (`mapping/flowlogs.py`).
  - everything else is structural (`in_vpc`/`in_subnet`/`attached_to`/`secured_by`) with both ends
    in one VPC → that VPC (and dropped if the ends disagree, which well-formed AWS data never does).
- **Seed + edge-additions, not edge-only.** Seeding each node into its home group keeps home-only
  nodes present (e.g. a VPC with no resources), matching the old node loop. Edge placement is purely
  *additive* on top, so non-shared graphs (and the 4-VPC example) split byte-for-byte as before —
  the only behavioural change is shared sources now appearing in more than one file.
- **Only nodes duplicate; edges never do.** Each edge has one reached target ⇒ one VPC. A node is
  shared because it has several *edges*, one per VPC. A reach edge landing only in its target VPC is
  why `vpc-b`'s file never gains `vpc-a`'s own target SG (asserted in the new test).

## 4. Deviations from the plan
- None. The change request's root-cause analysis was accurate; implemented exactly the
  edge-semantics fix it called for.

## 5. Gotchas, surprises & AWS quirks
- `Graph.add_node`/`add_edge` **sort on read** (`Graph.nodes`/`.edges` sort by `(type, id)` /
  `(source, target, relationship)`), so insertion order into the sub-graphs does not affect output
  determinism — the `set` iteration in the placement loop is safe. Each sub-graph gets its own fresh
  `Node`/`Edge` copies (`dict(attributes)`), so duplicating a node across VPCs shares no mutable
  state.
- `internet` nodes are keyed **per-ENI** (`internet:<eni.id>`, see `builder.py`), so despite being
  in `_REACH_TYPES` they are never actually shared; only `cidr:<cidr>` nodes, `flow_peer` nodes and
  SG-as-source nodes are genuinely multi-VPC. The fix handles all of them via the edge rule without
  special-casing any type.
- A pulled-in foreign source appears as a **bare** node in the reached VPC (only the `can_reach` /
  `connects_to` edge, none of its own structural edges — those live in its home VPC). That is the
  intended "who can reach in" picture and matches how CIDR/internet sources already render.

## 6. Known gaps / TODO for later phases
- Cross-VPC **ENI↔ENI** `connects_to` flows are placed only in the initiator's VPC (anchored on
  `source`), not mirrored into the responder's VPC. This is strictly better than `main` (which
  dropped them entirely) and outside the reported bug's scope; revisit if per-VPC files should show
  such a flow from both sides.

## 7. How to verify this phase
- `pip install -e .` (once), then:
  - `python -m pytest tests/test_convert.py -k split_by_vpc -q` — the split tests, incl. the three
    new shared-source reproductions.
  - `python -m pytest -q` — full suite (offline; mocks at `aws/runner.py`).
  - `ruff check src/cloudbreachgraph/output/html_export.py tests/test_convert.py`.
- Manual: run the two reproductions from the change request against `split_by_vpc` and confirm
  `vpc-b`'s sub-graph now contains the shared node and its `can_reach` edge.
