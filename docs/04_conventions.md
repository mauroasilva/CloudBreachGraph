# 04 — Conventions

Shared rules for every session. Following these keeps the three segregated sessions
compatible without constant re-reading.

## Code

- Python 3.11+, full type hints, `dataclasses` for models.
- Standard library first. **No required third-party runtime dependency.** `pytest` is a dev
  dependency; any convenience lib (e.g. `graphviz`) must be an optional extra.
- Keep modules small and single-purpose per the layout in `02_architecture.md §2`.
- Deterministic output: sort nodes/edges before serializing so JSON/DOT diffs are stable.
- Read-only: collectors may only invoke AWS `describe-*` calls. Never a mutating API.

## AWS access assumptions

- AWS CLI v2 is installed and on PATH; invoked as a subprocess with
  `--output json --no-cli-pager`.
- Credentials come from the environment/profile; pass `--profile`/`--region` through.
- Tests never hit the network — they mock the subprocess boundary and feed fixtures from
  `tests/fixtures/`.

## Testing

- `pytest`. Every phase adds tests for the code it introduces.
- Mock at the `runner` boundary (the function that shells out to `aws`) so collectors and
  mapping logic are tested with recorded JSON.
- Include at least the mapping edge cases enumerated in `03_phase_plan.md` (Phase 2).

## Git & branch

- Work on the branch named in your prompt. Commit in logical chunks with clear messages.
- Push when the phase's deliverables and its `learnings_phaseX.md` are complete.
- Do **not** open a pull request unless explicitly asked.

## The learnings file — REQUIRED output of every session

**Every Claude Code session that changes this repo ends by writing exactly one learnings file
in `docs/learnings/`**, committed alongside that session's code. Its job: tell the *next* agent
everything it needs that isn't obvious from the code alone. This applies to the original build
phases **and** to every later change session.

Naming (see `docs/learnings/README.md`):
- **Build phases:** `learnings_phase1.md`, `learnings_phase2.md`, `learnings_phase3.md`.
- **Every other change session:** `learnings_<YYYY-MM-DD>_<change-slug>.md` — date-prefixed so
  files sort chronologically, plus a short kebab-case slug (e.g.
  `learnings_2026-07-20_csv-output.md`). Add `-2`, `-3`, … to disambiguate same-day collisions.

Use this template (say "Change: <name>" instead of "Phase X" for change sessions):

```markdown
# Learnings — Phase X (<theme>)   ← or:  # Learnings — <YYYY-MM-DD> <change-slug>

## 1. What this phase delivered
- Bullet list of modules/functions/files added and what they do.

## 2. Interface contract for the next phase
- The exact shapes/signatures the next phase should code against.
- Where they live (module paths, function names).
- Any change from what docs/03_phase_plan.md predicted (and why).

## 3. Decisions & rationale
- Every non-obvious choice and why it was made (naming, normalization shape,
  library choices, layout changes vs. docs/02_architecture.md §2).

## 4. Deviations from the plan
- Anything done differently from docs/. If none, say "none".

## 5. Gotchas, surprises & AWS quirks
- Real AWS CLI schema surprises, empty-result behaviors, pagination notes,
  ELB description formats observed, anything that bit you.

## 6. Known gaps / TODO for later phases
- What's intentionally left undone and who should pick it up.

## 7. How to verify this phase
- Exact commands to run the tests / reproduce the output.
```

Keep it concrete and specific — a later agent in a fresh session with no memory should be
able to continue from your learnings file plus the code, without guessing.

## When a prior learnings file is missing or thin

If you start a phase and the expected `learnings_phase(X-1).md` is absent or unhelpful:
1. Reconstruct the contract by reading the previous phase's committed code.
2. Note the gap as a risk at the top of your own learnings file.
3. Proceed against the actual code, not the assumed contract.
