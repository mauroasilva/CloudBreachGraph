# 03 — Phase Plan & Interface Contracts

Three phases, three **separate** Claude Code sessions. Each phase is defined by its inputs
(what earlier phases produced), its deliverables, its acceptance criteria, and the
**interface contract** it exposes to the next phase. Contracts are what make segregated
sessions safe: as long as a phase honors its contract, the next session can build against it
without re-reading everything.

If a phase must deviate from a contract, it records the deviation in `learnings_phaseX.md`.

---

## Phase 1 — Foundation & AWS CLI Collection

**Goal:** Stand up the project and a reliable, testable data-collection layer.

**Deliverables**
- `pyproject.toml`, package skeleton under `src/cloudbreachgraph/`, `README.md` stub.
- `aws/runner.py`: a subprocess wrapper that runs `aws <args> --output json --no-cli-pager`,
  threads through `--region`/`--profile`, parses JSON, and raises a clear error on failure.
- `config.py`: the account→profile + role/target loader/resolver described in
  `02_architecture.md §10–§11` — `load_config(path)` (TOML via stdlib `tomllib`, with the
  discovery order) supporting both `[accounts.*]` and `[targets.*]`. Resolution is **role-aware**:
  `resolve_target(cfg, target=..., account=..., profile_override=...) -> ResolvedTarget` whose
  `.roles` maps each role → `{profile, account_id, region}`, with `resolve_profile(...)` kept as a
  thin single-account/`network`-role wrapper. Apply the precedence (`--profile` override →
  `--target`/`--account` binding / `default` → CLI default). Include a helper that runs
  `sts get-caller-identity` **once per distinct resolved account** and verifies it matches the
  expected account id. **v1 only activates the `network` role**, but build the resolver and the
  registry so `flow_logs` (and other roles) can be bound in config without grammar changes
  (`02_architecture.md §11`, `05_roadmap.md`).
- `aws/collectors.py`: functions returning normalized lists:
  - `collect_network_interfaces(...) -> list[dict]`
  - `collect_ec2_instances(...) -> list[dict]`  (flattened out of Reservations)
  - `collect_load_balancers_v2(...) -> list[dict]`
  - `collect_load_balancers_classic(...) -> list[dict]`
  - `collect_subnets(...) -> list[dict]`
  - `collect_vpcs(...) -> list[dict]`
  - `collect_all(...) -> dict` bundling the above under fixed keys (see contract).
- Optional `--cache-dir` raw-JSON dump.
- `tests/fixtures/` with at least one recorded/representative JSON sample per command, plus
  unit tests that mock `subprocess` and assert the collectors normalize correctly.
- Tests for `config.py`: alias lookup, account-id lookup, `--profile` override precedence,
  missing-config fallback, unresolvable `--account`/`--target` error, and a multi-account
  target that resolves `network` and `flow_logs` roles to **different** accounts/profiles. Parse
  the shipped `docs/examples/cloudbreachgraph.example.toml` in a test so the example stays valid.

**Acceptance criteria**
- `pip install -e .` works; `python -c "import cloudbreachgraph"` works.
- Collectors run against fixtures without touching the network in tests.
- Runner surfaces AWS CLI stderr on non-zero exit.
- `resolve_profile` honors the precedence in `02_architecture.md §10`.
- `learnings_phase1.md` written (see `04_conventions.md` template).

**Interface contract exposed to Phase 2** — `collect_all()` returns exactly:
```python
{
    "meta": {"region": str, "profile": str | None, "account_id": str | None},
    "network_interfaces": [ <normalized ENI dict>, ... ],
    "ec2_instances":      [ <normalized instance dict>, ... ],
    "load_balancers_v2":  [ <normalized elbv2 dict>, ... ],
    "load_balancers_classic": [ <normalized classic elb dict>, ... ],
    "subnets":            [ <normalized subnet dict>, ... ],
    "vpcs":               [ <normalized vpc dict>, ... ],
}
```
Each normalized dict **must** preserve the fields listed in `02_architecture.md §4`
(at minimum the id + the fields used for mapping). Phase 1 documents the exact normalized
shape it settled on in `learnings_phase1.md` — Phase 2 codes against that.

**Interface contract exposed to Phase 3** — the `config.py` resolution API (`load_config`,
`resolve_target`/`resolve_profile`, account-verification helper) from `02_architecture.md §10–§11`,
plus the `collect_all(region=..., profile=...)` signature. Phase 3's CLI wires user flags
(`--target`/`--account`/`--profile`) into these. Phase 1 records the exact function signatures
and the `ResolvedTarget`/`ResolvedAccount` shapes it settled on in `learnings_phase1.md`.

---

## Phase 2 — Domain Models, Graph Construction & Relationship Mapping

**Goal:** Turn collected data into a graph by applying the relationship rules.

**Inputs:** Phase 1 code + `docs/learnings/learnings_phase1.md` (the exact collector output shape).

**Deliverables**
- `model/resources.py`: dataclasses `Eni`, `Ec2Instance`, `LoadBalancer`, `Subnet`, `Vpc`,
  each with a `from_collected(dict)` constructor.
- `model/graph.py`: `Node`, `Edge`, `Graph` with `add_node` (merge-on-duplicate),
  `add_edge`, deterministic ordering, and `to_dict()` (the Phase 3 contract).
- `mapping/builder.py`: `build_graph(collected: dict) -> Graph` implementing, in order:
  ENI enumeration → EC2/LB attribution (rules in `02_architecture.md §5`) → subnet → VPC,
  including synthetic/unresolved nodes and `match_rule` edge metadata.
- Tests covering each mapping rule: instance-attached ENI, ALB ENI, NLB ENI, Classic-ELB
  ENI, unattached service ENI (NAT/endpoint), and missing-subnet/VPC synthetic cases.

**Acceptance criteria**
- `build_graph(collect_all_fixture)` produces the expected nodes/edges deterministically.
- No ENI is ever attached to both an instance and a load balancer.
- Every ENI has exactly one `in_subnet` edge; every subnet has exactly one `in_vpc` edge.
- `learnings_phase2.md` written, including any real-account quirks in ELB description parsing.

**Interface contract exposed to Phase 3** — `Graph.to_dict()` returns:
```python
{
  "meta":  {...},
  "nodes": [ {"id","type","label","attributes"}, ... ],   # sorted by (type, id)
  "edges": [ {"source","target","relationship","attributes"}, ... ],  # sorted
}
```

---

## Phase 3 — Output, Visualization & End-to-End CLI

**Goal:** Make the tool usable end to end and produce the map artifacts.

**Inputs:** Phase 1 + Phase 2 code + `docs/learnings/learnings_phase1.md` + `docs/learnings/learnings_phase2.md`.

**Deliverables**
- `output/json_export.py`: `write_json(graph, path)`.
- `output/dot_export.py`: `write_dot(graph, path)` — nodes colored by type, VPC clustering,
  relationship-labeled edges; optional `render(dot_path, fmt)` shelling out to `dot`.
- `cli.py`: argparse entrypoint wiring `config.resolve_profile` → `collect_all` → `build_graph`
  → writers. Flags: `--account <alias|id>` and `--config <path>` (account→profile mapping, §10),
  `--profile <name>` (direct override), `--verify-account/--no-verify-account`, `--region`,
  `--cache-dir`, `--output-dir`, `--render {png,svg}`, `--from-cache <dir>` (build from
  previously cached JSON with no live calls), and optional `--all-accounts` (loop over configured
  accounts, one graph each — §10.4 stretch goal).
- `pyproject.toml` console-script entry `cloudbreachgraph = cloudbreachgraph.cli:main`.
- End-to-end test: fixtures → CLI (with `--from-cache`) → assert `graph.json` and
  `graph.dot` are produced and well-formed.
- Update the user-facing `README.md` with real usage examples.

**Acceptance criteria**
- `cloudbreachgraph --from-cache tests/fixtures/... --output-dir out/` produces
  `graph.json` + `graph.dot` offline.
- `cloudbreachgraph --account <alias>` resolves and uses the mapped profile; `--profile`
  overrides it; account verification catches a profile/account mismatch.
- Graceful degradation when `dot` is not installed (still writes `.dot`, warns on render).
- Read-only guarantee holds (no mutating calls anywhere).
- `learnings_phase3.md` written, including final CLI surface and any follow-up ideas.

---

## Cross-phase dependency graph

```
Phase 1 (collection) ──► Phase 2 (graph/mapping) ──► Phase 3 (output/CLI)
      │                        │                            │
   docs/learnings/        docs/learnings/              docs/learnings/
   learnings_phase1.md    learnings_phase2.md          learnings_phase3.md
      └───────────────────────┴────────────────────────────┘
                    all committed; each read by later phases
```

## Definition of done for the whole build
- One command against a live account yields a correct `graph.json` + `graph.dot`.
- All three `docs/learnings/learnings_phaseX.md` files exist and are accurate.
- Tests pass offline via fixtures; the tool never mutates AWS.

---

## Future phases (post-v1, not built now)

These are designed for but out of scope for the v1 build above. Each is its own segregated
session with its own `docs/learnings/learnings_phaseX.md`, following `05_roadmap.md`.

- **Phase 4 — `flow_logs` role (cross-account VPC Flow Logs).** Add the `flow_logs` collectors
  (workload account: `ec2:DescribeFlowLogs`; logging account: CloudWatch Logs / S3 destinations),
  new `flow_log`/`log_group`/`log_bucket` node types and `logs_to`/`delivers_to` edges, and wire
  the role into the existing role-aware collection loop. No config-grammar or CLI change needed —
  users just bind `flow_logs` to their logging account in a target. See `02_architecture.md §11`
  and `05_roadmap.md`.
- **Later roles** (`dns`, `cloudtrail`, deeper networking) follow the same pattern.
