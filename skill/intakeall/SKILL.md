---
name: intakeall
description: "Automated batch intake for idea-stage work items. Discovers all items in idea stage and runs /intake for each sequentially, auto-completing well-defined items, detecting producer-input needs, and producing a summary report."
---

# IntakeAll

Use this skill when asked to run batch intake on all `idea` stage work items. It processes each item sequentially, auto-completes items with sufficient detail, detects items requiring producer input, and provides a summary report.

## Behavior

1. **Orphan recovery**: Query `wl list --stage idea --json` to find items in `idea` stage. Orphans (`status=completed` + `stage=idea` or `status=in_progress` + `stage=idea`) are reset to `status=open` via `_recover_orphans()` before processing.
2. **Signal registration**: SIGINT/SIGTERM handlers recover the active item (reset to `status=open, stage=idea`) on abort.
3. For each item:
   - If sufficient detail (ACs + implementation guidance), auto-complete to `intake_complete` without invoking `/intake`
   - Otherwise, **skip the interactive `/intake` subprocess** (blocks indefinitely waiting for stdin) and mark as `needs_input`
4. On error, attempt recovery (reset to `stage=idea, status=open`) and record outcome
5. Continue processing remaining items despite errors/input-needs
6. Produce summary report

## Invocation

```
/skill:intakeall [--json] [--parent-id <id>] [--dry-run] [--max N] [--item-timeout N]
python3 ./scripts/intakeall.py [same flags]
pi run /intakeall
```

## Output

### Markdown summary
```
# IntakeAll Summary
**Total**: 5 | **Auto-completed**: 2 | **Intake completed**: 1 | **Needs input**: 1 | **Errors**: 1
- **SA-ITEM-001**: `auto_completed`
- **SA-ITEM-005**: `error` — Intake failed (rc=1): timeout exceeded | Recovery: reset_status_to_open_with_stage_idea ✓
```

### JSON output (`--json`)
```json
{
  "total": 5, "auto_completed": 2, "intake_completed": 1, "needs_input": 1, "errors": 1,
  "items": [
    {"id": "SA-ITEM-001", "outcome": "auto_completed"},
    {"id": "SA-ITEM-005", "outcome": "error", "error_detail": "...", "recovery": {"action": "reset_status_to_open_with_stage_idea", "success": true}}
  ]
}
```

## Auto-complete criteria

Item is auto-completed to `stage=intake_complete, status=open` if: NOT an epic, description has measurable ACs (`## Acceptance Criteria` or `## Success Criteria`), and has an implementation section. Mirrors PlanAll's `has_sufficient_detail()`.

## Needs-input detection

Items failing `has_sufficient_detail()` are marked `needs_input` without invoking `/intake` (the interactive subprocess blocks in batch mode). Direct `_invoke_intake()` detects needs by non-zero exit, question patterns, or exceptions.

## Error handling

- `wl list` failure: empty list returned gracefully
- Auto-complete claim failure: logged as warning, marked `error`
- Insufficient detail items: marked `needs_input` (no recovery needed)
- Direct `/intake` failure: recovered to `stage=idea, status=open`
- All errors/recoveries captured in summary report

## Idempotence

Only processes items in `idea` stage. Already-processed items excluded. Safe to re-run.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--json` | off | JSON output |
| `--dry-run` | off | Simulate only |
| `--parent-id <id>` | None | Post summary as parent comment |
| `--max` | 0 | Max items (0 = all) |
| `--item-timeout` | 600 | Per-item timeout in seconds |
| `--verbose` | off | Verbose logging |

## Examples

```bash
python3 ./scripts/intakeall.py --json --max 5 --item-timeout 300
python3 ./scripts/intakeall.py --parent-id SA-0MQK9SWN6008DWVQ
```

## Scripts

- Runner: `./scripts/intakeall.py`
- Tests: `./tests/test_intakeall.py`

## Related skills

- `command/intake.md` — Per-item intake command
- `../planall/SKILL.md` — Sibling batch planning skill
- `../ralph/SKILL.md` — Auto-intake for individual items

> **Implementation notes:**
> - The `_invoke_intake()` previously invoked `/intake` via `pi run /intake <id>` which blocked indefinitely in batch mode. Fixed in SA-0MQRAMZ4V0056K14 (type-safety) and SA-0MQP33ID9004OR5M (JSON invocation). Batch flow now skips interactive subprocess.
> - Status claim was fixed in SA-0MQS18ZOI005ER2V to use status-only (`--status in_progress`), matching documented pattern.
