---
name: ralph
description: "Run an iterative implement→audit loop for a target work item. Ralph is a launcher/orchestrator, not the normal Worklog implementation workflow."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>` or `ralph status`.

When invoked as `ralph <work-item-id>`, do not perform general Worklog discovery or planning steps before launching the Ralph loop. Only inspect work items when the operator explicitly asks for diagnostics or when the launch fails and you need to debug the failure.

## Command invocation and ID detection

The skill accepts a work-item id provided inline in the user's command. Supported invocation forms include:

- `/ralph <WORKITEM>`
- `ralph <WORKITEM>`
- `run ralph <WORKITEM>`
- `ralph loop <WORKITEM>`

A work-item id is any short token matching the Worklog id pattern used in your environment (for example `WL-1234`, `CG-0MP12H40Q003Y7OU`, or an 8+ char identifier). When an id is present in the command the skill will use it and will not prompt for an id. If no id is detected, the skill will ask the operator to provide one or abort, except for `ralph status`, which is an intentional no-id exception.

## Behavior

1. Detect a work-item id in the invocation if present; otherwise ask the operator for an id or abort, except for `ralph status`, which intentionally runs without a work-item id.
2. For `ralph <work-item-id>`, immediately run the deterministic loop through the `skill/ralph/ralph` wrapper so the run starts under `nohup` and the launcher records the PID, start time, and log path needed by `ralph status`.
3. Do not create, claim, update, or reprioritize work items as part of the Ralph launcher itself. The wrapper/script owns the loop.
4. Use `ralph status` to inspect the current background run without needing the original work-item id.

For direct foreground debugging, run the script locally:

- Use `--child <id>` only when you explicitly want to focus Ralph on a single direct child work item while keeping the parent as context.
- Use `--debug-persist` when you need to save raw Pi payloads for `no_text_extracted` debugging.

Delegated `pi` and `wl` commands are logged before execution in both normal console output and `--json` output, so operators and automation can see the exact command Ralph ran.
If streamed `pi` output stops producing stdout and keeps the pipe open too long, Ralph will terminate the run with a clear stall error instead of hanging indefinitely.

## Pi subprocess cleanup at loop completion

When Ralph's implement→audit loop ends (whether by success, cancellation, max attempts, or producer-input-required), it runs a deterministic cleanup step for any lingering Pi subprocess:

1. **Graceful shutdown**: Sends SIGTERM to the Pi process and waits up to the configured grace period (default 5 seconds).
2. **Escalation**: If the process has not exited within the grace period, sends SIGKILL (via `process.kill()`) and waits up to 1 second for it to drain.
3. **Observability**: Every step is logged with distinct event names so operators can distinguish normal completion (`ralph.cleanup.pi.graceful_exit`) from forced termination (`ralph.cleanup.pi.forced_kill`) in the log output.

The cleanup is safe to call even if the process has already exited — it checks `process.poll()` before sending any signals.

### Per-phase model routing

Ralph supports phase-specific model selection for `intake`, `planning`, `implementation`, and `audit`.

- Source toggle: `--model-source <remote|local>` (default: `local`)
- Shorthand: `ralph <id> remote` or `ralph <id> local` (equivalent to `--model-source`)
- Per-phase CLI overrides:
  - `--model-intake`
  - `--model-planning`
  - `--model-implementation`
  - `--model-audit`
- Config supports `model_source` plus `model.<phase>` keys (nested object or dotted keys).
- No implicit remote↔local fallback is attempted by Ralph.
- Backward compatibility remains: when per-phase inputs are not used, Ralph continues the legacy single-model path (`--model` / string `model` config / `skill/ralph/assets/.ralph.json` defaults).
- Default per-phase models are shipped in `skill/ralph/assets/.ralph.json`. Values in that file are overridden by a `.ralph.json` in the current working directory, which in turn are overridden by CLI flags.

```bash
# Launch a background Ralph run from the skill installation.
# The wrapper handles nohup plus PID/start-time capture for status reporting.
# Preferred (skill-relative):
skill/ralph/ralph <work-item-id> --json

# Inspect the current background run (no work item id required):
skill/ralph/ralph status --json

# If you need to run the foreground loop directly for debugging:
# python3 skill/ralph/scripts/ralph_loop.py <work-item-id> --json
#
# To focus on a single direct child while keeping the parent for context:
# python3 skill/ralph/scripts/ralph_loop.py <parent-id> --child <child-id> --json

# If your skills are installed at a different location (for example a
# project-level skills directory), run the script using the full path to
# that skill directory instead, e.g.:
# python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id> --json
```

See `docs/ralph.md` and `ralph --help` for full details of the features available.

## Ralph Status

When the operator runs `ralph status`, keep the report brief and focused on essentials:

1. Check whether the PID is still active and report how long the run has been operating.
2. Review the logs since the last status update and summarize the work completed.
3. Count the work items and children in the Ralph scope, grouped by Worklog `status`, and report the totals plus deltas since the last status report.
4. Include any other essential information only if it materially helps the operator understand the current run.
5. Do not require a work-item id for status, and do not perform broader Worklog inspection unless the operator asks for it.

Keep any remembered values needed for status reporting, such as issue counts and the last log cursor, in the control-loop context. Do not persist them between runs.

