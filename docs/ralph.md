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
| `--pi-bin` | `pi` | Path to the `pi` binary for delegating implement and audit. |
| `--wl-bin` | `wl` | Path to the `wl` binary for worklog operations. |

### Preconditions

- **Stage gate**: The target work item must be at stage `plan_complete`. If not, ralph exits with an error.
- **Scope**: Only the target item and its direct children are processed.

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