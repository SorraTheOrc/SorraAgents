# AMPA triage-audit flow

This document explains the AMPA scheduler's triage-audit post-processing. It is triggered for scheduled commands with `command_id="wl-triage-audit"` or `command_type="triage-audit"`.

References:
- `ampa/audit_poller.py` (candidate detection, cooldown filtering, selection)
- `ampa/triage_audit.py` (audit execution, Discord notifications, comment posting, auto-completion)
- `ampa/scheduler.py` (routing)
- `tests/test_audit_poller.py` (poller unit tests)
- `tests/test_triage_audit.py` (integration and behavior tests)
- `command/audit.md` (the `/audit` command itself)

## Architecture

The triage-audit flow is split into two modules:

- **`audit_poller.py`** — Detection layer. Queries for `in_review` items, applies store-based cooldown filtering, selects the oldest eligible candidate, persists the `last_audit_at` timestamp, and hands off the selected candidate to the audit handler.
- **`triage_audit.py`** — Execution layer (`TriageAuditRunner`). Receives a pre-selected work item dict and performs audit execution, structured report extraction, Discord notifications, Worklog comment posting, and auto-completion checks.

The scheduler's `start_command()` routes `wl-triage-audit` commands through `poll_and_handoff()`, passing a handler adapter that delegates to `TriageAuditRunner.run()`.

## End-to-end flow

1. **Detect and select a candidate (audit poller)**
   - Run `wl list --stage in_review --json` and normalize the response shape (handles list/dict formats, deduplicates by ID).
   - Read the `last_audit_at_by_item` dict from the scheduler store.
   - Filter out items whose last audit is within the cooldown window (`audit_cooldown_hours`).
   - Sort remaining candidates by `updated_at` ascending (oldest first; items with no timestamp sorted first).
   - Select the first (oldest) eligible candidate.
   - Persist `last_audit_at_by_item[selected_id] = now` to the store **before** calling the handler (prevents re-selection during long-running audits).
   - Hand off the selected work item dict to the handler.

2. **Run the audit (TriageAuditRunner)**
   - Execute `opencode run "/audit <work_id>"`.
   - Capture stdout and stderr into a single audit output string.
   - The audit agent produces a **structured report** bounded by delimiter markers (see [Structured audit output](#structured-audit-output) below).

3. **Extract the structured report**
   - Parse the raw output looking for `--- AUDIT REPORT START ---` and `--- AUDIT REPORT END ---` delimiter lines.
   - Extract the content between these markers as the structured audit report.
   - If the markers are missing (e.g., the audit agent failed or produced legacy output), fall back to using the full raw output and log a warning.

4. **Post a Discord summary (optional)**
   - If `AMPA_DISCORD_BOT_TOKEN` is set, extract the `## Summary` section from the structured report.
   - If no `## Summary` heading is found, fall back to the legacy regex extraction (`_extract_summary()`).
   - If neither produces a summary, fall back to a short line with the work id, title, and exit code.
   - Send a Discord message capped to ~1000 chars.

5. **Post structured audit report to Worklog**
    - Create a Worklog comment with a standard heading: `# AMPA Audit Result`.
    - The comment body contains the extracted structured report (not the full raw output).
    - If the report is short enough, embed it directly in the comment.
    - If the report is too large, write it to a temp file and post a comment that references the file path.
    - Temp files used for comments are removed after posting.

6. **Auto-complete check (optional)**
   The scheduler will attempt to move the work item to `completed` and `in_review` when:
   - The audit output indicates a merged PR (PR URL or "PR merged" token), and
   - There are no open child work items, or the audit explicitly says it is ready to close.
   - The update command includes `--needs-producer-review true` to flag the item for producer review.

    If a GitHub PR URL is found, the scheduler can verify merge status with `gh pr view`.

## Cooldown logic

Cooldown is determined solely from the scheduler store's `last_audit_at_by_item` dict. For each candidate, the poller looks up `last_audit_at_by_item[item_id]`. If `(now - last_audit_at) < audit_cooldown_hours`, the item is skipped. Items with no store entry are always eligible.

The `last_audit_at` timestamp is written to the store **before** the handler is called (step 1 above). This means that if the audit handler fails or takes longer than the polling interval, the item won't be re-selected until the cooldown expires.

> **Note:** Previous versions used comment scanning (`wl comment list` per candidate) to determine cooldown. This was removed in favor of store-only cooldown, which eliminates per-candidate shell calls and simplifies the implementation. If the store is lost or reset, all items become immediately eligible for re-audit.

## Structured audit output

The audit agent produces a structured report bounded by delimiter markers. The report follows this format:

```
--- AUDIT REPORT START ---
## Summary

<concise 2-4 sentence summary>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <text>    | met/unmet/partial | <file:line — note> |

## Children Status

### <child-title> (<child-id>) — <status>/<stage>

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <text>    | met/unmet/partial | <file:line — note> |

## Recommendation

<closing recommendation>
--- AUDIT REPORT END ---
```

The `_extract_audit_report()` function in `triage_audit.py` extracts content between these markers. If the markers are missing, the full raw output is used with a warning logged.

## Output locations and formats

- **Worklog comment**: A comment is added with heading `# AMPA Audit Result` containing the extracted structured report (between the delimiter markers).
- **Discord summary**: The `## Summary` section is extracted from the structured report and sent to Discord. If no `## Summary` heading is found, the legacy regex fallback (`_extract_summary()`) is used. The notification includes extra fields:
  - **Work Item**: The work item ID (always present).
  - **GitHub**: A link to the corresponding GitHub issue (`https://github.com/<owner>/<repo>/issues/<number>`), included when `githubIssueNumber` is available on the work item and `githubRepo` is configured in `.worklog/config.yaml`.
  - **Summary**: The extracted summary text.
  - **PR**: A link to the associated pull request, included when a PR URL is found in the work item description or comments.

## Configuration and metadata

Environment variables:
- `AMPA_DISCORD_BOT_TOKEN`: If set, Discord summary messages are sent via the bot.
- `AMPA_VERIFY_PR_WITH_GH`: If set to `1|true|yes`, verifies PR merge status with `gh` when a PR URL appears in output. If unset, defaults to enabled.

Per-command metadata (from the scheduler command spec):
- `audit_cooldown_hours` (default: 6): Minimum hours between audits for the same work item. A single value applies to all items regardless of status.
- `truncate_chars` (default: 65536): Max chars to inline in Worklog comments before writing to a temp file.
- `verify_pr_with_gh` (default: true): Overrides `AMPA_VERIFY_PR_WITH_GH` when present.

## Module responsibilities

| Concern | Module | Function/Class |
|---------|--------|----------------|
| Candidate query | `audit_poller.py` | `_query_candidates()` |
| Cooldown filtering | `audit_poller.py` | `_filter_by_cooldown()` |
| Candidate selection | `audit_poller.py` | `_select_candidate()` |
| Store persistence | `audit_poller.py` | `poll_and_handoff()` |
| Handoff protocol | `audit_poller.py` | `AuditHandoffHandler` protocol |
| Audit invocation | `triage_audit.py` | `TriageAuditRunner.run()` |
| Report extraction | `triage_audit.py` | `_extract_audit_report()` |
| Discord notifications | `triage_audit.py` | via `notifications` module |
| Comment posting | `triage_audit.py` | `TriageAuditRunner.run()` |
| Auto-completion | `triage_audit.py` | `TriageAuditRunner.run()` |
| Scheduler routing | `scheduler.py` | `Scheduler.start_command()` |

## Notes

- The triage-audit flow is post-processing only. The scheduler still records the command run normally before executing this logic.
- The `/audit` command is a separate command definition; triage-audit invokes it. The audit agent is instructed to produce structured output with delimiter markers (see `skill/audit/SKILL.md`).
- The structured report format enables reliable extraction of the report content and summary, replacing earlier regex-based heuristics.
- `TriageAuditRunner.run()` requires a `work_item` keyword argument (a dict with at least `"id"` and `"title"` keys). Calling it without one raises `TypeError`. This ensures the old call convention (which included inline detection) cannot be used accidentally.
