# Ralph orchestration loop

`ralph` is a deterministic implement→audit orchestration loop for a target Worklog item.

## Overview

Ralph drives an iterative cycle of:

1. **Implement (per child)** — if the target has children, Ralph delegates each child individually via `implement-single` (single-work-item runs still use `implement`).
2. **Compact on child transition** — after each implement pass, detect children that moved to `in_review` and invoke `/compact` once per transition before auditing.
3. **Audit** — run the `audit` skill after each child implementation and run a final parent-level audit once all children pass.
4. **Remediate** — if a child audit finds unmet or partial criteria, Ralph re-runs only that child before moving on.
5. **Repeat** until audits pass, max attempts are reached, the model reports no safe path without producer input, or the operator cancels.

Ralph is launched from the `skill/ralph/ralph` wrapper. The wrapper starts the deterministic loop in the background under `nohup`, writes runtime context under `.worklog/ralph/`, and exposes `ralph status` for live or post-exit inspection.

## Usage

```bash
# Launch a background Ralph run from the skill installation so it works
# regardless of the current working directory. Use the skill-installed
# path (expand ~ in shell):
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> [options]

# Use local models (default):
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id>

# Use remote models with shorthand syntax:
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> remote

# Use remote models with explicit flag:
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> --model-source remote

# Inspect the current run without the work item id:
/home/rgardler/.pi/agent/skills/ralph/ralph status --json

# If you need to run the foreground loop directly for debugging:
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <work-item-id> [options]

# If your skills are installed at a different location, run the script
# using the full path to that skill directory instead, e.g.:
# python3 /path/to/skills/ralph/scripts/ralph_loop.py <work-item-id> [options]
```

### Runtime files

The background launcher stores the current runtime context in `.worklog/ralph/current.json` and writes the live log to a per-run log file under the same directory. `ralph status` reads that context file to report the current state, active task, recent activity, and final summary.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--max-attempts` | 10 | Maximum number of implement→audit cycles before giving up. |
| `--check-cmd` | (none) | Build/test command(s) to run after a successful audit. Pytest commands are normalized to `pytest -q -r a --disable-warnings` by default, and package-manager test commands are normalized to quiet variants such as `npm --silent test`. Can be specified multiple times. |
| `--confirm-merge` | off | Execute `git fetch`, `git merge --ff-only`, `git push` after successful audit and checks. **Without this flag, no merge side effects occur.**  Note: This direct push to main may fail if server-side branch protection requiring pull requests is enabled. See [Merge Safety Model](#merge-safety-model) for PR-based alternatives. |
| `--cancel-file` | (none) | Path checked each attempt; if the file exists, the loop stops with status `cancelled`. |
| `--child` | (none) | Focus the loop on a single direct child work item. Ralph validates that the child belongs to the supplied target and then runs the loop against the child only. |
| `--debug-persist` | off | Persist raw Pi payloads to `/tmp/ralph-payloads/` when a streamed run produces no user-facing text. |
| `--quiet` | off | Suppress all console output and pi streaming; only print the final JSON result. |
| `--verbose` | off | Show detailed delegation commands, subprocess stdout/stderr, and raw audit output. |
| `--no-stream` | off | Don't stream pi subprocess output to console (use buffered capture). Progress logging still shown. |
| `--model` | `opencode-go/glm-5.1` | Legacy single-model override applied to all phases when per-phase mode is not enabled. |
| `--model-source` | `local` | Selects source-specific per-phase model defaults and source-mapped config values: `remote` or `local`. No automatic fallback between sources. |
| `--model-intake` | (none) | Override the intake phase model for this run. |
| `--model-planning` | (none) | Override the planning phase model for this run. |
| `--model-implementation` | (none) | Override the implementation phase model for this run. |
| `--model-audit` | (none) | Override the audit phase model for this run. |
| `--pi-bin` | `pi` | Path to the `pi` binary for delegating implement and audit. |
| `--wl-bin` | `wl` | Path to the `wl` binary for worklog operations. |
| `--no-autoplan` | off | Disable the auto-plan step for `intake_complete` items. When set, ralph proceeds directly to implementation without running effort-and-risk evaluation. |
| `--autoplan-effort-skip` | Extra Small, Small | T-shirt effort sizes that allow skipping `/plan`. Accepts multiple space-separated values. |
| `--autoplan-risk-skip` | Low | Risk levels that allow skipping `/plan`. Accepts multiple space-separated values. |
| `--fail-open` | off | Continue on delegated command failures (non-fatal) when possible. When set, ralph will log failures from delegated tools and continue unless the failing command's category is marked fatal (see `--fatal-cmd`). Default behaviour is fail-fast. |
| `--retry` | 0 | Number of additional retries for delegated commands on failure. Retries occur before deciding to fail or continue. |
| `--retry-delay` | 1.0 | Delay (seconds) between retry attempts. |
| `--fatal-cmd` | (none) | Repeatable. Command categories to treat as fatal even when `--fail-open` is set. Examples: `merge`, `pi`, `check`, `wl`, `effort_and_risk`. Default fatal categories are `merge`, `check`, and `pi`. |
| `--pi-stream-timeout` | (source-specific) | Override the pi stdout stream watchdog timeout in seconds. When not specified, defaults are read from `.ralph.json` (60s for local models, 300s for remote models). |

### Stream timeout configuration

Ralph monitors the stdout pipe of the delegated `pi` process with a watchdog timeout. If the stream becomes idle for longer than the timeout, Ralph terminates the subprocess with a clear `stream_stalled` error. This prevents the orchestration loop from hanging indefinitely when `pi` keeps the pipe open.

**Default timeouts:**

- **Local models**: 60 seconds — suitable for fast, low-latency local inference.
- **Remote models**: 300 seconds — accommodates higher network latency and variable response times from cloud providers.

**Configuration in `.ralph.json`:**

```json
{
  "timeout": {
    "pi_stream": {
      "remote": 300,
      "local": 60
    }
  }
}
```

A global numeric override (applies to both sources):

```json
{
  "timeout": {
    "pi_stream": 120
  }
}
```

**CLI override:**

```bash
# Set a custom timeout for this run (overrides config and source defaults)
ralph SA-1234 --pi-stream-timeout 180
```

The resolution order is: **CLI flag > config > source-specific default > global default**.

### Delegated command fail-open & retry

Ralph delegates many subprocess commands (worklog `wl` calls, `pi` runs, `git` operations, shell `bash` checks, and skill orchestrators such as the effort-and-risk orchestrator). Transient failures (network blips, temporary permission errors, flaky tooling) can cause the loop to abort. The new fail-open & retry flags allow opt-in, configurable behaviour to make automation more resilient.

Key flags

- `--fail-open` — opt-in; when set, ralph will log delegated command failures and continue the loop where safe instead of raising immediately. Default is fail-fast.
- `--retry N` — number of additional retry attempts for delegated commands on failure (default: `0`). Retries are attempted before deciding to fail or continue.
- `--retry-delay S` — delay in seconds between retry attempts (default: `1.0`).
- `--fatal-cmd` — repeatable; mark a command category as *fatal* even when `--fail-open` is set. Example categories: `merge`, `pi`, `check`, `wl`, `effort_and_risk`.

Behaviour

- Fail-fast by default: without `--fail-open`, ralph preserves previous behaviour and raises on delegated command failures.
- When `--fail-open` is enabled, ralph will only treat failures as non-fatal for categories *not* marked fatal. The default fatal categories are `merge`, `check`, and `pi` — these remain fatal to avoid unsafe merges and surface real problems.
- `wl` (worklog) calls are non-fatal by default to tolerate transient worklog or network errors; you can make them fatal via `--fatal-cmd wl`.
- The `effort_and_risk` orchestrator is treated specially: it is non-fatal by default and, on failure, Ralph defaults to invoking `/plan` (safety-first). This preserves existing autoplan behaviour.
- Retries: when a delegated command fails, ralph will re-run it up to `--retry` times (i.e., initial attempt + `--retry` additional attempts) with `--retry-delay` seconds between attempts. If retries are exhausted, the usual fail-open/fatal decision is applied.

Examples

- Continue on transient failures and retry twice with a 2-second delay:

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --fail-open --retry 2 --retry-delay 2
```

- Treat `wl` as fatal even when using `--fail-open`:

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --fail-open --fatal-cmd wl
```

Guidance

- Use `--fail-open` for long-running automation where occasional delegated command failures are expected (CI flakiness, occasional network hiccups). Keep `merge` and `check` fatal unless you explicitly want to allow merges to proceed even when checks fail.
- Use `--retry` conservatively (1-3) to avoid long delays; combine with a moderate `--retry-delay` for transient external errors.

Testing

- Unit and integration tests were added under `skill/ralph/tests/` to validate retry and fail-open semantics.

### Preconditions

- **Stage gate**: The target work item must be at stage `plan_complete`, `in_review`, `in_progress`, or `intake_complete`.
  - At `intake_complete`: ralph automatically runs the **auto-plan** decision (see Auto-Plan Decision section). If effort and risk are below thresholds, ralph proceeds directly to implementation. If effort or risk exceed thresholds, `/plan` is invoked first, then implementation continues. Auto-plan only runs on the first attempt; subsequent iterations proceed directly to implementation.
  - At `plan_complete`: ralph runs the full implement\u2192audit loop.
  - At `in_progress`: ralph accepts an `in_progress` work item as a valid entrypoint and will resume the implement→audit loop. Behavior is equivalent to `plan_complete` for orchestration purposes. Note that the auto-plan decision is not automatically run for `in_progress` targets (auto-plan remains limited to `intake_complete`).
  - At `in_review`: ralph **skips the first implement pass** and audits immediately. If audit passes, ralph proceeds to checks/merge without any implement step. If audit fails, ralph falls into the normal implement\u2192audit loop with remediation.
  - At any other stage: ralph exits with an error.
- **Scope**: Only the target item and its direct children are processed.
- **Child skip logic**: When iterating through children in per-child mode, Ralph decides whether to skip a child based on its stage and most recent audit result:
  - Children in terminal stages (`done`, `completed`, `closed`) are **always skipped**.
  - Children in `in_review` stage are **skipped only if** their most recent persisted audit result says "Ready to close: Yes".
  - Children in `in_review` whose most recent audit says "Ready to close: No" (or have no persisted audit) are **re-processed** (re-implemented and re-audited).
  - All other stages (e.g., `in_progress`, `plan_complete`) are processed normally.

## Agent Post-Launch Behavior

When an agent (Pi or other) invokes the Ralph skill via `ralph <work-item-id>`, the agent MUST follow this specific post-launch behavior to avoid unnecessary polling loops:

### Post-launch steps

1. **Launch**: Immediately run the Ralph wrapper in the background (this is handled by the skill invocation).
2. **Wait**: Wait exactly **20 seconds** once to allow Ralph to initialize and begin processing.
3. **Check status**: Run `skill/ralph/ralph status --json` one time to verify the loop is running.
4. **Report result**:
   - If Ralph is **running**: Confirm success and inform the operator they can use `ralph status` to monitor progress.
   - If Ralph has **stopped or failed**: Provide a **Root Cause Analysis (RCA)** using available log evidence from the status output.
5. **Stop polling**: Do NOT enter any polling loop. Let the operator decide when to check status next.

### Example agent workflow

```bash
# Agent launches Ralph
skill/ralph/ralph SA-12345

# Agent waits 20 seconds
sleep 20

# Agent checks status once
status_output=$(skill/ralph/ralph status --json)

# Agent evaluates and reports
if echo "$status_output" | grep -q '"state": "running"'; then
  echo "Ralph loop started successfully. Use 'ralph status' to monitor progress."
else
  echo "Ralph loop failed to start. Providing RCA based on logs..."
  # Extract and analyze log evidence from status output
fi
```

### Critical: No polling loops

Agents must NOT implement polling loops after launch (repeatedly sleeping and checking logs). This behavior wastes resources and creates noise. The operator will use `ralph status` to check progress as needed.

## Compaction trigger behavior

After every implement pass (including the first pass after auto-plan), Ralph snapshots child stages before implementation and compares them to the child stages immediately after implementation.

For each child where:

- previous stage != `in_review`
- new stage == `in_review`

Ralph invokes `/compact` once before continuing to audit.

Key semantics:

- `/compact` is invoked **without** an explicit work-item id; the compaction plugin derives context from the current session. With session-per-call invocations, each Pi call operates in its own session (identified by `--session-id`), so compaction is scoped to that session's context.
- `/compact` failures are **non-fatal**. Ralph logs a warning and continues with the loop.
- Compaction evidence is **logs only** (no worklog comments are persisted for compact output).

### Stage Check Expansion Fix

**Note**: A fix was implemented to expand the stage check logic in Ralph to handle a broader range of completed stages. Previously, Ralph only recognized `in_review` stage for per-child iteration scope. The fix expanded this to also include `done`, `completed`, and `closed` stages in the `_scope_in_review` method.

**Rationale**: This fix addresses CI failures where work items in terminal stages (`done`, `completed`, `closed`) were not properly handled in per-child iteration scenarios, causing the loop to fail with max attempts errors.

**Implementation**: The change was made in `skill/ralph/scripts/ralph_loop.py` around line 2396, expanding the allowed stages from only `in_review` to include `done`, `completed`, and `closed`:

```python
def _scope_in_review(self, scope_ids: Iterable[str]) -> bool:
    allowed = {"in_review", "done", "completed", "closed"}  # Expanded from only "in_review"
    # ... rest of method
```

### CI Runner Availability Fix

**Note**: Fixes were implemented to address `wl` CLI availability issues in CI environments that were causing `FileNotFoundError` during child iteration tests.

**Rationale**: The CI failures were caused by the `wl` command not being available in the runner environment, leading to `FileNotFoundError` when Ralph tried to execute worklog operations.

**Implementation**: The fixes were implemented in PRs #688 and #689 to ensure the `wl` CLI is properly available in CI runner environments, resolving the FileNotFoundError that was preventing child iteration tests from passing.

### Debug Logging Enhancements

**Note**: Debug logging was enhanced to help identify why per-child runs reach max attempts, particularly for audit parsing and unmet criteria detection.

**Rationale**: The original CI failures included scenarios where Ralph would reach max attempts without clear visibility into why the audit process was failing.

**Implementation**: Debug logging was added in PR #691 to provide better visibility into:

- Audit parsing processes
- Unmet criteria detection
- Per-child iteration progress
- Compact invocation and failure scenarios

This helps operators and developers understand why Ralph might be reaching max attempts and diagnose issues more effectively.

## Single Feature Branch for Child Iterations

When Ralph processes a parent work item with children, it creates a **single feature branch** that is shared across all child iterations. This ensures all changes from a Ralph run are consolidated on one branch, making review and integration straightforward.

### Branch Creation

- Ralph creates a feature branch named `wl-<parent-id>-<short-desc>` before iterating over children.
- If the branch already exists, Ralph checks it out rather than creating a new one.
- The branch is created from `origin/dev` when available, ensuring the latest integration point.

### Child Iteration on Shared Branch

- All child implementations execute on the shared feature branch.
- Children do **not** create new feature branches — they check out and use the parent's branch.
- Child iterations are serialized (one-at-a-time) on the shared branch to avoid concurrency hazards.

### Commit Traceability

Child commit messages include a `Related-Work: <child-id>` trailer to ensure traceability back to the child work item. Example commit message format:

```
SA-12345: Add authentication handler for API endpoints

Related-Work: SA-67890
```

This convention allows:

- Tracing which child work item each commit addresses
- Filtering commits by work item in git history
- Maintaining clear lineage between parent and child changes

### Graceful Degradation

If branch creation fails (e.g., due to git permissions), Ralph logs a warning and continues without branch sharing. This ensures the orchestration loop is not blocked by transient git issues.

## Configuration File

Ralph reads settings from a `.ralph.json` file in the current directory (or `ralph.config.json`). The file is a simple JSON object. Values from the config file are defaults; CLI flags take precedence.

### Per-phase model config

Ralph supports per-phase model routing for:

- `intake` (first implement pass when starting from `intake_complete`)
- `planning` (`/skill:plan`)
- `implementation` (normal implement passes)
- `audit` (`/skill:audit`)

Use `model_source` to choose which source-specific model to resolve (`remote` or `local`), and per-phase keys under `model`:

```json
{
  "model_source": "remote",
  "model": {
    "intake": {
      "remote": "Claude Opus 4.7",
      "local": "Llama-3.1 70B (Q4_K_M)"
    },
    "planning": {
      "remote": "GPT 5.5",
      "local": "Qwen 3.x 32B"
    },
    "implementation": {
      "remote": "Qwen 3.6 Plus",
      "local": "Qwen 32B"
    },
    "audit": {
      "remote": "Claude Opus 4.7",
      "local": "Llama-3.1 70B (Q4_K_M)"
    }
  },
  "max_attempts": 10
}
```

A copy/pasteable example is also available at `docs/ralph.example.config.json`.

Equivalent dotted keys are also supported (for example: `model.intake`, `model.planning`, `model.implementation`, `model.audit`, and optionally `model.remote.intake`/`model.local.intake`).

### Supported model keys

| Key | Type | Description |
|-----|------|-------------|
| `model_source` | string | `remote` or `local`. Defaults to `remote`. No implicit remote↔local fallback is attempted. |
| `model` | string | Legacy single-model config for all phases (backward compatibility path). |
| `model.intake` | string/object | Intake phase model. String applies to both sources; object can provide `{ "remote": "...", "local": "..." }`. |
| `model.planning` | string/object | Planning phase model. |
| `model.implementation` | string/object | Implementation phase model. |
| `model.audit` | string/object | Audit phase model. |
| `max_attempts` | integer | Default maximum implement→audit cycles. Overridden by `--max-attempts`. |

### Complexity-tier model configuration

Ralph supports **risk/effort-based model complexity tiers** so that different work items can use different model configurations automatically. This lets you use fast, cost-effective models for simple tasks while reserving stronger models for complex work.

#### Tier mapping

The complexity tier is determined by the work item's `effort` (t-shirt size) and `risk` fields:

| Tier | Mapping |
|------|---------|
| **Low** | Effort is `Extra Small` **or** `Small` **AND** Risk is `Low` |
| **Medium** | Effort is `Medium` **OR** Risk is `Medium` |
| **High** | Effort is `Large` **or** `Extra Large` **OR** Risk is `High` |

The thresholds are configurable via `complexity_tier` in `.ralph.json`. If a work item has no effort/risk values, or the values cannot be evaluated, the tier defaults to **Medium**.

#### Tiered config structure

Within each model source, you can define per-tier model mappings:

```json
{
  "model_source": "local",
  "model": {
    "remote": {
      "low": { "intake": "opencode-go/glm-5.1", "planning": "opencode-go/glm-5.1", "implementation": "opencode-go/glm-5.1", "audit": "opencode-go/glm-5.1" },
      "medium": { "intake": "opencode/claude-opus-4.7", "planning": "opencode/gpt-5.5", "implementation": "opencode-go/qwen3.6-plus", "audit": "opencode-go/glm-5.1" },
      "high": { "intake": "opencode/claude-opus-4.7", "planning": "opencode/gpt-5.5", "implementation": "opencode-go/qwen3.6-plus", "audit": "opencode-go/glm-5.1" }
    },
    "local": {
      "low": { "intake": "Proxy/qwen3", "planning": "Proxy/qwen3", "implementation": "Proxy/qwen3", "audit": "Proxy/qwen3" },
      "medium": { "intake": "Proxy/qwen3", "planning": "Proxy/qwen3", "implementation": "Proxy/qwen3", "audit": "Proxy/qwen3" },
      "high": { "intake": "Proxy/qwen3", "planning": "Proxy/qwen3", "implementation": "Proxy/qwen3", "audit": "Proxy/qwen3" }
    }
  },
  "complexity_tier": {
    "low": { "max_effort": "Small", "max_risk": "Low" },
    "high": { "min_effort": "Large", "min_risk": "High" }
  }
}
```

#### Per-child tier evaluation

When Ralph processes children in per-child mode, it evaluates the effort-and-risk skill (if not already computed) for each child and selects the appropriate tier for that child's phases. The tier is passed through to `pi` subprocess invocations for all phases.

#### Defaults and backwards compatibility

- **No tier specified:** When no complexity tier is active (e.g., during legacy usage or when effort/risk cannot be evaluated), Ralph defaults to the `medium` tier.
- **Flat per-phase keys (legacy):** If you have an older `.ralph.json` with flat `model.remote.intake` / `model.local.intake` etc. keys (without tier nesting), Ralph will continue to use those. You can migrate to tiered config at your own pace.
- **Mixed config:** You can mix tiers and flat keys in the same config; tiers take priority where defined.

### Resolution precedence

For each phase, Ralph resolves `--model` passed to `pi` in this order:

1. CLI phase override (`--model-intake`, `--model-planning`, `--model-implementation`, `--model-audit`)
2. Per-tier config (`model.<source>.<tier>.<phase>`) — only when a complexity tier is active
3. Per-phase config (`model.<phase>` or `model.<source>.<phase>`)
4. Legacy single model (`--model` CLI or string `model` config)
5. `DEFAULT_MODEL` (`opencode-go/glm-5.1`) when per-phase mode is not enabled
6. Canonical source-specific defaults (below) when per-phase mode is enabled but no explicit per-phase value is provided

Canonical defaults used by per-phase mode:

- intake: remote `Claude Opus 4.7`; local `Llama-3.1 70B (Q4_K_M)`
- planning: remote `GPT 5.5`; local `Qwen 3.x 32B`
- implementation: remote `Qwen 3.6 Plus`; local `Qwen 32B`
- audit: remote `Claude Opus 4.7`; local `Llama-3.1 70B (Q4_K_M)`

### Migration note

Existing single-model behavior is preserved when no per-phase config/flags are supplied: Ralph continues to use the legacy single model (`--model` / string `model` / `DEFAULT_MODEL`) for all phases.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success — audit passed, checks passed, merge offered |
| 2 | Error — precondition failure, command error, or producer-input-required stop |
| 3 | Cancelled — cancel file detected |
| 4 | Max attempts reached — loop exhausted without success |

## Merge Safety Model

Ralph will **never** merge or push without explicit operator confirmation:

- Without `--confirm-merge`: ralph reports success and notes that merge is available, but performs **no git operations**.
- With `--confirm-merge`: ralph executes `git fetch origin main`, `git merge --ff-only origin/main`, and `git push origin HEAD`.
- If any git step fails (e.g., permission denied, push rejected), ralph raises a clear error.

### Branch protection on main

The `--confirm-merge` direct-push approach will **fail** if server-side branch protection requiring pull requests is enabled on `main`. For repositories with branch protection, use one of these alternatives:

1. **Release merge script** (recommended): Use `scripts/release/merge-dev-to-main.sh` which creates a temporary release branch, opens a PR, waits for status checks, and merges via `gh pr merge`. See the [script documentation](../scripts/release/merge-dev-to-main.sh) for usage details.
2. **Manual PR workflow**: Manually create a PR from your feature branch to `main` using the `gh` CLI, wait for checks to pass, and merge via `gh pr merge --merge --delete-branch`.

The `--confirm-merge` flag is safe to use in repositories **without** branch protection on main, or when the operator has bypass permissions to push directly.

## Console Output

By default, ralph prints two kinds of output to the console:

1. **Structured progress messages** (logger at INFO level): lifecycle events like attempt start, audit result, merge decision, etc.
2. **Delegated command logs**: every delegated `pi` and `wl` command is logged before it runs. The console formatter shows the rendered command string, and `--json` output includes structured `cmd` and `argv` fields for machine-readable inspection.
3. **Pi subprocess streaming**: pi output is parsed per pi's JSON streaming protocol. Only `text_delta` events (the agent's actual user-facing response) are shown in real-time, printed additively. Thinking/reasoning, metadata, and structural events are suppressed for a clean, readable console.

This means during an implement or audit pass, you'll see the assistant's response appear token-by-token as `text_delta` events stream in — no thinking blocks, no JSON envelope, no session metadata. Each Pi invocation uses a unique session ID (e.g., `ralph-SA-123-implement-a1b2c3`) which is preserved for debugging, but not displayed in the streaming output.

The `text_delta` content is additive (each delta contains only the new text since the last delta), so there's no duplication.

Use `--quiet` to suppress all progress output and pi streaming — only the final JSON result is printed. Useful for scripted invocations.

Use `--no-stream` to keep progress logging but disable pi output streaming (output is still captured, just not echoed to the console).

When streaming is enabled, Ralph also watches for stdout inactivity and terminates a stalled `pi` subprocess with a clear error so the loop does not hang forever.

Use `--verbose` to see additional delegation details in addition to the default command logs:

- Raw JSON lines from pi (logged at DEBUG level)
- Subprocess output (first 1000 chars of stdout/stderr for check and merge commands)
- Full pi run prompts (logged under `prompt_full`)
- Pi run output (first 1000 chars)
- Raw audit output (first 1000 chars) and parsed criteria details
- Comment counts and work item stage/status from worklog commands

Typical `--verbose` output includes:

```
DEBUG ralph ralph.cmd.wl.show cmd=['wl', 'show', 'SA-1234', '--json', '--children']
DEBUG ralph ralph.cmd.wl.show id=SA-1234 stage=plan_complete status=open children=1
DEBUG ralph ralph.cmd.pi.run prompt_len=142
DEBUG ralph ralph.cmd.pi.run prompt_full=
implement SA-1234
Target scope includes direct children only: SA-5678.
Continue until scope items are in_review, but do not merge.
DEBUG ralph ralph.cmd.pi.run stdout_len=2048 stdout_start=Audit report...
DEBUG ralph ralph.loop.audit.raw_output target=SA-1234 attempt=1 len=2048 output_start=Ready to close: No...
DEBUG ralph ralph.loop.audit.parsed target=SA-1234 attempt=1 ready=False criteria_count=3 unmet=2
```

## Observability

## Pi process cleanup at loop completion

When Ralph's implement→audit loop ends (regardless of outcome — success, cancellation, max attempts, or producer-input-required), a deterministic cleanup step ensures no orphaned Pi subprocess remains:

1. **Check**: If no Pi process is tracked, or it has already exited, cleanup returns immediately.
2. **Graceful shutdown**: Sends SIGTERM to the Pi process and waits up to `pi_cleanup_timeout` seconds (configurable, default 5.0) for it to exit.
3. **Escalation**: If the process has not exited within the grace period, sends SIGKILL via `process.kill()` and waits up to 1 second for it to drain.
4. **Safe to retry**: If the process was already gone (e.g., `ProcessLookupError`), cleanup handles it gracefully without raising.

### Observability events

Ralph emits structured log events at key lifecycle points using the `ralph` Python logger:

| `ralph.cleanup.pi.already_exited` | INFO | pid, returncode |
| `ralph.cleanup.pi.sending_sigterm` | INFO | pid, timeout |
| `ralph.cleanup.pi.already_gone` | INFO | pid |
| `ralph.cleanup.pi.sigterm_failed` | WARNING | pid, error |
| `ralph.cleanup.pi.graceful_exit` | INFO | pid, returncode |
| `ralph.cleanup.pi.graceful_timeout` | WARNING | pid, escalating_to_sigkill |
| `ralph.cleanup.pi.forced_kill` | WARNING | pid, returncode |
| `ralph.cleanup.pi.sigkill_wait_timeout` | WARNING | pid |
| `ralph.cleanup.pi.kill_failed` | WARNING | pid, error |

| Event | Level | Data |
|-------|-------|------|
| `ralph.loop.start` | INFO | target, scope, max_attempts |
| `ralph.loop.attempt.start` | INFO | target, attempt number |
| `ralph.loop.audit.start` | INFO | target, attempt |
| `ralph.loop.audit.complete` | INFO | target, attempt, ready, unmet count |
| `ralph.loop.remediate` | INFO | target, attempt, unmet_count |
| `ralph.loop.checks.start` | INFO | target |
| `ralph.loop.merge` | INFO | target, confirm flag |
| `ralph.loop.cancelled` | INFO | target, attempt |
| `ralph.loop.no_safe_path` | WARNING | target, attempt, reason |
| `ralph.loop.max_attempts` | WARNING | target |
| `ralph.compact.transition` | INFO | target, child, attempt, `compact.invocations` |
| `ralph.compact.failed` | WARNING | target, child, attempt, `compact.failures`, error |
| `ralph.compact.metrics` | INFO | target, attempt, cumulative `compact.invocations`, cumulative `compact.failures` |

The final JSON result now includes a `compact` object:

```json
{
  "compact": {
    "invocations": 1,
    "failures": 0
  }
}
```

## Session management

### Session-per-call with unique IDs

Each Pi invocation within Ralph uses a unique session ID (rather than the previous ephemeral session approach). This preserves session history for debugging and audit purposes while maintaining isolation between orchestration steps.

Session IDs follow the format `ralph-{work_item_id}-{phase}-{short_uuid}`:

- `ralph-` prefix: identifies Ralph-generated sessions for cleanup targeting
- `{work_item_id}`: the first work item ID in scope (e.g., `SA-1234`)
- `{phase}`: the orchestration phase (`intake`, `planning`, `implementation`, `audit`)
- `{short_uuid}`: 8-character hex string from `uuid.uuid4().hex[:8]`

Example session ID: `ralph-SA-1234-implementation-a1b2c3d4`

Each call produces a unique session ID, so sessions can be inspected with `pi --session <id>` or by resuming with `/resume` in a Pi interactive session.

### Session pruning and retention

Ralph automatically prunes old Ralph-generated Pi sessions after each loop completion (success or failure). Only sessions with the `ralph-` prefix are targeted; non-Ralph Pi sessions are never removed.

#### Retention period

- **Default**: 112 days
- **Config**: `session.retention_days` key in `.ralph.json`
- **Override**: `--session-retention-days` CLI flag (highest precedence)

The retention period controls how long Ralph-generated sessions are kept before being automatically deleted. Files older than the configured number of days are removed at each loop exit.

#### Session directory

The Pi session directory defaults to `~/.pi/agent/sessions/` and can be overridden via:

- `PI_CODING_AGENT_SESSION_DIR` environment variable
- `--session-dir` CLI flag

#### Observability

| Log event | Level | Data |
|-----------|-------|------|
| `ralph.session.prune.completed` | INFO | pruned count, bytes reclaimed, retention_days |
| `ralph.session.prune.directory_not_found` | DEBUG | path |
| `ralph.session.prune.os_error` | DEBUG | path |
| `ralph.session.prune.failed` | WARNING | error message |

## Pi output validation

Ralph validates the output returned from every Pi delegation (implement and audit phases) to detect silent failures:

- **Empty output**: If Pi returns no user-facing text (e.g., all assistant messages have empty content), Ralph raises a clear `RalphError` instead of treating it as success.
- **Input echo**: If Pi echoes back the input prompt (e.g., when a local model endpoint is unavailable and falls back to returning the prompt), Ralph detects the echo and raises a `RalphError` with a message identifying the issue.
- **Raw skill content**: If Pi returns raw SKILL.md file content instead of execution results, Ralph detects this pattern and raises a `RalphError`.
- **Short implementation output**: For implementation phases, if the output is very short and contains no structured actions, Ralph treats it as invalid.
- **Audit markers**: For audit phases, Ralph checks for expected audit markers (e.g., "Ready to close:") in short outputs.

This prevents the common failure mode where a down or misconfigured model provider causes Pi to return the user prompt as the assistant response, leading Ralph to falsely believe work was completed.

## No safe path stop condition

If the implement step returns a structured `no_safe_path` response, Ralph stops immediately with `status: producer_input_required`, includes the model-provided reason in the JSON result and warning logs, and skips the audit step for that attempt. This keeps the loop non-interactive when the model cannot continue safely without producer input.

## Auto-Plan Decision

The auto-plan decision logic has been extracted from Ralph's inline code into
a **shared module**, canonically bundled with the plan skill at
`skill/plan/plan_helpers.py`. This module is the single source of truth for
effort/risk threshold decisions, used by:

- **Ralph** — delegates decision logic to `command.plan_helpers` functions
  (which load from the canonical `skill/plan/plan_helpers.py`), while keeping
  its own I/O infrastructure (runner, retry, fail-open) for backward
  compatibility.
- **`/plan` command** — runs `python3 skill/plan/plan_helpers.py plan-if-needed <id>`
  (the legacy `command/plan_helpers.py` also works as a delegation wrapper)
  as a pre-check before the full planning decomposition.
- **PlanAll** — benefits automatically since it shells out to `/skill:plan <id>`.

When a work item is at stage `intake_complete`, ralph automatically runs an **auto-plan** decision before the first implementation pass:

1. **Check idempotence**: If the work item already has non-empty `effort` and `risk` fields, or an existing `autoplan-decision-hash:` comment, ralph skips the effort-and-risk computation and uses the stored values for the threshold check.
2. **Evaluate effort and risk**: Otherwise, ralph calls the `effort-and-risk` skill (`orchestrate_estimate.py`) to compute the effort t-shirt size and risk level.
3. **Threshold decision**:
   - If effort is **Extra Small** or **Small** **AND** risk is **Low**, ralph skips planning and proceeds directly to implementation.
   - If effort or risk exceed these thresholds, ralph invokes `/plan <id>` to create a plan before implementation. Ralph runs the plan via the Pi agent runtime (the `pi` binary) by invoking the plan skill (e.g. `/skill:plan <id>`), so planning executes inside the agent framework with the configured model and runtime semantics. This is distinct from engine-level `opencode run "/plan <id>"` dispatch.
   - If the effort-and-risk skill fails or returns ambiguous data, ralph defaults to running `/plan` (safety-first).
4. **Post decision comment**: ralph posts a human-readable comment on the work item documenting the auto-plan decision (effort, risk, outcome). This comment is idempotent \u2014 re-running ralph will not create duplicate comments.

### Auto-plan observability

| Event | Level | Data |
|-------|-------|------|
| `ralph.autoplan.start` | INFO | target |
| `ralph.autoplan.already_computed` | INFO | target, effort, risk |
| `ralph.autoplan.effort_risk.start` | INFO | target |
| `ralph.autoplan.effort_risk.complete` | INFO | target, t-shirt, risk level |
| `ralph.autoplan.effort_risk.failed` | WARNING | target, return code |
| `ralph.autoplan.result` | INFO | target, t-shirt, risk level, do_plan |
| `ralph.autoplan.plan_invoked` | INFO | target |
| `ralph.autoplan.plan_complete` | INFO | target |
| `ralph.autoplan.skip_plan` | INFO | target |
| `ralph.autoplan.cached_decision` | INFO | target, effort, risk, do_plan |

### Disabling auto-plan

Use `--no-autoplan` to skip the auto-plan step entirely and proceed directly to implementation for `intake_complete` items:

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --no-autoplan
```

### Customizing thresholds

Override the default thresholds for skipping `/plan`:

```bash
# Allow Medium effort to skip /plan (in addition to Extra Small and Small)
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --autoplan-effort-skip Extra Small Small Medium

# Allow Low and Medium risk to skip /plan
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --autoplan-risk-skip Low Medium
```

## Audit Processing

When ralph receives audit output from the `/audit` skill, it processes it in two stages:

### Sanitization

The `/audit` skill may produce user-facing preamble text before the structured audit report (e.g., explanatory notes, formatting markers). Ralph sanitises the raw audit output by extracting only the structured block beginning with `Ready to close: Yes` or `Ready to close: No`. Any content before this header is stripped. This ensures that `wl update --audit-text` always receives text whose first non-empty line is the structured header, regardless of preamble content.

If no `Ready to close:` header is found in the audit output, ralph raises a `RalphError` with a short excerpt of the raw output to help the operator triage the issue.

### Deduplication

When ralph re-runs audit (e.g., after a failed attempt) and produces the same structured audit text (same content hash), it skips both `wl update --audit-text` and the AMPA comment to avoid overwriting or duplicating the persisted result. A changed audit (different content hash) is persisted as a revised entry.

## Idempotence

- Audit results are deduplicated by content hash. Re-running ralph with identical audit output will not overwrite the persisted audit text or create duplicate AMPA comments.
- Changed audit content (different hash) calls both `wl update --audit-text` and appends a new AMPA comment (clear revision, not a duplicate).
- Auto-plan decision comments are deduplicated by a deterministic hash of the effort/risk values. Re-running ralph when effort and risk are unchanged will not create duplicate auto-plan comments.
- When effort and risk fields are already set on the work item, ralph skips the effort-and-risk computation and uses the stored values for the threshold decision.

## Examples

### Basic run (no merge)

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --max-attempts 5
```

### Run with build checks and merge

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --check-cmd "pytest -q -r a --disable-warnings" --confirm-merge
```

Quiet package-manager test in a sibling repo:

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --check-cmd "npm --silent test" --confirm-merge
```

### Run with cancellation support

```bash
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py SA-1234 --cancel-file /tmp/ralph-cancel
# To cancel: touch /tmp/ralph-cancel
```

## Signal & Notification System

Ralph writes a JSON signal file and optionally sends a Discord webhook notification when major events occur during the loop lifecycle. See [docs/ralph-signal.md](ralph-signal.md) for:

- Signal file format (JSON schema, event types, file behaviour)
- Discord webhook payload format and configuration
- Pi integration specification for consuming signals
- Signal file path configuration via `.ralph.json`

### Configuration Example

The signal file path and Discord webhook URL are configured in `.ralph.json`:

```json
{
  "signal": {
    "file_path": ".ralph/event.pending"
  },
  "discord": {
    "webhook_url": "https://discord.com/api/webhooks/your-webhook-url"
  }
}
```

When the webhook URL is omitted or empty, no webhook notifications are sent.
