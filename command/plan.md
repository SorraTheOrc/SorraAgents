---
description: Decompose an epic into features and tasks
tags:
  - workflow
  - plan
  - decomposition
  - effort-risk
  - threshold-check
agent: build
subtask: true
---

> **Note:** The canonical source for planning is the ``skill/plan/SKILL.md`` skill file.
> This command file is a legacy adapter that delegates to the skill. When the agent
> framework supports skill commands, prefer ``/skill:plan <work-item-id>`` over this command.
>
> See [skill/plan/SKILL.md](../skill/plan/SKILL.md) for the full, authoritative planning workflow.

You are helping the team decompose a Worklog epic (or other Worklog work item) into **features**
and **implementation tasks** by delegating to the canonical planning skill.

## Inputs

- The supplied `<work-item-id>` is $1.
  - If no valid `<work-item-id>` is provided (ids are formatted as `<prefix>-<hash>`), ask the
    user to provide one.
  - Optional additional freeform arguments may be provided to guide your work. Freeform
    arguments are found in the arguments string `$ARGUMENTS` after the `<work-item-id>` ($1).

## Results and Outputs

- The parent work item ($1) or each of the epics below it are decomposed into child feature
  work items.
- Idempotence: The command reuses existing child work items and updates or augments previously
  generated feature work-items instead of creating duplicates.

## Procedure

This command delegates the full planning workflow to the canonical skill at
[skill/plan/SKILL.md](../skill/plan/SKILL.md). Follow that file's instructions for:

1. **Claim and Status lifecycle** — set status to `in_progress` before any other action
2. **Seed context** — gather docs, work item details, and related artifacts
3. **Pre-check: Effort/Risk Threshold** — run the effort/risk gate (see below for command paths)
4. **Process** — evaluate, fetch, interview, propose, automated review stages, update work items
5. **Automated review on existing content** — auto-complete path when pre-check returns `skip`
6. **Calculate Effort and Risk** — estimate the planned work
7. **Traceability & idempotence** — ensure re-runs don't create duplicates
8. **Editing rules & safety** — preserve intent, keep changes conservative
9. **Finishing** — set `plan_complete` stage, sync, display
10. **Appendix** — maintain auditable Q&A from interviews

> Read [skill/plan/SKILL.md](../skill/plan/SKILL.md) before proceeding with any planning task.

## Note

- This Hard requirements section is populated with the mandatory progression rule above;
  review the rest of the hard requirements for task-specific constraints.

## Pre-check: Effort/Risk Threshold

Before starting the planning process, run the effort/risk check to determine whether
the work item is small enough to skip full planning. This uses the shared logic from
`skill/plan/plan_helpers.py` (canonical) or the legacy wrapper `command/plan_helpers.py`:

```bash
python3 skill/plan/plan_helpers.py plan-if-needed <work-item-id>
# or (legacy compatibility)
python3 command/plan_helpers.py plan-if-needed <work-item-id>
```

Interpret the JSON result (`skip` or `plan`) per the instructions in
[skill/plan/SKILL.md](../skill/plan/SKILL.md) (see Pre-check section).

## Examples

- `/plan wl-456` — Start an interview to break epic `wl-456` into feature and task work items.
- `/plan wl-456 MVP first` — Same as above, but seeds the interview with "MVP first".
