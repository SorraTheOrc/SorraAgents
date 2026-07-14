---
name: planall
description: "Automated batch planning for intake_complete work items. Discovers all items in intake_complete status and invokes /plan for each sequentially, producing a summary report."
---

# PlanAll

Use this skill when asked to run batch planning on all `intake_complete` work items. It processes each item sequentially, detects items requiring producer input, and provides a summary report.

## Behavior

1. Query `wl list --stage intake_complete --json` to discover all eligible work items
2. For each item, claim it (`wl update <id> --status in_progress`) and invoke `/plan`
3. Detect items needing producer input (unanswered questions, non-zero exit, or specific output patterns)
4. Continue processing remaining items even when one requires input or encounters an error
5. Produce a summary report showing totals and per-item outcomes

## Command invocation

PlanAll can be invoked in the following ways:

- `/skill:planall` — Process all intake_complete items with markdown summary output
- `/skill:planall --json` — JSON output for programmatic consumption
- `/skill:planall --parent-id <id>` — Post the summary as a comment on the specified parent work item
- `/skill:planall --max N` — Process at most N items, then stop
- `/skill:planall --item-timeout N` — Set per-item subprocess timeout in seconds (default: 600)
- `python3 ./scripts/planall.py` — Direct Python invocation
- `pi run /planall` — Agent framework invocation

### CLI flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--json` | flag | off | Produce JSON output instead of Markdown |
| `--parent-id` | str | None | Post the summary as a comment on the specified parent work item |
| `--max` | int | 0 | Maximum number of items to process (0 = no limit) |
| `--item-timeout` | int | 600 | Timeout in seconds for each item's subprocess call |
| `--verbose` | flag | off | Enable verbose logging |

## Output

After processing all items, PlanAll produces a summary report:

```
# PlanAll Summary

**Total processed**: 5
**Planned**: 3
**Needs input**: 1
**Errors**: 1
**Remaining**: 2

## Results

- **SA-ITEM-001**: `planned`
- **SA-ITEM-002**: `planned`
- **SA-ITEM-003**: `needs_input`
- **SA-ITEM-004**: `error`
- **SA-ITEM-005**: `planned`
```

When `--json` is used, the output is a JSON object:

```json
{
  "total": 5,
  "planned": 3,
  "needs_input": 1,
  "errors": 1,
  "remaining": 2,
  "items": [
    {"id": "SA-ITEM-001", "title": "...", "outcome": "planned"}
  ]
}
```

## Producer-input detection

PlanAll detects items requiring producer intervention by:

- Non-zero exit code from the `/plan` command
- Presence of question-like patterns in the output (e.g., `? (yes/no)`, "What should", "Do you want")
- Any exception during the plan invocation

Items flagged as `needs_input` are not retried — the skill moves on to the next item.

## Error handling

- If `wl list` fails (non-zero exit or exception), returns an empty list gracefully
- If claiming an item fails, logs a warning and marks the item as `error`
- If `/plan` fails for an item, logs a warning, attempts recovery (resets item status to `open` and stage back to `intake_complete`), and continues to the next item
- On per-item timeout (`subprocess.TimeoutExpired`), the item is recovered same as failure and processing continues
- All errors are captured in the summary report

## Signal handling

PlanAll registers SIGINT and SIGTERM handlers for graceful abort. On receiving a signal:

1. The currently in-progress item is recovered — its stage is reset to `intake_complete` and status to `open`
2. Original signal handlers are restored
3. The process exits with the signal code (128 + signum)

This ensures no items are left in a stuck `in_progress` state if the batch process is interrupted.

## Idempotence

- PlanAll processes only items currently in `intake_complete` stage
- Items that have already been planned (moved past `intake_complete`) are naturally excluded on subsequent runs
- Re-running PlanAll is safe and will only process remaining intake_complete items

## Examples

```bash
# Process all intake_complete items
python3 ./scripts/planall.py

# JSON output
python3 ./scripts/planall.py --json

# Post summary as a comment on a parent epic
python3 ./scripts/planall.py --parent-id SA-0MQA6ECEU003GUKH

# Process only the first 5 items
python3 ./scripts/planall.py --max 5

# Set per-item timeout to 300 seconds (5 minutes)
python3 ./scripts/planall.py --item-timeout 300

# Combine --max and --item-timeout
python3 ./scripts/planall.py --max 3 --item-timeout 120
```

## Scripts

- Canonical runner: `./scripts/planall.py`
- Tests: `./tests/test_planall.py`

## Related skills

- `skill/plan/SKILL.md` — The `/skill:plan` command that PlanAll invokes for each item
- `../ralph/SKILL.md` — Ralph orchestration loop that provides auto-planning for individual items
