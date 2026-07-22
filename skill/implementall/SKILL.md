---
name: implementall
description: "Automated batch implementation for plan_complete work items. Discovers all items in plan_complete stage and runs /skill:implement for each sequentially, detecting producer-input needs, and producing a summary report."
---

# ImplementAll

Use this skill when asked to run batch implementation on all `plan_complete` work items. It processes each item sequentially, detects items requiring producer input, and provides a summary report.

## Behavior

1. Query `wl list --stage plan_complete --status open --json` for eligible items
2. Claim each item (`wl update <id> --status in_progress --stage in_progress`) and run `/skill:implement <id>`
3. Detect producer-input need (non-zero exit, question patterns, exceptions)
4. On error, recover (reset status to `open`) and record outcome
5. Continue processing remaining items despite errors
6. Produce summary report with per-item outcomes

## Invocation

```
/skill:implementall [--json] [--parent-id <id>] [--max N] [--dry-run] [--item-timeout N]
python3 ./scripts/implementall.py [same flags]
pi run /skill:implementall
```

## Output

### Markdown summary

```
# ImplementAll Summary
**Total**: 5 | **Implemented**: 3 | **Needs input**: 1 | **Errors**: 1 | **Remaining**: 0

- **SA-ITEM-001**: `implemented`
- **SA-ITEM-004**: `needs_input`
- **SA-ITEM-005**: `error` — Implement failed (rc=1): timeout exceeded | Recovery: reset_status_to_open_with_stage_plan_complete ✓
```

### JSON output (`--json`)

```json
{
  "total": 5, "implemented": 3, "needs_input": 1, "errors": 1,
  "items": [
    {"id": "SA-ITEM-001", "outcome": "implemented"},
    {"id": "SA-ITEM-005", "outcome": "error", "error_detail": "...", "recovery": {"action": "reset_status_to_open_with_stage_plan_complete", "success": true}}
  ]
}
```

## Producer-input detection

Detected by: non-zero exit from `/skill:implement`, question-like patterns in output (`? (yes/no)`, "What should"), or exceptions. Flagged items are skipped (not retried).

## Error handling

- `wl list` failure: returns empty list gracefully
- Claim failure: logs warning, marks as `error`
- Implement failure: resets status to `open` and stage to `plan_complete`; continues
- Question-pattern output: marks as `needs_input` without recovery (preserves context)
- Error outcomes trigger a **Script Execution Failure Notice** wrapping via `./scripts/failure_notice.py`

## Signal handling

SIGINT/SIGTERM handlers recover the in-progress item (stage→`plan_complete`, status→`open`), restore handlers, and exit with signal code. Prevents stuck `in_progress` items.

## Idempotence

Only processes items in `plan_complete` + `open` status. Already-implemented items are excluded. Safe to re-run.

## CLI flags

| Flag | Description |
|------|-------------|
| `--json` | JSON output |
| `--dry-run` | Simulate only |
| `--parent-id <id>` | Post summary as parent comment |
| `--max N` | Max items (0 = all) |
| `--item-timeout N` | Per-item timeout in seconds (default 600) |
| `--verbose` | Verbose logging |

## Examples

```bash
python3 ./scripts/implementall.py
python3 ./scripts/implementall.py --json --max 5 --item-timeout 300
python3 ./scripts/implementall.py --parent-id SA-0MQO6YMZ3006N5MG
```

## Scripts

- Runner: `./scripts/implementall.py`
- Tests: `./tests/test_implementall.py`

## Related skills

- `../implement/SKILL.md` — Invoked per-item
- `../planall/SKILL.md`, `../intakeall/SKILL.md` — Sibling batch skills
- `../ralph/SKILL.md` — Auto-implementation for individual items
