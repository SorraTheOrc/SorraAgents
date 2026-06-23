---
name: implementall
description: "Automated batch implementation for plan_complete work items. Discovers all items in plan_complete stage and runs /skill:implement for each sequentially, detecting producer-input needs, and producing a summary report."
---

# ImplementAll

Use this skill when asked to run batch implementation on all `plan_complete` work items. It processes each item sequentially, detects items requiring producer input, and provides a summary report.

## Behavior

1. Query `wl list --stage plan_complete --status open --json` to discover all eligible work items
2. For each item, claim it (`wl update <id> --status in_progress`) and invoke `/skill:implement <id>`
3. Detect items needing producer input (unanswered questions, non-zero exit, or specific output patterns)
4. On error, attempt recovery (reset item status to `open`) and record recovery outcome
5. Continue processing remaining items even when one requires input or encounters an error
6. Produce a summary report showing totals and per-item outcomes with error/recovery details

## Command invocation

ImplementAll can be invoked in the following ways:

- `/skill:implementall` — Process all plan_complete items with markdown summary output
- `/skill:implementall --json` — JSON output for programmatic consumption
- `/skill:implementall --parent-id <id>` — Post the summary as a comment on the specified parent work item
- `/skill:implementall --max N` — Process at most N items before stopping
- `/skill:implementall --dry-run` — Simulate processing without making any changes
- `python3 ./scripts/implementall.py` — Direct Python invocation
- `pi run /skill:implementall` — Agent framework invocation

## Output

After processing all items, ImplementAll produces a summary report:

```
# ImplementAll Summary

**Total processed**: 5
**Implemented**: 3
**Needs input**: 1
**Errors**: 1

## Results

- **SA-ITEM-001**: `implemented`
- **SA-ITEM-002**: `implemented`
- **SA-ITEM-003**: `implemented`
- **SA-ITEM-004**: `needs_input`
- **SA-ITEM-005**: `error`
  - Error: Implement failed (rc=1): timeout exceeded
  - Recovery: `reset_status_to_open` ✓
```

When `--json` is used, the output is a JSON object:

```json
{
  "total": 5,
  "implemented": 3,
  "needs_input": 1,
  "errors": 1,
  "items": [
    {"id": "SA-ITEM-001", "title": "...", "outcome": "implemented", "error_detail": null, "recovery": null},
    {"id": "SA-ITEM-005", "title": "...", "outcome": "error", "error_detail": "Implement failed (rc=1): timeout exceeded", "recovery": {"action": "reset_status_to_open", "success": true}}
  ]
}
```

## Producer-input detection

ImplementAll detects items requiring producer intervention by:

- Non-zero exit code from the `/skill:implement` command
- Presence of question-like patterns in the output (e.g., `? (yes/no)`, "What should", "Do you want")
- Any exception during the implement invocation

Items flagged as `needs_input` are not retried — the skill moves on to the next item.

## Error handling and recovery

- If `wl list` fails (non-zero exit or exception), returns an empty list gracefully
- If claiming an item fails, logs a warning and marks the item as `error`
- If `/skill:implement` fails for an item, logs a warning, attempts recovery (resets item status to `open`), and continues to the next item
- If `/skill:implement` output contains question patterns, marks the item as `needs_input` without recovery (status stays `in_progress` to preserve the question context)
- All errors and recovery actions are captured in the summary report
- Recovery outcomes (success/failure) are included in per-item results
- When any items report an error outcome, the summary report is wrapped with a
  **Script Execution Failure Notice** (first and last lines) using the shared
  utility at `skill/scripts/failure_notice.py`. This provides a prominent visual
  signal that some items failed during batch implementation.

## `--max` flag

The `--max` flag controls how many items are processed in a single run:

- `--max 0` (default): Process all eligible items (no limit)
- `--max N` (positive integer): Process at most N items before stopping

The count includes all processed items regardless of outcome (implemented, needs_input, error).

## Idempotence

- ImplementAll processes only items currently in `plan_complete` stage with `open` status
- Items that have already been implemented (moved past `plan_complete`) are naturally excluded on subsequent runs
- Re-running ImplementAll is safe and will only process remaining plan_complete items

## CLI flags

| Flag | Description |
|------|-------------|
| `--json` | Produce JSON output instead of Markdown |
| `--dry-run` | Simulate processing without making any changes |
| `--parent-id <id>` | Post the summary as a comment on the specified parent work item |
| `--max N` | Maximum number of items to process (0 = no limit) |
| `--verbose` | Enable verbose logging |

## Examples

```bash
# Process all plan_complete items
python3 ./scripts/implementall.py

# JSON output
python3 ./scripts/implementall.py --json

# Dry run (simulate without changes)
python3 ./scripts/implementall.py --dry-run

# Process at most 5 items
python3 ./scripts/implementall.py --max 5

# Set per-item timeout to 300 seconds
python3 ./scripts/implementall.py --item-timeout 300

# Combine --max and --item-timeout
python3 ./scripts/implementall.py --max 3 --item-timeout 120

# Post summary as a comment on a parent epic
python3 ./scripts/implementall.py --parent-id SA-0MQO6YMZ3006N5MG
```

## Scripts

- Canonical runner: `./scripts/implementall.py`
- Tests: `./tests/test_implementall.py`

## Related skills

- `../implement/SKILL.md` — The implement skill that ImplementAll invokes for each item
- `../planall/SKILL.md` — PlanAll: the batch planning skill that ImplementAll mirrors
- `../intakeall/SKILL.md` — IntakeAll: the batch intake skill that ImplementAll mirrors
- `../ralph/SKILL.md` — Ralph orchestration loop that provides auto-implementation for individual items
