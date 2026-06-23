---
name: intakeall
description: "Automated batch intake for idea-stage work items. Discovers all items in idea stage and runs /intake for each sequentially, auto-completing well-defined items, detecting producer-input needs, and producing a summary report."
---

# IntakeAll

Use this skill when asked to run batch intake on all `idea` stage work items. It processes each item sequentially, auto-completes items with sufficient detail, detects items requiring producer input, and provides a summary report.

## Behavior

1. **Pre-processing — orphan recovery**: Query `wl list --stage idea --json` (no status filter) to discover ALL items in `idea` stage. Orphaned items in contradictory states (`status=completed` + `stage=idea` or `status=in_progress` + `stage=idea`) are automatically reset to `status=open` via `_recover_orphans()` before any processing begins.
2. **Signal handler registration**: SIGINT and SIGTERM handlers are registered. If an external abort (Ctrl+C) occurs during processing, the currently-active item is recovered (reset to `status=open, stage=idea`) before the process exits.
3. For each item:
   - If the item has sufficient detail (acceptance criteria + implementation guidance), auto-complete it to `intake_complete` without invoking `/intake`
   - Otherwise, claim it (`wl update <id> --status in_progress --stage in_progress`) and invoke `/intake`
   - After `/intake` completes successfully, update to `stage=intake_complete, status=open`
4. Detect items needing producer input (unanswered questions, non-zero exit, or specific output patterns)
5. On error, attempt recovery (reset item stage to `idea` and status to `open`) and record recovery outcome
6. Continue processing remaining items even when one requires input or encounters an error
7. Produce a summary report showing totals and per-item outcomes with error/recovery details

## Command invocation

IntakeAll can be invoked in the following ways:

- `/skill:intakeall` — Process all idea-stage items with markdown summary output
- `/skill:intakeall --json` — JSON output for programmatic consumption
- `/skill:intakeall --parent-id <id>` — Post the summary as a comment on the specified parent work item
- `/skill:intakeall --dry-run` — Simulate processing without making any changes
- `/skill:intakeall --max N` — Process at most N items, then stop
- `/skill:intakeall --item-timeout N` — Set per-item subprocess timeout in seconds (default: 600)
- `python3 ./scripts/intakeall.py` — Direct Python invocation
- `pi run /intakeall` — Agent framework invocation

## Output

After processing all items, IntakeAll produces a summary report:

```
# IntakeAll Summary

**Total processed**: 5
**Auto-completed**: 2
**Intake completed**: 1
**Needs input**: 1
**Errors**: 1

## Results

- **SA-ITEM-001**: `auto_completed`
- **SA-ITEM-002**: `auto_completed`
- **SA-ITEM-003**: `intake_completed`
- **SA-ITEM-004**: `needs_input`
- **SA-ITEM-005**: `error`
  - Error: Intake failed (rc=1): timeout exceeded
  - Recovery: `reset_status_to_open` ✓
```

When `--json` is used, the output is a JSON object:

```json
{
  "total": 5,
  "auto_completed": 2,
  "intake_completed": 1,
  "needs_input": 1,
  "errors": 1,
  "items": [
    {"id": "SA-ITEM-001", "title": "...", "outcome": "auto_completed", "error_detail": null, "recovery": null},
    {"id": "SA-ITEM-005", "title": "...", "outcome": "error", "error_detail": "Intake failed (rc=1): timeout exceeded", "recovery": {"action": "reset_status_to_open", "success": true}}
  ]
}
```

## Auto-complete criteria

Items with sufficient detail are auto-completed to `stage=intake_complete, status=open` without invoking `/intake`:

- Item is NOT an epic
- Description contains measurable acceptance criteria (e.g., `## Acceptance Criteria` or `## Success Criteria`)
- Description has an implementation section (e.g., `## Implementation`, `## Desired Change`, `## Proposed Approach`)

This mirrors the PlanAll v2 auto-complete pattern (`has_sufficient_detail()`).

## Producer-input detection

IntakeAll detects items requiring producer intervention by:

- Non-zero exit code from the `/intake` command
- Presence of question-like patterns in the output (e.g., `? (yes/no)`, "What should", "Do you want")
- Any exception during the intake invocation

Items flagged as `needs_input` are not retried — the skill moves on to the next item.

## Orphan recovery

Before processing any items, `_recover_orphans()` scans all discovered items for orphaned states:

- Any item in `stage=idea` with `status=completed` is detected as an orphan
- Any item in `stage=idea` with `status=in_progress` is detected as an orphan
- Each orphan is reset via `wl update <id> --stage idea --status open --json`
- If `wl` rejects the status transition (e.g., `completed→open` is not allowed), the error is logged and processing continues — the item's in-memory status is still set to `open`
- Items already at `status=open` are unaffected
- During dry-run mode, no actual `wl` calls are made

## Signal handling (abort recovery)

SIGINT (Ctrl+C) and SIGTERM handlers are registered at the start of `run_all()`:

- The handler tracks which work item is currently being processed via `_current_item_id`
- On signal, the handler calls `_attempt_recovery()` for the current item (reset to `status=open, stage=idea`)
- After recovery, the process exits with code `128 + signum`
- If no item is being processed when the signal arrives, no recovery is attempted
- Handlers are restored to their original values in the `finally` block of `run_all()`

## Error handling and recovery

- If `wl list` fails (non-zero exit or exception), returns an empty list gracefully
- If claiming an item fails, logs a warning and marks the item as `error`
- After `/intake` succeeds for an item, updates to `stage=intake_complete, status=open`
- If `/intake` fails for an item, logs a warning, attempts recovery (resets item stage to `idea` and status to `open`), and continues to the next item
- All errors and recovery actions are captured in the summary report
- Recovery outcomes (success/failure) are included in per-item results

## Idempotence

- IntakeAll processes only items currently in `idea` stage
- Items that have already been intake-processed (moved past `idea`) are naturally excluded on subsequent runs
- Re-running IntakeAll is safe and will only process remaining idea-stage items
- Auto-completed items are advanced to `intake_complete` and excluded from future runs

## CLI flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--json` | flag | off | Produce JSON output instead of Markdown |
| `--dry-run` | flag | off | Simulate processing without making any changes |
| `--parent-id <id>` | str | None | Post the summary as a comment on the specified parent work item |
| `--max` | int | 0 | Maximum number of items to process (0 = no limit) |
| `--item-timeout` | int | 600 | Timeout in seconds for each item's subprocess call |
| `--verbose` | flag | off | Enable verbose logging |

## Examples

```bash
# Process all idea-stage items
python3 ./scripts/intakeall.py

# JSON output
python3 ./scripts/intakeall.py --json

# Dry run (simulate without changes)
python3 ./scripts/intakeall.py --dry-run

# Post summary as a comment on a parent epic
python3 ./scripts/intakeall.py --parent-id SA-0MQK9SWN6008DWVQ

# Process only the first 5 items
python3 ./scripts/intakeall.py --max 5

# Set per-item timeout to 300 seconds
python3 ./scripts/intakeall.py --item-timeout 300

# Combine --max and --item-timeout
python3 ./scripts/intakeall.py --max 3 --item-timeout 120
```

## Scripts

- Canonical runner: `./scripts/intakeall.py`
- Tests: `./tests/test_intakeall.py`

## Related skills

- `command/intake.md` — The `/intake` command that IntakeAll invokes for each item
- `../planall/SKILL.md` — PlanAll: the batch planning skill that IntakeAll mirrors
- `../ralph/SKILL.md` — Ralph orchestration loop that provides auto-intake for individual items
