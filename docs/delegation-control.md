# Controlling AMPA Delegation

This document explains how to prevent the Automated PM Agent (AMPA) from delegating a specific work item, and how AMPA reports that decision.

When AMPA runs its delegation flow it considers candidates returned by `wl next` and acts only on items whose workflow *stage* is one of:

- `idea`
- `intake_complete`
- `plan_complete`

If you want to stop a particular work item from being delegated there are three supported, auditable approaches (ordered by recommended usage):

1) Add a `do-not-delegate` tag (recommended)

- Use: `wl tag add <WORK_ID> do-not-delegate`
- AMPA checks candidates for the tag (case-insensitive) and will skip the item when present.
- When a candidate is skipped for this reason AMPA logs an INFO line such as:

  Delegation skipping candidate SA-12345 (Title): marked do-not-delegate

- This is auditable: the tag remains on the work item and appears in `wl show` and WL history.

2) Set an unsupported stage (quick manual stop)

- Change the work item's `stage` to anything other than the actionable stages above (for example `backlog` or `closed`).
- Examples:
  - Interactive: `wl edit <WORK_ID>` and set `stage` to `backlog`.
  - Non-interactive (if supported): `wl update <WORK_ID> --stage backlog`

3) Set per-item metadata to block delegation

- Add `do_not_delegate` (or `no_delegation`) to the work-item metadata and set it truthy (`true`, `1`, etc.).
- Example payload: `{"do_not_delegate": true}` (how to supply depends on your WL client).

How AMPA evaluates the signal

- The scheduler uses the helper `_is_do_not_delegate(candidate)` which checks, in order:
  1. `tags` (list or comma-separated string) for `do-not-delegate` or `do_not_delegate`.
  2. `metadata` / `meta` dictionary for `do_not_delegate` or `no_delegation` truthy values.
  3. explicit `do_not_delegate` boolean/string field on the candidate.
- If the function returns `True` the candidate is skipped and AMPA continues to the next candidate.

Logging & Discord

- When a candidate is skipped due to the tag, AMPA logs at INFO level (see above). This keeps the decision visible but non-fatal in logs.
- Unsupported stages are logged at ERROR and reported to Discord (AMPA will continue trying later candidates).
- When AMPA dispatches a delegation it posts a follow-up delegation report to the configured Discord bot channel summarizing the post-dispatch state.

Recommended practice

1. Prefer the `do-not-delegate` tag for explicit, auditable per-item control.
2. Use stage changes for quick one-off stops when editing the item is acceptable.
3. If you need stricter enforcement (e.g. notification when delegation is attempted), ask to enable automatic WL comments when AMPA skips a tagged item — this can be added.

If you want me to: (a) add a WL comment whenever AMPA skips an item for `do-not-delegate`, or (b) create a runbook snippet with copy/paste commands for triage engineers, tell me which and I'll add it.

## Delegation watchdog / timeout

AMPA enforces a configurable timeout on every delegated `opencode run` process to
prevent the scheduler from being stuck with `running=true` indefinitely.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DELEGATION_TIMEOUT_SECONDS` | 3600 | Maximum seconds a delegated `opencode run` may run before being terminated. Supercedes the legacy `AMPA_DELEGATION_OPENCODE_TIMEOUT` variable. |
| `AMPA_DELEGATION_OPENCODE_TIMEOUT` | *(see above)* | Legacy alias for `DELEGATION_TIMEOUT_SECONDS`. Still honoured for backward compatibility. |
| `AMPA_CMD_TIMEOUT_SECONDS` | 3600 | Global default timeout for all scheduled commands (used when neither delegation-specific variable is set). |

### Termination sequence

When the timeout expires the scheduler:

1. Sends **SIGTERM** to the entire process group (the child shell and all its children) and waits up to 5 seconds for graceful shutdown.
2. If the process group is still alive after the grace period, sends **SIGKILL**.
3. Records `exit_code=124` in the run history and clears the `running` flag so the command is eligible to run again on the next poll.
4. Logs a `WARNING` message with the command ID and timeout value, and sends a Discord error notification when configured.

The `running` flag is always cleared via `Scheduler._record_run` whether the process exits normally, crashes, or times out.

