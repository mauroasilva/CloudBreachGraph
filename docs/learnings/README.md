# Learnings

**Every Claude Code session that changes this repo writes exactly one learnings file here**, as
the required final step of that session. It captures the decisions, gotchas, and interface
notes the next session needs but can't get from the diff alone.

## Naming convention

- **Build phases** (the original 3-session build):
  `learnings_phase1.md`, `learnings_phase2.md`, `learnings_phase3.md`.
- **Every other change session:** `learnings_<YYYY-MM-DD>_<change-slug>.md`
  - `<YYYY-MM-DD>` — the session date (files sort chronologically).
  - `<change-slug>` — short kebab-case description of the change (e.g. `csv-output`,
    `flow-logs-role`, `fix-classic-elb-eu-west-1`).
  - Example: `learnings_2026-07-20_csv-output.md`.
  - If two sessions land the same slug on the same day, append `-2`, `-3`, …

## Contents

Use the template in `../04_conventions.md`. Keep it concrete and specific — a later agent in a
fresh session, with no memory, should be able to continue from your learnings file plus the code.
