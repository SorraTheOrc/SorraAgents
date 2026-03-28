# AMPA triage-audit flow

This document explains the AMPA scheduler's audit post-processing (historically called "triage-audit"). It is triggered for scheduled commands with `command_id="wl-audit"` or `command_type="audit"` (the runtime continues to accept the legacy `wl-triage-audit` / `triage-audit` identifiers during a migration window) and describes the descriptor-driven handlers now used to perform audits.

References:
- `ampa/audit_poller.py` (candidate detection, cooldown filtering, selection)
- `ampa/audit/handlers.py` (descriptor-driven audit handlers: `audit_result`, `audit_fail`, `close_with_audit`)
- `ampa/audit/result.py` (audit result parser and dataclasses: `AuditResult`, `CriterionResult`, `ParseError`)
- `ampa/scheduler.py` (routing)
- `tests/test_audit_poller.py` (poller unit tests)
- `tests/test_audit_handlers.py` and `tests/test_scheduler_audit_routing.py` (integration and behavior tests)
- `skill/audit/SKILL.md` (the `/audit` command itself)

## Architecture

The audit flow is organised around three responsibilities:

- `audit_poller.py` — Detection and selection. Queries for `in_review` items, applies store-based cooldown filtering, selects the oldest eligible candidate, persists the `last_audit_at` timestamp, and hands off the selected candidate to a handoff handler.
- `ampa/audit/handlers.py` — Descriptor-driven handlers implementing the audit command lifecycle. Handlers run the audit (via `opencode run "/audit <id>"`), parse output using `ampa/audit/result.py`, write structured `# AMPA Audit Result` comments, evaluate invariants, and apply descriptor effects (state transitions, tags, notifications).
- `ampa/audit/result.py` — Parsing and typed models. Extracts the structured report between marker delimiters and returns a typed `AuditResult` or `ParseError` for handlers to consume.

The scheduler's `start_command()` routes `wl-audit` (legacy: `wl-triage-audit`) commands through `poll_and_handoff()` and passes a handler adapter that delegates to the descriptor-driven handlers in `ampa/audit/handlers.py`.

## End-to-end flow

1. Detect and select a candidate (audit poller)
   - Run `wl list --stage in_review --json` and normalise the response (handles list/dict formats, deduplicates by ID).
   - Read the `last_audit_at_by_item` dict from the scheduler store and filter out items still in their cooldown window.
   - Sort remaining candidates by `updated_at` ascending (oldest first).
   - Select the first eligible candidate and persist `last_audit_at_by_item[selected_id] = now` to the store BEFORE calling the handler to avoid re-selection during long-running audits.
   - Hand off the selected work item dict to the handler.

2. Run the audit (handler / opencode)
   - The handler executes `opencode run "/audit <work_id>"` (via the injected `run_shell` / `opencode` adapter).
   - Capture stdout and stderr into a single audit output string.
   - The audit agent is expected to produce a **structured report** bounded by delimiter markers (see below).

3. Parse the structured report
   - Parsing is implemented in `ampa/audit/result.py`. The parser extracts content between `--- AUDIT REPORT START ---` and `--- AUDIT REPORT END ---`, parses the `## Summary`, the Acceptance Criteria table (rows → `CriterionResult`), `## Recommendation`, and detects closure recommendation.
   - The parser returns a typed `AuditResult` with fields such as `summary`, `criteria`, `recommendation`, `audit_recommends_closure`, `raw_output`, and `report_text`. If the output is empty or malformed the parser returns a `ParseError`.
   - Handlers use the parsed `AuditResult` to produce the Worklog comment and to evaluate invariants that gate descriptor effects.

4. Post a Discord summary (optional)
   - If `AMPA_DISCORD_BOT_TOKEN` (or equivalent notification config) is present, handlers extract the `## Summary` section and send a Discord message capped in length.

5. Post structured audit report to Worklog
   - Handlers create a Worklog comment with heading `# AMPA Audit Result` and include the structured report (or the raw output when parsing fails).
   - If the report is larger than the configured `truncate_chars`, the handler writes it to a temp file and posts a comment referencing the file path. Temp files are removed after posting.

6. Auto-complete check (optional)
   - The `close_with_audit` handler verifies `audit_recommends_closure` via the `InvariantEvaluator` pre-invariant, extracts PR URLs (from comments/text), optionally verifies merge status using `gh pr view` (configurable via `verify_pr_with_gh`), and checks that there are no open direct children via `wl show --children --json`.
   - On success the handler issues `wl update --status completed --stage in_review --needs-producer-review true`, applies the `audit_closed` tag, and sends configured notifications.

## Cooldown logic

Cooldown is determined solely from the scheduler store's `last_audit_at_by_item` dict. For each candidate the poller looks up `last_audit_at_by_item[item_id]`. If `(now - last_audit_at) < audit_cooldown_hours` the item is skipped. Items with no store entry are eligible immediately.

The `last_audit_at` timestamp is written to the store BEFORE the handler is called. This prevents re-selection while an audit is running; if the store is lost or reset all items become immediately eligible again.

> Note: Older implementations used per-candidate comment scanning to determine cooldown. The current design uses the scheduler store to avoid per-candidate shell calls and simplify behaviour.

## Structured audit output

The audit agent should produce a structured report bounded by delimiter markers. The expected format is:

```text
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

The parser in `ampa/audit/result.py` extracts the content between the markers and produces a typed `AuditResult` for handlers to consume.

## Output locations and formats

- **Worklog comment**: Handlers post a comment with heading `# AMPA Audit Result` containing the extracted structured report (or raw output when parsing fails).
- **Discord summary**: Handlers extract the `## Summary` and include it in a notification message. Additional metadata such as Work Item ID, GitHub issue link (when available), and PR link are included when present.

## Configuration and metadata

Environment variables and per-command metadata used by the audit flow:

- `AMPA_DISCORD_BOT_TOKEN`: If set, Discord summary messages are sent via the bot.
- `AMPA_VERIFY_PR_WITH_GH`: When truthy, handlers verify PR merge state via `gh pr view`; otherwise PR verification is skipped.

Per-command metadata (scheduler command spec):

- `audit_cooldown_hours` (default: 6): Minimum hours between audits for the same work item.
- `truncate_chars` (default: 65536): Max chars to inline in Worklog comments before writing to a temp file.
- `verify_pr_with_gh` (default: true): Overrides `AMPA_VERIFY_PR_WITH_GH` for this run.

## Module responsibilities

| Concern | Module | Function/Class |
|---------|--------|----------------|
| Candidate query | `audit_poller.py` | `_query_candidates()` |
| Cooldown filtering | `audit_poller.py` | `_filter_by_cooldown()` |
| Candidate selection | `audit_poller.py` | `_select_candidate()` |
| Store persistence | `audit_poller.py` | `poll_and_handoff()` |
| Handoff protocol | `audit_poller.py` | `AuditHandoffHandler` protocol |
| Audit invocation | `ampa/audit/handlers.py` | handler lifecycle: run audit, parse output, post comment |
| Report parsing | `ampa/audit/result.py` | `parse_audit_output()` — returns `AuditResult` / `ParseError` |
| Discord notifications | `ampa/audit/handlers.py` | via `notifications` module |
| Comment posting | `ampa/audit/handlers.py` | posts `# AMPA Audit Result` |
| Auto-completion | `ampa/audit/handlers.py` | `close_with_audit` handler (PR verification, children check) |
| Scheduler routing | `scheduler.py` | `Scheduler.start_command()` |

## Notes

- The audit flow is post-processing only. The scheduler still records the command run normally before executing this logic.
- The `/audit` command is a separate command definition; the audit poller invokes it. The audit agent is instructed to produce structured output using the marker format above.
- `ampa/audit/handlers.py` expect a work item dict (with at least `id` and `title`) when invoked by the scheduler poller.

(End of file)
