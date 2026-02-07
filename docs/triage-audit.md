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

3. **Post a Discord summary (optional)**
   - If `AMPA_DISCORD_WEBHOOK` is set, extract a "Summary" section from the audit output.
   - If no summary is found, fall back to a short line with the work id, title, and exit code.
   - Send a Discord message capped to ~1000 chars.

4. **Post full audit output to Worklog**
   - Create a Worklog comment with a standard heading: `# AMPA Audit Result`.
   - If output is short enough, embed it directly in the comment.
   - If output is too large, write it to a temp file and post a comment that references the file path.
   - Temp files used for comments are removed after posting.

5. **Auto-complete check (optional)**
   The scheduler will attempt to move the work item to `completed` and `in_review` when:
   - The audit output indicates a merged PR (PR URL or "PR merged" token), and
   - There are no open child work items, or the audit explicitly says it is ready to close.

   If a GitHub PR URL is found, the scheduler can verify merge status with `gh pr view`.

## Cooldown logic

To avoid repeatedly auditing the same item, triage-audit inspects prior Worklog comments on each item. Any comment containing `# AMPA Audit Result` is treated as a prior audit. If the most recent audit is within the cooldown window, the item is skipped.

## Output locations and formats

- **Worklog comment**: A comment is added with heading `# AMPA Audit Result` and the audit output under an "Audit output" label. This heading is also used for cooldown detection.
- **Discord summary**: A short message is sent to the webhook using a markdown-style header:

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

## Notes

- The triage-audit flow is post-processing only. The scheduler still records the command run normally before executing this logic.
- The `/audit` command is a separate command definition; triage-audit only invokes it and does not define its output format.
