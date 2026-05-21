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
- Logs every delegated `pi` and `wl` command before execution so the console and `--json` output show the exact command Ralph ran.
- Records structured audit output and AMPA-style deduplicated comment.
- Offers merge only after checks pass; merge executes only with explicit confirmation.

## Script entrypoint

```bash
# Run from the skill installation to avoid ambiguous relative paths.
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id>

# If skills are installed elsewhere, run using the full path to the skill.
# python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id>
```
