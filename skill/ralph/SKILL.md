---
name: ralph
description: "Run an iterative implement→audit loop for a target work item until scope reaches in_review and audit passes."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>`.

## Command invocation and ID detection

The skill accepts a work-item id provided inline in the user's command. Supported invocation forms include:

- `/ralph <WORKITEM>`
- `ralph <WORKITEM>`
- `run ralph <WORKITEM>`
- `ralph loop <WORKITEM>`

A work-item id is any short token matching the Worklog id pattern used in your environment (for example `WL-1234`, `CG-0MP12H40Q003Y7OU`, or an 8+ char identifier). When an id is present in the command the skill will use it and will not prompt for an id. If no id is detected the skill will ask the operator to provide one (or permission to create one).

## Behavior

1. Detect a work-item id in the invocation if present; otherwise ask the operator for an id (or permission to create one).
2. Run deterministic script locally:

```bash
python skill/ralph/scripts/ralph_loop.py <work-item-id> --json
```

## Auto-Plan Decision

When the target work item is at stage `intake_complete`, ralph automatically runs an **auto-plan** decision before the first implementation pass:

1. **Evaluate effort and risk**: ralph calls the `effort-and-risk` skill to compute the effort t-shirt size and risk level.
2. **Threshold check**:
   - If effort is **Extra Small** or **Small** AND risk is **Low** → skip `/plan` and proceed directly to implementation.
   - If effort or risk exceed these thresholds → invoke `/plan <id>` before implementation.
   - If the effort-and-risk skill fails → default to running `/plan` (safety-first).
3. **Idempotence**: If effort/risk are already computed or a decision comment exists, ralph skips re-computation and uses the stored values.
4. **Decision comment**: ralph posts a human-readable comment documenting the auto-plan decision.

Use `--no-autoplan` to disable this step and proceed directly to implementation.
Use `--autoplan-effort-skip` and `--autoplan-risk-skip` to customize the thresholds.

See `docs/ralph.md` for full details.

