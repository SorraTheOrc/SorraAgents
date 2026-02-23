# AMPA triage-audit flow

This document explains the AMPA scheduler's triage-audit post-processing. It is triggered for scheduled commands with `command_id="wl-triage-audit"` or `command_type="triage-audit"`.

References:
- `ampa/scheduler.py` (implementation)
- `tests/test_triage_audit.py` (behavior tests)
- `command/audit.md` (the `/audit` command itself)

## End-to-end flow

1. **Select a candidate work item**
   - Run `wl in_progress --json` and normalize the response shape.
   - Build a candidate list sorted by least-recently-updated (oldest first).
   - Filter out items that have a recent audit comment (cooldown window).

2. **Run the audit**
   - Execute `opencode run "/audit <work_id>"`.
   - Capture stdout and stderr into a single audit output string.
   - The audit agent produces a **structured report** bounded by delimiter markers (see [Structured audit output](#structured-audit-output) below).

3. **Extract the structured report**
   - Parse the raw output looking for `--- AUDIT REPORT START ---` and `--- AUDIT REPORT END ---` delimiter lines.
   - Extract the content between these markers as the structured audit report.
   - If the markers are missing (e.g., the audit agent failed or produced legacy output), fall back to using the full raw output and log a warning.

4. **Post a Discord summary (optional)**
   - If `AMPA_DISCORD_WEBHOOK` is set, extract the `## Summary` section from the structured report.
   - If no `## Summary` heading is found, fall back to the legacy regex extraction (`_extract_summary()`).
   - If neither produces a summary, fall back to a short line with the work id, title, and exit code.
   - Send a Discord message capped to ~1000 chars.

5. **Post structured audit report to Worklog**
    - Create a Worklog comment with a standard heading: `# AMPA Audit Result`.
    - The comment body contains the extracted structured report (not the full raw output).
    - If the report is short enough, embed it directly in the comment.
    - If the report is too large, write it to a temp file and post a comment that references the file path.
    - Temp files used for comments are removed after posting.
    - If `audit_only` is enabled, delegation is skipped.

6. **Auto-complete check (optional)**
   The scheduler will attempt to move the work item to `completed` and `in_review` when:
   - The audit output indicates a merged PR (PR URL or "PR merged" token), and
   - There are no open child work items, or the audit explicitly says it is ready to close.

    If a GitHub PR URL is found, the scheduler can verify merge status with `gh pr view`.
    - Skipped when `audit_only` is enabled.

## Cooldown logic

To avoid repeatedly auditing the same item, triage-audit inspects prior Worklog comments on each item. Any comment containing `# AMPA Audit Result` is treated as a prior audit. If the most recent audit is within the cooldown window, the item is skipped.

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

- **Worklog comment**: A comment is added with heading `# AMPA Audit Result` containing the extracted structured report (between the delimiter markers). This heading is also used for cooldown detection.
- **Discord summary**: The `## Summary` section is extracted from the structured report and sent to Discord. If no `## Summary` heading is found, the legacy regex fallback (`_extract_summary()`) is used. The message format:

```
# /audit <work_id> <work-item-title>

<summary text>
```

## Configuration and metadata

Environment variables:
- `AMPA_DISCORD_WEBHOOK`: If set, Discord summary messages are sent.
- `AMPA_VERIFY_PR_WITH_GH`: If set to `1|true|yes`, verifies PR merge status with `gh` when a PR URL appears in output. If unset, defaults to enabled.

Per-command metadata (from the scheduler command spec):
- `audit_cooldown_hours` (default: 6): Minimum hours between audits for the same work item.
- `truncate_chars` (default: 65536): Max chars to inline in Worklog comments before writing to a temp file.
- `verify_pr_with_gh` (default: true): Overrides `AMPA_VERIFY_PR_WITH_GH` when present.
- `audit_only` (default: false): When true, do not update work item stage/status and skip delegation.

## Notes

- The triage-audit flow is post-processing only. The scheduler still records the command run normally before executing this logic.
- The `/audit` command is a separate command definition; triage-audit invokes it. The audit agent is instructed to produce structured output with delimiter markers (see `skill/audit/SKILL.md`).
- The structured report format enables reliable extraction of the report content and summary, replacing earlier regex-based heuristics.
