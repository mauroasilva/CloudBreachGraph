# Learnings — 2026-07-22 optional-security-groups

Follow-up to the two 2026-07-22 reachability changes. Makes security groups an **optional layer**
via a flag on both `cloudbreachgraph` and `cloudbreachgraph-to-html`.

## 1. What this change delivered
- **Two reachability shapes** in `mapping/builder.py`, chosen by `build_graph(...,
  show_security_groups: bool = True)`:
  - **Shown (default), `_map_reachability_via_sgs`** — security groups are nodes: `ENI ─secured_by→
    SG`, and each SG's inbound sources link to the SG (`source ─can_reach→ SG`). Internet is
    per-SG (`internet:<sg-id>`); a peer-SG reference is an `SG ─can_reach→ SG` edge. No routability
    (it's per-ENI). SG node id is the raw `sg-<id>` (attrs `group_id`, `vpc_id`).
  - **Hidden, `_map_reachability_direct`** — the old direct shape: `source ─*_can_reach→ ENI` with
    the routability split, no SG nodes. A peer-SG reference is **expanded to the private IPs of the
    referenced SG's member ENIs** (`/32` `cidr` nodes).
- **`--security-groups / --no-security-groups`** (BooleanOptionalAction, default on) on `cli.py`
  (drives `build_graph`) and on `convert.py`.
- **`mapping/collapse.py::collapse_security_groups(graph)`** — a view transform used by the
  converter's `--no-security-groups`: collapses a *shown* graph to the hidden shape (bringing IP
  sources forward, expanding peer SGs to member `/32`s). It only ever **removes** SG nodes, and
  **no-ops** unless the graph has `secured_by` edges (so it can't mangle an already-collapsed graph
  or an older/foreign graph shape). Collapsed edges are plain `can_reach` (a written graph has no
  route data).
- **DOT** (`dot_export.py`): `security_group` nodes cluster inside their VPC (`_node_vpc` uses their
  `vpc_id`); `secured_by` edges are dashed purple (`#7E57C2`).
- **HTML ringed** (`html_export.py`): now **6 rings** (`_RING_COUNT=6`): VPC·subnet·ENI·EC2/LB·
  **security_group (4)**·**IP source internet/cidr (5)**. `_vpc_group_of` traces `secured_by` and
  transitive `source→SG→ENI`; sources align to the ENIs behind their SGs. Optimizer ring tuples
  extended to `(1,2,3,4,5)`.

## 2. Interface contract for the next session
- `build_graph(collected, *, include_orphans=False, show_security_groups=True)`.
- **New edge relationship `secured_by`** (ENI → SG). The `*_can_reach` family now targets **SGs**
  (shown) or **ENIs** (hidden). Anything grouping reachability must treat `secured_by` separately
  from `can_reach` (see `html_export._REACH_RELS`, which is the can_reach family only).
- **`security_group` node id changed**: raw `sg-<id>` now (was `sg-source:<gid>` in the prior
  reachability PR). `sg-source:` no longer exists.
- **Internet node id is context-dependent**: `internet:<sg-id>` in the shown shape,
  `internet:<eni-id>` in the hidden shape.
- `html_export._REACH_TYPES` is now `{internet, cidr}` (SG is its own ring, handled separately).
- `mapping/collapse.py::collapse_security_groups(Graph) -> Graph` is the shown→hidden transform.

## 3. Decisions & rationale
- **Default = SGs shown** (confirmed with the user via a question). The attached anonymised graph
  made the case: **613 of 751 edges** were direct source→ENI reach edges, one CIDR fanning out to
  **20 ENIs**; routing those through the ~22 shared SG nodes collapses the hairball. `secured_by`
  adds ~56 shared membership edges but removes hundreds of source spokes.
- **Routability only in the hidden shape.** Routability is a *(source, ENI)* verdict; a shared SG
  can front ENIs in different subnets, so a `source→SG` edge has no single verdict. So the shown
  shape is plain `can_reach`; the routable/not-routable split lives on the hidden `source→ENI`
  edges. Documented in §5.6.
- **Peer-SG references expand to member private IPs when hidden** — literally "the IPs behind those
  security groups" (the user's words). Without this a peer rule would vanish in the hidden view;
  expanding it surfaces the concrete addresses. In-VPC `/32`s, so routable via the local route.
- **Converter collapse is gated on `secured_by`.** First cut stripped all reachability from the
  *old-format* anonymised graph (its `security_group` nodes reach ENIs directly, no `secured_by`).
  Gating on `secured_by` presence makes collapse a safe no-op on anything that isn't a new shown
  graph — verified against the anonymised graph (`collapse` is byte-identical no-op there).
- **New module `mapping/collapse.py`** rather than growing `builder.py`/`graph_io.py` — it's a pure
  graph→graph transform, cohesive on its own. Recorded in `02_architecture.md §2`.

## 4. Deviations from the plan
- The default output shape changed (SGs now shown by default), a deliberate product decision
  confirmed with the user. The prior reachability tests were rehomed to the hidden mode (they now
  build with `show_security_groups=False` via a `_collapsed` helper) and shown-mode tests added.
- `RouteResolver.classify`'s `security_group` branch is now effectively dead (hidden mode feeds it
  only internet/cidr — peer SGs are pre-expanded to `/32` cidrs). Kept as a defensive default;
  noted in §5.6.

## 5. Gotchas / surprises
- Flipping the default broke ~22 tests (fixture graphs are shown by default now). Expected — the
  reachability assertions moved to `--no-security-groups`.
- A shown-mode `security_group` node can be **both** a membership hub (an ENI's SG) **and** a source
  (a peer reference from another SG's rule). Same raw `sg-<id>` node, edges merge — this is correct
  and is exercised by the fixture (`sg-0aaa0002`).
- The ringed `_ring_radii` collapses empty rings to radius 0, so the hidden shape (empty SG ring 4)
  nests the source ring 5 straight onto the ENIs with no gap — no special-casing needed.
- Converter `--no-security-groups` can only *remove* SG nodes; it cannot reconstruct an SG layer
  that a hidden-built graph never had. Documented in the CLI help and README.

## 6. Known gaps / follow-ups
- The converter's collapsed edges are plain `can_reach` (no routability) because a written graph
  has no route tables. Only the builder's native hidden mode produces the routability split.
- Peer-SG expansion needs the referenced SG's members to be **collected ENIs**; a peer SG with no
  collected members contributes nothing in the hidden view.
- No test renders the SG-shown ringed layout for a *multi-VPC* graph with SGs spanning VPCs; the
  grouping picks the first VPC deterministically (documented in `_vpc_group_of`).

## 7. How to verify
```bash
pip install -e '.[dev]'
pytest                       # 217 passing, offline
ruff check . && ruff format --check .
# Default (SGs shown): SG nodes + secured_by, no routability.
cloudbreachgraph --from-cache tests/fixtures --output-dir /tmp/cbg-sg
# Hidden: sources straight to ENIs, routable/not-routable, peer SG -> member /32.
cloudbreachgraph --from-cache tests/fixtures --no-security-groups --output-dir /tmp/cbg-nosg
# Converter collapse of a shown graph:
cloudbreachgraph-to-html /tmp/cbg-sg/graph.json --no-security-groups -o /tmp/collapsed.html
```
