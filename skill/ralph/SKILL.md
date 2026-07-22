---
name: ralph
description: "Run an iterative implement→audit loop for a target work item. Ralph is a launcher/orchestrator, not the normal Worklog implementation workflow."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>` or `ralph status`.

When invoked as `ralph <work-item-id>`, do not perform general Worklog discovery or planning steps before launching the Ralph loop. Only inspect work items when the operator explicitly asks for diagnostics or when the launch fails and you need to debug the failure.

## Command invocation and ID detection

Detect a work-item id inline. Supported forms:

- `/ralph <WORKITEM>`, `ralph <WORKITEM>`, `run ralph <WORKITEM>`, `ralph loop <WORKITEM>`

A work-item id matches the Worklog pattern (e.g., `WL-1234` or `SA-0MP12...`). If found, use it without prompting. If not found, ask the operator or abort — except `ralph status` (intentional no-id exception).

## Behavior

1. Detect a work-item id in the invocation; otherwise ask or abort (except `ralph status` which runs without one).
2. For `ralph <work-item-id>`, launch via `./ralph` wrapper so the run starts under `nohup` with PID/start-time/log-path capture.
3. Do NOT create, claim, update, or reprioritize work items from the launcher — the wrapper owns the loop.
4. Use `ralph status` to inspect the background run without needing the work-item id.
5. **Post-launch:** Wait 20s once, check status once via `./ralph status --json`. If running, report success. If stopped/failed, provide RCA from status output. Do NOT poll.

For direct foreground debugging: use `--child <id>` to focus on a single child, `--debug-persist` to save raw Pi payloads for debugging.

Delegated `pi` and `wl` commands are logged before execution in both normal console output and `--json` output, so operators and automation can see the exact command Ralph ran.
If streamed `pi` output stops producing stdout and keeps the pipe open too long, Ralph will terminate the run with a clear stall error instead of hanging indefinitely.

## Pi subprocess cleanup at loop completion

When Ralph's implement→audit loop ends (whether by success, cancellation, max attempts, or producer-input-required), it runs a deterministic cleanup step for any lingering Pi subprocess:

1. **Graceful shutdown**: SIGTERM + wait (default 5s).
2. **Escalation**: SIGKILL if not exited within grace period + 1s drain.
3. **Observability**: Distinct event names (`ralph.cleanup.pi.graceful_exit` vs `ralph.cleanup.pi.forced_kill`).

Safe to call even if the process has already exited (checks `process.poll()` first).

### Worktree for child iterations

Ralph creates a single worktree for all child iterations of a parent work item. All children share it (serialized one-at-a-time):

```bash
git worktree add --track -b wl-<parent-id>-<short-slug> .worklog/worktrees/wl-<parent-id>-<short-slug> dev
# ... work happens ...
git worktree remove .worklog/worktrees/wl-<parent-id>-<short-slug>
git worktree prune
```

See [[concepts/git-worktree-best-practices-for-agent-workflows]] and [AGENTS.md](../../AGENTS.md) for the full lifecycle.

### Per-phase model routing

Supports phase-specific models for `intake`, `planning`, `implementation`, `audit`.

- Source toggle: `--model-source <remote|local>` (default: `local`); shorthand `ralph <id> remote|local`
- Per-phase overrides: `--model-intake`, `--model-planning`, `--model-implementation`, `--model-audit`
- Config: `model_source` + `model.<phase>` keys (nested object or dotted keys).
- No implicit remote↔local fallback.
- When per-phase inputs are absent, falls back to legacy single-model path (`--model` / `./assets/.ralph.json` defaults).
- Override order: `./assets/.ralph.json` < `.ralph.json` (CWD) < CLI flags.

```bash
# Background run (preferred):
./ralph <work-item-id> --json

# Inspect background run:
./ralph status --json

# Foreground debugging:
# python3 ./scripts/ralph_loop.py <work-item-id> --json
# python3 ./scripts/ralph_loop.py <parent-id> --child <child-id> --json
```

See `docs/ralph.md` and `ralph --help` for full details.

## Architecture: Shared Auto-Plan Decision Logic

The auto-plan decision logic is extracted into a shared module at ``../plan/plan_helpers.py`` (canonical) with legacy delegation at ``command/plan_helpers.py``.

Shared by:

- **Ralph** — delegates to ``command.plan_helpers`` (maintains own I/O infrastructure for backward compat).
- **`/skill:plan`** — invokes ``python3 ../plan/plan_helpers.py plan-if-needed <id>``.
- **PlanAll** — shells out to ``/skill:plan <id>``.

Key exports:

| Function / Constant | Purpose |
|---------------------|---------|
| `make_autoplan_decision()` | Top-level orchestrator returning `(do_plan, stage)` |
| `resolve_complexity_tier()` | Resolve low/medium/high from effort+risk |
| `is_effort_risk_computed()` | Idempotence check |
| `run_effort_and_risk()` | Invoke the effort-and-risk orchestrator |
| `append_autoplan_decision_comment()` | Idempotent decision comment posting |
| `plan_if_needed()` | CLI entry point returning JSON `{decision, effort, risk}` |
| `DEFAULT_AUTOPLAN_EFFORT_SKIP` | Default effort threshold: `{Extra Small, Small}` |
| `DEFAULT_AUTOPLAN_RISK_SKIP` | Default risk threshold: `{Low}` |

See `docs/ralph.md` for the full auto-plan decision flow.

## Scripts

- Launcher: `./ralph` (preferred — records PID/start-time for background runs)
- Foreground loop: `./scripts/ralph_loop.py`
- Shared autoplan: `../plan/plan_helpers.py` (canonical)
- Helpers: `./scripts/ralph_control.py`, `./scripts/structured_response.py`

```bash
./ralph SA-0MPYMFZXO0004ZU4 --json          # background
python3 ./scripts/ralph_loop.py SA-0MPYMFZXO0004ZU4 --json  # foreground
./ralph status --json                       # inspect
```

## Ralph Status

When the operator runs `ralph status`, output the script's structured markdown report **verbatim** without reinterpretation or reformatting.

```bash
./ralph status        # human-readable markdown
./ralph status --json # programmatic
```

Report sections:

1. **Header**: State, PID, target work-item id
2. **Active Task**: Current child being processed (if any)
3. **Status Counts**: Work items grouped by status with deltas
4. **Recent Activity**: Last 20 log lines
5. **Exit Code** (if stopped)
6. **Final Summary** (if available)

**Critical:** Forward the script's output directly. Do NOT add summaries, interpretations, or reformatting. Do NOT use phrases like "I have reviewed the logs" or ask for a work-item id for status.
