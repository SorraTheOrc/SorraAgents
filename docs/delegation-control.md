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
