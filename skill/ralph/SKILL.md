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

Start-of-iteration audit skipping: When the target is already at stage `in_review`, Ralph will normally run a start-of-iteration audit. To avoid redundant audits, Ralph will skip invoking the audit skill at the start of the iteration if the most recent `# AMPA Audit Result` comment (across the target and all recursive descendants) has a `createdAt` timestamp that is equal to or newer than the most-recent `updatedAt` timestamp in the same scope. In that case Ralph will read the persisted audit from the work item and proceed without re-running the audit skill.

Accepting `in_progress`: Ralph now accepts work items in stage `in_progress` as a valid entrypoint. Invoking Ralph on an `in_progress` item resumes the implement→audit loop and behaves like a `plan_complete` entrypoint (i.e., it will perform the full implement→audit cycle). Note: the auto-plan decision is still only applied to `intake_complete` items and is not automatically invoked for `in_progress` targets.


```bash
# Run the ralph orchestrator from the skill installation so it works
# regardless of the current working directory. Use the skill-installed
# path (expand ~ in shell):
# Preferred: use the executable wrapper that is safe to invoke from any CWD
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> --json

# Alternatively, run the script directly from the installed skill directory:
# python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id> --json

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

When you supply `--check-cmd`, use quiet pytest mode by default:
`pytest -q -r a --disable-warnings`

For deeper debugging, the shared test-runner helper can add `--showlocals`.

See `docs/ralph.md` for full details.

