# Ralph orchestration loop

`ralph` is a deterministic implement→audit orchestration loop for a target Worklog item.

## Overview

Ralph drives an iterative cycle of:

1. **Implement** — delegate implementation of the target work item (+ direct children) via the `implement` skill.
2. **Compact on child transition** — after each implement pass, detect children that moved to `in_review` and invoke `/compact` once per transition before auditing.
3. **Audit** — run the `audit` skill and persist structured results.
4. **Remediate** — if audit finds unmet or partial criteria, feed those into the next implement pass.
5. **Repeat** until audit passes, max attempts are reached, the model reports no safe path without producer input, or the operator cancels.

Ralph is launched from the `skill/ralph/ralph` wrapper. The wrapper starts the deterministic loop in the background under `nohup`, writes runtime context under `.worklog/ralph/`, and exposes `ralph status` for live or post-exit inspection.

## Usage

```bash
# Launch a background Ralph run from the skill installation so it works
# regardless of the current working directory. Use the skill-installed
# path (expand ~ in shell):
/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id> [options]

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
| `--confirm-merge` | off | Execute `git fetch`, `git merge --ff-only`, `git push` after successful audit and checks. **Without this flag, no merge side effects occur.** |
| `--cancel-file` | (none) | Path checked each attempt; if the file exists, the loop stops with status `cancelled`. |
| `--child` | (none) | Focus the loop on a single direct child work item. Ralph validates that the child belongs to the supplied target and then runs the loop against the child only. |
| `--debug-persist` | off | Persist raw Pi payloads to `/tmp/ralph-payloads/` when a streamed run produces no user-facing text. |
| `--quiet` | off | Suppress all console output and pi streaming; only print the final JSON result. |
| `--verbose` | off | Show detailed delegation commands, subprocess stdout/stderr, and raw audit output. |
| `--no-stream` | off | Don't stream pi subprocess output to console (use buffered capture). Progress logging still shown. |
| `--model` | `opencode-go/glm-5.1` | Legacy single-model override applied to all phases when per-phase mode is not enabled. |
| `--model-source` | `remote` | Selects source-specific per-phase model defaults and source-mapped config values: `remote` or `local`. No automatic fallback between sources. |
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

## Compaction trigger behavior

After every implement pass (including the first pass after auto-plan), Ralph snapshots child stages before implementation and compares them to the child stages immediately after implementation.

For each child where:

- previous stage != `in_review`
- new stage == `in_review`

Ralph invokes `/compact` once before continuing to audit.

Key semantics:

- `/compact` is invoked **without** an explicit work-item id; the compaction plugin derives context from the current session.
- `/compact` failures are **non-fatal**. Ralph logs a warning and continues with the loop.
- Compaction evidence is **logs only** (no worklog comments are persisted for compact output).

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

### Resolution precedence

For each phase, Ralph resolves `--model` passed to `pi` in this order:

1. CLI phase override (`--model-intake`, `--model-planning`, `--model-implementation`, `--model-audit`)
2. Per-phase config (`model.<phase>`)
3. Legacy single model (`--model` CLI or string `model` config)
4. `DEFAULT_MODEL` (`opencode-go/glm-5.1`) when per-phase mode is not enabled
5. Canonical source-specific defaults (below) when per-phase mode is enabled but no explicit per-phase value is provided

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

## Console Output

By default, ralph prints two kinds of output to the console:

1. **Structured progress messages** (logger at INFO level): lifecycle events like attempt start, audit result, merge decision, etc.
2. **Delegated command logs**: every delegated `pi` and `wl` command is logged before it runs. The console formatter shows the rendered command string, and `--json` output includes structured `cmd` and `argv` fields for machine-readable inspection.
3. **Pi subprocess streaming**: pi output is parsed per pi's JSON streaming protocol. Only `text_delta` events (the agent's actual user-facing response) are shown in real-time, printed additively. Thinking/reasoning, metadata, and structural events are suppressed for a clean, readable console.

This means during an implement or audit pass, you'll see the assistant's response appear token-by-token as `text_delta` events stream in — no thinking blocks, no JSON envelope, no session metadata.

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

Ralph emits structured log events at key lifecycle points using the `ralph` Python logger:

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

## No safe path stop condition

If the implement step returns a structured `no_safe_path` response, Ralph stops immediately with `status: producer_input_required`, includes the model-provided reason in the JSON result and warning logs, and skips the audit step for that attempt. This keeps the loop non-interactive when the model cannot continue safely without producer input.

## Auto-Plan Decision

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
