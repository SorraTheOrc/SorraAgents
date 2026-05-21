---
name: ralph
description: "Run an iterative implement→audit loop for a target work item until scope reaches in_review and audit passes."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>` or `ralph status`.

## Command invocation and ID detection

The skill accepts a work-item id provided inline in the user's command. Supported invocation forms include:

- `/ralph <WORKITEM>`
- `ralph <WORKITEM>`
- `run ralph <WORKITEM>`
- `ralph loop <WORKITEM>`

A work-item id is any short token matching the Worklog id pattern used in your environment (for example `WL-1234`, `CG-0MP12H40Q003Y7OU`, or an 8+ char identifier). When an id is present in the command the skill will use it and will not prompt for an id. If no id is detected, the skill will ask the operator to provide one or abort, except for `ralph status`, which is an intentional no-id exception.

## Behavior

1. Detect a work-item id in the invocation if present; otherwise ask the operator for an id or abort, except for `ralph status`, which intentionally runs without a work-item id.
2. Run the deterministic loop in the background under `nohup` so the caller is free to keep working.
3. Use `ralph status` to inspect the current background run without needing the original work-item id.

For direct foreground debugging, run the script locally:

- Use `--child <id>` when you need to focus Ralph on a single direct child work item while keeping the parent as context.
- Use `--debug-persist` when you need to save raw Pi payloads for `no_text_extracted` debugging.

Delegated `pi` and `wl` commands are logged before execution in both normal console output and `--json` output, so operators and automation can see the exact command Ralph ran.


```bash
# Run the ralph orchestrator from the skill installation so it works
# regardless of the current working directory. Use the skill-installed
# path (expand ~ in shell):
# Preferred: use the executable wrapper that is safe to invoke from any CWD
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> --json

# Inspect the current background run (no work item id required):
/home/rgardler/.pi/agent/skills/ralph/ralph status --json

# If you need to run the foreground loop directly for debugging:
# python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id> --json
#
# To focus on a single direct child while keeping the parent for context:
# python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <parent-id> --child <child-id> --json

# If your skills are installed at a different location (for example a
# project-level skills directory), run the script using the full path to
# that skill directory instead, e.g.:
# python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id> --json
```

## Auto-Plan Decision

When the target work item is at stage `intake_complete`, ralph automatically runs an **auto-plan** decision before the first implementation pass:

1. **Evaluate effort and risk**: ralph calls the `effort-and-risk` skill to compute the effort t-shirt size and risk level.
2. **Threshold check**:
   - If effort is **Extra Small** or **Small** AND risk is **Low** → skip `/plan` and proceed directly to implementation.
   - If effort or risk exceed these thresholds → invoke `/plan <id>` before implementation. Ralph invokes `/plan` via the Pi agent runtime (using the `pi` binary and `/skill:plan <id>`), so planning runs inside the agent framework with the configured model. This differs from engine-level `opencode run "/plan <id>"` dispatch.
   - If the effort-and-risk skill fails → default to running `/plan` (safety-first).
3. **Idempotence**: If effort/risk are already computed or a decision comment exists, ralph skips re-computation and uses the stored values.
4. **Decision comment**: ralph posts a human-readable comment documenting the auto-plan decision.

Use `--no-autoplan` to disable this step and proceed directly to implementation.
Use `--autoplan-effort-skip` and `--autoplan-risk-skip` to customize the thresholds.

When you supply `--check-cmd`, use quiet test mode by default:
`pytest -q -r a --disable-warnings`

Non-pytest test runners should be invoked in quiet form, for example `npm --silent test`.

For deeper debugging, the shared test-runner helper can add `--showlocals`.

See `docs/ralph.md` for full details.

