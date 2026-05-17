# Ralph orchestration loop

`ralph` is a deterministic implement→audit orchestration loop for a target Worklog item.

## Overview

Ralph drives an iterative cycle of:

1. **Implement** — delegate implementation of the target work item (+ direct children) via the `implement` skill.
2. **Audit** — run the `audit` skill and persist structured results.
3. **Remediate** — if audit finds unmet or partial criteria, feed those into the next implement pass.
4. **Repeat** until audit passes, max attempts are reached, or the operator cancels.

## Usage

```bash
python skill/ralph/scripts/ralph_loop.py <work-item-id> [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--max-attempts` | 10 | Maximum number of implement→audit cycles before giving up. |
| `--check-cmd` | (none) | Build/test command(s) to run after a successful audit. Can be specified multiple times. |
| `--confirm-merge` | off | Execute `git fetch`, `git merge --ff-only`, `git push` after successful audit and checks. **Without this flag, no merge side effects occur.** |
| `--cancel-file` | (none) | Path checked each attempt; if the file exists, the loop stops with status `cancelled`. |
| `--quiet` | off | Suppress all console output and pi streaming; only print the final JSON result. |
| `--verbose` | off | Show detailed delegation commands, subprocess stdout/stderr, and raw audit output. |
| `--no-stream` | off | Don't stream pi subprocess output to console (use buffered capture). Progress logging still shown. |
| `--model` | `opencode-go/glm-5.1` | Model ID to pass to `pi run --model`. Can also be set in `.ralph.json`. |
| `--pi-bin` | `pi` | Path to the `pi` binary for delegating implement and audit. |
| `--wl-bin` | `wl` | Path to the `wl` binary for worklog operations. |

### Preconditions

- **Stage gate**: The target work item must be at stage `plan_complete` or `in_review`.
  - At `plan_complete`: ralph runs the full implement→audit loop.
  - At `in_review`: ralph **skips the first implement pass** and audits immediately. If audit passes, ralph proceeds to checks/merge without any implement step. If audit fails, ralph falls into the normal implement→audit loop with remediation.
  - At any other stage: ralph exits with an error.
- **Scope**: Only the target item and its direct children are processed.

## Configuration File

Ralph reads settings from a `.ralph.json` file in the current directory (or `ralph.config.json`). The file is a simple JSON object. Values from the config file are used as defaults; CLI flags take precedence.

```json
{
    "model": "opencode-go/glm-5.1",
    "max_attempts": 10
}
```

The file supports these keys:

| Key | Type | Description |
|-----|------|-------------|
| `model` | string | Model ID passed to `pi -p --mode json --model <model>`. Overrides the default. |
| `max_attempts` | integer | Default maximum implement→audit cycles. Overridden by `--max-attempts` CLI flag. |

A config key like `"model"` sets the default model used for all `pi run` commands. Command-line `--model` overrides it.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success — audit passed, checks passed, merge offered |
| 2 | Error — precondition failure or command error |
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
2. **Pi subprocess streaming**: pi output is parsed as JSON. Only the essential text content (assistant messages, tool results) is shown in real-time. Metadata events, tool-use envelopes, and empty deltas are suppressed for a clean, readable console.

This means during an implement or audit pass, you'll see the assistant's actual responses appear line by line — not the underlying JSON protocol.

Use `--quiet` to suppress all progress output and pi streaming — only the final JSON result is printed. Useful for scripted invocations.

Use `--no-stream` to keep progress logging but disable pi output streaming (output is still captured, just not echoed to the console).

Use `--verbose` to see detailed delegation information in addition to streaming:

- Every `wl`, `pi`, `git`, and `bash` command before it runs
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
| `ralph.loop.max_attempts` | WARNING | target |

## Idempotence

- Audit comments are deduplicated by content hash. Re-running ralph with identical audit output will not create duplicate AMPA comments.
- Changed audit content appends a new comment (clear revision, not a duplicate).

## Examples

### Basic run (no merge)

```bash
python skill/ralph/scripts/ralph_loop.py SA-1234 --max-attempts 5
```

### Run with build checks and merge

```bash
python skill/ralph/scripts/ralph_loop.py SA-1234 --check-cmd "pytest -q" --confirm-merge
```

### Run with cancellation support

```bash
python skill/ralph/scripts/ralph_loop.py SA-1234 --cancel-file /tmp/ralph-cancel
# To cancel: touch /tmp/ralph-cancel
```