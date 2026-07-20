# CloudBreachGraph — Deployment Plan

This `docs/` folder is the shared source of truth for building **CloudBreachGraph**, a
Python application that uses the **AWS CLI** to build a map (graph) of an AWS account.

The graph is built bottom-up from the network layer:

```
Network Interface (ENI)
      │
      ├──► EC2 Instance      (if attached to an instance)
      ├──► Load Balancer     (if owned by an ALB / NLB / Classic ELB)
      │
      └──► Subnet ──► VPC
```

> **Read order for every session:** `01_overview.md` → `02_architecture.md` →
> `03_phase_plan.md` → `04_conventions.md` → `05_roadmap.md` → the relevant prompt in
> `prompts/`, plus the `learnings_phaseX.md` files produced by earlier phases (see below).

## The build is split into 3 segregated Claude Code sessions

Each phase runs in its **own fresh Claude Code session** with no shared memory. Sessions
communicate **only** through:

1. The committed code from the previous phase.
2. The design documents in this `docs/` folder.
3. A **`learnings_phaseX.md`** file that each phase writes in `docs/learnings/`
   (see `04_conventions.md`) capturing every decision, deviation, assumption, and gotcha
   that later phases must know about.

| Phase | Session prompt | Theme | Depends on |
|-------|----------------|-------|------------|
| 1 | `prompts/phase1_foundation_collection.md` | Project scaffolding + AWS CLI collection layer | — |
| 2 | `prompts/phase2_modeling_graph.md` | Domain models + graph construction & relationship mapping | Phase 1 |
| 3 | `prompts/phase3_output_cli.md` | Rendering, visualization, end-to-end CLI, tests | Phases 1–2 |

## How to run the build

1. Open a **new** Claude Code session.
2. Paste the contents of `prompts/phase1_foundation_collection.md` as the first message.
3. Let it complete. It will commit code **and** write `docs/learnings/learnings_phase1.md`.
4. Open **another new** session, paste `prompts/phase2_modeling_graph.md`, and so on.

Never run two phases in the same session — segregation is intentional. It keeps each
session's context small and forces the interface contracts between phases to be explicit
and written down.

## The learnings files (mandatory output of every phase)

Every phase **must** end by producing `learnings_phaseX.md`. This is not optional and is
called out explicitly in each prompt. See `04_conventions.md` for the required template.
If a later phase finds the previous phase's learnings file missing or thin, it should
note that as a risk in its own learnings file and reconstruct the contract from the code.

## Documents in this folder

| File | Purpose |
|------|---------|
| `README.md` | This index. |
| `01_overview.md` | Vision, scope, goals, non-goals, the target AWS account model. |
| `02_architecture.md` | Technical design: layout, data model, AWS CLI commands, the relationship-mapping rules (the heart of the app). |
| `03_phase_plan.md` | Phase breakdown, interface contracts between phases, deliverables, acceptance criteria. |
| `04_conventions.md` | Coding standards, testing strategy, AWS access assumptions, and the `learnings_phaseX.md` template. |
| `05_roadmap.md` | Extensibility model + future features (e.g. cross-account VPC flow logs) and how resource roles plug in. |
| `learnings/` | Where each phase writes its `learnings_phaseX.md` handoff file. |
| `examples/cloudbreachgraph.example.toml` | Sample account→profile mapping config ("for account X use profile Y"). |
| `prompts/phase1_foundation_collection.md` | Copy-paste prompt for the Phase 1 session. |
| `prompts/phase2_modeling_graph.md` | Copy-paste prompt for the Phase 2 session. |
| `prompts/phase3_output_cli.md` | Copy-paste prompt for the Phase 3 session. |
