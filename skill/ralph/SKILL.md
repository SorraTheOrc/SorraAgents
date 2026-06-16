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
5. After launching the background Ralph loop, the agent MUST follow this **post-launch behavior**:
   - Wait exactly **20 seconds** (once, not in a loop) to allow Ralph to initialize.
   - Check the Ralph status **one time only** using `skill/ralph/ralph status --json`.
   - If Ralph is running: Report the loop started successfully and inform the operator they can use `ralph status` to monitor progress.
   - If Ralph has stopped or failed: Provide a **Root Cause Analysis (RCA)** using available log evidence from the status output.
   - **Do NOT** enter any polling loop — let the operator decide when to check status next.

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

### Single feature branch for child iterations

When Ralph processes a parent work item with children, it creates a single feature branch at the start of the run and all child iterations reuse that branch. This ensures:

- All changes from child iterations are consolidated on one branch
- Branch names follow the canonical pattern: `wl-<parent-id>-<short-desc>`
- Child implementations are serialized (one-at-a-time) on the shared branch
- Commits are traceable to child work-item IDs via commit messages

The branch is created once before the first child iteration and passed to all subsequent child implementations via the `parent_branch` parameter.

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

## Architecture: Shared Auto-Plan Decision Logic

The auto-plan decision logic (effort/risk threshold checks) has been extracted
from Ralph's inline code into a **shared module** at:

- `command/plan_helpers.py`

This module is the single source of truth for autoplan decisions, shared by:

- **Ralph** (`skill/ralph/scripts/ralph_loop.py`) — delegates decision logic to
  `command.plan_helpers` functions while keeping its own I/O infrastructure
  (runner, retry, fail-open) for backward compatibility.
- **`/plan` command** (`command/plan.md`) — runs `python3 command/plan_helpers.py
  plan-if-needed <id>` as a pre-check before the full planning decomposition.
- **PlanAll** — benefits automatically since it shells out to `/plan <id>`.

Key functions provided by `command/plan_helpers.py`:

| Function / Constant | Purpose |
|---------------------|---------|
| `make_autoplan_decision()` | Top-level orchestrator returning `(do_plan, stage)` |
| `resolve_complexity_tier()` | Resolve low/medium/high from effort+risk |
| `is_effort_risk_computed()` | Idempotence check (pure function) |
| `run_effort_and_risk()` | Invoke the effort-and-risk orchestrator |
| `append_autoplan_decision_comment()` | Idempotent decision comment posting |
| `plan_if_needed()` | CLI entry point returning JSON `{decision, effort, risk}` |
| `DEFAULT_AUTOPLAN_EFFORT_SKIP` | Default threshold: `{Extra Small, Small}` |
| `DEFAULT_AUTOPLAN_RISK_SKIP` | Default threshold: `{Low}` |

See `docs/ralph.md` for the full auto-plan decision flow.

## Scripts (canonical runner & modules)

- Launcher wrapper: `skill/ralph/ralph` (preferred wrapper that records PID/start-time and handles background runs)
- Foreground loop: `skill/ralph/scripts/ralph_loop.py` (python3)
- Shared autoplan module: `command/plan_helpers.py`
- Helpers and control: `skill/ralph/scripts/ralph_control.py`, `skill/ralph/scripts/structured_response.py`

Example (documentation):

```bash
# Start a background Ralph run for work item SA-0MPYMFZXO0004ZU4
skill/ralph/ralph SA-0MPYMFZXO0004ZU4 --json

# For direct debugging (foreground)
python3 skill/ralph/scripts/ralph_loop.py SA-0MPYMFZXO0004ZU4 --json

# Inspect status (no work item id required)
skill/ralph/ralph status --json
```

## Ralph Status

When the operator runs `ralph status`, the script produces a **structured markdown report** that must be emitted **directly to the operator without reinterpretation or reformatting**. Do NOT summarize, rephrase, or re-interpret the output.

### How to run

```bash
# Human-readable markdown output (recommended):
skill/ralph/ralph status

# JSON output for programmatic use:
skill/ralph/ralph status --json
```

### What the script reports

The `ralph status` command produces a markdown report with the following consistent sections:

1. **Header**: State (running/stopped), PID, and target work-item id
2. **Active Task**: The current child work item being processed (if any)
3. **Status Counts**: A table showing work item counts grouped by Worklog `status`, with deltas since the last status check
4. **Recent Activity**: Up to the last 20 log lines from the Ralph run
5. **Exit Code**: If the run has stopped, the exit code
6. **Final Summary**: Status and summary from the loop's final result (if available)

### Critical instructions

- **Output the script's markdown report verbatim**. Do NOT add your own summaries, interpretations, or reformatting.
- The script's `format_status()` function produces the canonical markdown output. Forward it directly.
- Do NOT use phrases like "I have reviewed the logs" or "Summary of work completed" – the markdown report itself is the summary.
- Keep any remembered values needed for status reporting (log cursor, status counts) in the control-loop context. Do not persist them between runs.
- Do not require a work-item id for status, and do not perform broader Worklog inspection unless the operator asks for it.

### Example output

```
# Ralph Status

**State**: `running` | **PID**: `12345` | **Target**: `SA-0MPYMFZXO0004ZU4`

**Active Task**: `SA-0MPYMFZXO0004ZU5`

## Status Counts

| Status | Count | Delta |
|--------|-------|-------|
| `completed` | 3 | +1 |
| `open` | 5 | -1 |

## Recent Activity

- child_focus parent=SA-0MPYMFZXO0004ZU4 child=SA-0MPYMFZXO0004ZU5
- implementing work item SA-0MPYMFZXO0004ZU5

**Exit Code**: `0`
```

Keep any remembered values needed for status reporting, such as issue counts and the last log cursor, in the control-loop context. Do not persist them between runs.
