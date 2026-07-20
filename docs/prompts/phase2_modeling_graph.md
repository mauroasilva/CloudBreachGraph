# Phase 2 Session Prompt — Domain Models, Graph & Relationship Mapping

> Paste everything below into a **fresh** Claude Code session opened on the CloudBreachGraph
> repo. Do not reuse the Phase 1 session.

---

You are implementing **Phase 2 of 3** of CloudBreachGraph, a Python CLI that uses the **AWS
CLI** (not boto3) to map an AWS account's network topology. Phase 1 (project scaffolding +
AWS CLI collection layer) is already committed.

**Before writing any code, read these — they are the source of truth:**
- `docs/README.md`
- `docs/01_overview.md`
- `docs/02_architecture.md`  (**§5 relationship-mapping rules are the core of your work**, plus §6 graph model)
- `docs/03_phase_plan.md`    (your scope is the **Phase 2** section and its interface contract)
- `docs/04_conventions.md`   (coding rules + the mandatory learnings-file template)
- **`learnings_phase1.md`** at the repo root — the actual collector output shape you must
  code against. If it's missing or thin, reconstruct the contract from Phase 1's committed
  code in `src/cloudbreachgraph/aws/` and note the gap at the top of your own learnings file.

## Your scope (Phase 2 only — do not build CLI or output writers)

1. `model/resources.py` — dataclasses `Eni`, `Ec2Instance`, `LoadBalancer`, `Subnet`, `Vpc`,
   each with a `from_collected(dict)` constructor that consumes Phase 1's normalized dicts.
2. `model/graph.py` — `Node`, `Edge`, `Graph` with `add_node` (merge attributes on duplicate
   id), `add_edge`, deterministic ordering, and `to_dict()` returning the exact structure in
   the Phase 2 interface contract in `docs/03_phase_plan.md` (this is Phase 3's contract).
3. `mapping/builder.py` — `build_graph(collected: dict) -> Graph` that applies, **in the
   order the user asked for**:
   - enumerate all **ENIs** (anchor nodes),
   - attribute each ENI to its **EC2 instance** or **load balancer** using the priority rules
     in `docs/02_architecture.md §5` (instance attachment wins; then ELBv2 `ELB app/net/gwy`
     description match; then Classic ELB description match; then interface-type fallback;
     otherwise no attachment — tag with `InterfaceType`),
   - map each ENI to its **subnet** (`in_subnet`),
   - connect each subnet to its **VPC** (`in_vpc`),
   - create `synthetic`/`unresolved` nodes for referenced-but-missing subnets/VPCs/LBs, and
     record `match_rule` in load-balancer edge attributes.
4. Tests for **each** rule: instance-attached ENI, ALB ENI, NLB ENI, Classic-ELB ENI,
   unattached service ENI (e.g. NAT/VPC-endpoint), and missing-subnet/VPC synthetic cases.
   Reuse/extend Phase 1 fixtures; tests run offline.

Do **not** implement `output/` writers or `cli.py` — that's Phase 3.

## Invariants your tests must guarantee
- No ENI is attached to both an instance and a load balancer.
- Every ENI has exactly one `in_subnet` edge; every subnet exactly one `in_vpc` edge.
- Output is deterministic (stable node/edge ordering).

## Constraints
- Python 3.11+, full type hints, `dataclasses`, standard library only for runtime.
- Read-only tool; you're only transforming already-collected data here.

## REQUIRED final step — write `learnings_phase2.md`
Before finishing, create **`learnings_phase2.md` at the repo root** using the
`docs/04_conventions.md` template. Capture especially:
- The exact `Graph.to_dict()` structure and the node/edge `attributes` keys you emit (Phase 3
  renders these).
- How each mapping rule behaved against the fixtures, and **any real-account ELB description
  quirks** you discovered or that remain unverified.
- Any deviation from the docs and why.
- Exact commands to run the tests.

Commit `learnings_phase2.md` with the Phase 2 code, then push. No pull request unless asked.
