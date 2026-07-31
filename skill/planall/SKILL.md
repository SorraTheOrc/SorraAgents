---
name: planall
description: "Automated batch planning for intake_complete work items. Discovers all items in intake_complete status and invokes /plan for each sequentially, producing a summary report."
---

# PlanAll

Use this skill when asked to run batch planning on all `intake_complete` work items. It processes each item sequentially, detects items requiring producer input, and provides a summary report.

## Behavior

1. Query `wl list --stage intake_complete --needs-producer-review no --json` for eligible items (items flagged `needsProducerReview` are awaiting producer input and are excluded to avoid re-processing loops)
2. Claim each (`wl update <id> --status in_progress`) and invoke `/plan`
3. Detect producer-input need (non-zero exit, question patterns, exceptions)
4. Continue despite errors/input-needs
5. Produce summary report

## Invocation

```
/skill:planall [--json] [--parent-id <id>] [--max N] [--item-timeout N]
python3 ./scripts/planall.py [same flags]
pi run /planall
```

## Output

### Markdown summary

```
# PlanAll Summary
**Total**: 5 | **Planned**: 3 | **Needs input**: 1 | **Errors**: 1 | **Remaining**: 2
- **SA-ITEM-001**: `planned`
- **SA-ITEM-003**: `needs_input`
- **SA-ITEM-004**: `error`
```

### JSON output (`--json`)

```json
{
  "total": 5, "planned": 3, "needs_input": 1, "errors": 1, "remaining": 2,
  "items": [{"id": "SA-ITEM-001", "outcome": "planned"}]
}
```

## Producer-input detection

Detected by: non-zero exit from `/plan`, question-like patterns, or exceptions. Flagged items are skipped (not retried) and **recovered to a valid terminal state**: `status=open`, `stage=intake_complete`, `needsProducerReview=yes`. The producer-review flag keeps the item visible (`open`) while preventing the batch from re-processing it on the next run. Producers clear the flag via `wl reviewed <id> no` after triaging.

## Error handling

- `wl list` failure: empty list returned gracefully
- Claim failure: logged as warning, marked `error`
- `/plan` failure: recovers (`stage`→`intake_complete`, `status`→`open`), continues
- Producer-input need: recovers (`stage`→`intake_complete`, `status`→`open`, `needsProducerReview=yes`), continues
- Timeout: same recovery as failure
- All errors captured in summary report

## Status safety invariant

No matter how an item's processing ends (planned, needs input, error, or
interruption), the item is **never left in `in_progress`** when the batch
completes. Every terminal path resets to a valid status:

- **planned** → managed by the `/plan` skill itself (`open`/`plan_complete`)
- **needs_input** → `open` + `intake_complete` + `needsProducerReview=yes`
- **error** → `open` + `intake_complete`
- **signal abort** → `open` + `intake_complete`

## Signal handling

SIGINT/SIGTERM handlers recover the in-progress item (stage→`intake_complete`, status→`open`), restore handlers, and exit with signal code.

## Idempotence

Only processes items in `intake_complete` stage. Already-planned items excluded. Safe to re-run.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--json` | off | JSON output |
| `--parent-id` | None | Post summary as parent comment |
| `--max` | 0 | Max items (0 = all) |
| `--item-timeout` | 600 | Per-item timeout in seconds |
| `--verbose` | off | Verbose logging |

## Examples

```bash
python3 ./scripts/planall.py --json --max 5 --item-timeout 300
python3 ./scripts/planall.py --parent-id SA-0MQA6ECEU003GUKH
```

## Scripts

- Runner: `./scripts/planall.py`
- Tests: `./tests/test_planall.py`

## Related skills

- `../plan/SKILL.md` — Per-item planning command

