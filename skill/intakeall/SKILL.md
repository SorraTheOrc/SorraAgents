---
name: intakeall
description: "Automated batch intake for idea-stage work items. Discovers all items in idea stage and runs /intake for each sequentially, auto-completing well-defined items, detecting producer-input needs, and producing a summary report."
---

# IntakeAll

Use this skill when asked to run batch intake on all `idea` stage work items. It processes each item sequentially, auto-completes items with sufficient detail, detects items requiring producer input, and provides a summary report.

## Behavior

1. Query `wl list --stage idea --status open --json` to discover all eligible work items
2. For each item:
   - If the item has sufficient detail (acceptance criteria + implementation guidance), auto-complete it to `intake_complete` without invoking `/intake`
   - Otherwise, claim it (`wl update <id> --status in_progress`) and invoke `/intake`
3. Detect items needing producer input (unanswered questions, non-zero exit, or specific output patterns)
4. On error, attempt recovery (reset item status to `open`) and record recovery outcome
5. Continue processing remaining items even when one requires input or encounters an error
6. Produce a summary report showing totals and per-item outcomes with error/recovery details

## Command invocation

IntakeAll can be invoked in the following ways:

- `/skill:intakeall` — Process all idea-stage items with markdown summary output
- `/skill:intakeall --json` — JSON output for programmatic consumption
- `/skill:intakeall --parent-id <id>` — Post the summary as a comment on the specified parent work item
- `/skill:intakeall --dry-run` — Simulate processing without making any changes
- `python3 skill/intakeall/scripts/intakeall.py` — Direct Python invocation
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

Items with sufficient detail are auto-completed to `intake_complete` without invoking `/intake`:

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

## Error handling and recovery

- If `wl list` fails (non-zero exit or exception), returns an empty list gracefully
- If claiming an item fails, logs a warning and marks the item as `error`
- If `/intake` fails for an item, logs a warning, attempts recovery (resets item status to `open`), and continues to the next item
- All errors and recovery actions are captured in the summary report
- Recovery outcomes (success/failure) are included in per-item results

## Idempotence

- IntakeAll processes only items currently in `idea` stage
- Items that have already been intake-processed (moved past `idea`) are naturally excluded on subsequent runs
- Re-running IntakeAll is safe and will only process remaining idea-stage items
- Auto-completed items are advanced to `intake_complete` and excluded from future runs

## CLI flags

| Flag | Description |
|------|-------------|
| `--json` | Produce JSON output instead of Markdown |
| `--dry-run` | Simulate processing without making any changes |
| `--parent-id <id>` | Post the summary as a comment on the specified parent work item |
| `--verbose` | Enable verbose logging |

## Examples

```bash
# Process all idea-stage items
python3 skill/intakeall/scripts/intakeall.py

# JSON output
python3 skill/intakeall/scripts/intakeall.py --json

# Dry run (simulate without changes)
python3 skill/intakeall/scripts/intakeall.py --dry-run

# Post summary as a comment on a parent epic
python3 skill/intakeall/scripts/intakeall.py --parent-id SA-0MQK9SWN6008DWVQ
```

## Scripts

- Canonical runner: `skill/intakeall/scripts/intakeall.py`
- Tests: `skill/intakeall/tests/test_intakeall.py`

## Related skills

- `command/intake.md` — The `/intake` command that IntakeAll invokes for each item
- `skill/planall/SKILL.md` — PlanAll: the batch planning skill that IntakeAll mirrors
- `skill/ralph/SKILL.md` — Ralph orchestration loop that provides auto-intake for individual items
