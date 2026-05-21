---
description: Run Ralph background launch and status inspection for a Worklog item
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

- `ralph <work-item-id>` launches the deterministic loop in the background under `nohup` and writes runtime context under `.worklog/ralph/`.
- `ralph status` inspects the current runtime context and reports live or final status without requiring a work-item id.
- Validates `<work-item-id>` and stage precondition (`plan_complete`) before launching the loop.
- Targets only `<work-item-id>` + direct children.
- Iteratively runs implement + audit passes with remediation feedback.
- Logs every delegated `pi` and `wl` command before execution so the console and `--json` output show the exact command Ralph ran.
- Records structured audit output and AMPA-style deduplicated comment.
- Offers merge only after checks pass; merge executes only with explicit confirmation.

## Script entrypoint

```bash
# Run from the skill installation to avoid ambiguous relative paths.
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id>

# Inspect the current background run.
/home/rgardler/.pi/agent/skills/ralph/ralph status

# If you need to debug the foreground loop directly, run the Python script.
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id>

# If skills are installed elsewhere, run using the full path to the skill.
# python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id>
```
