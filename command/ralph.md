---
description: Run Ralph implement→audit loop for a Worklog item
tags:
  - workflow
  - ralph
agent: patch
---

# Ralph

Run the dedicated ralph orchestrator loop for a target work item.

## Usage

- `/ralph <work-item-id>`

## Behavior

- Validates `<work-item-id>` and stage precondition (`plan_complete`).
- Targets only `<work-item-id>` + direct children.
- Iteratively runs implement + audit passes with remediation feedback.
- Records structured audit output and AMPA-style deduplicated comment.
- Offers merge only after checks pass; merge executes only with explicit confirmation.

## Script entrypoint

```bash
python skill/ralph/scripts/ralph_loop.py <work-item-id>
```
