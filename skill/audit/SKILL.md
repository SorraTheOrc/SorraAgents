---
name: audit
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. This skill exposes a canonical runner for automated use and a structured markdown report format consumed by orchestrators such as Ralph.

## When To Use

1. **Scan for a work item ID** — search for `[A-Z]{2}-[A-Z0-9]+`. If found → item-level audit (step 3). If not → project-level (step 2).
2. **No ID found (project-level)** — run `wl list --json`, `wl in_progress --json`, `wl blocked --json`. For general status queries ("status", "audit", "What's the current status?").
3. **ID found (item-level)** — run `wl show <id> --children --json` for specific work-item queries.

## Pre-flight affirmation

Verify absence before proceeding to the audit flow. Confirm that the work item is ready for audit and that no active conflicting processes exist.

## Status Lifecycle

The audit runner manages the work item's `status` field during execution to prevent concurrent audit attempts.

1. **Capture original status** — fetched via `wl show <id> --json` at `cmd_issue()` start.
2. **`in_progress`** — set at `cmd_issue()` start, after capturing original status.
3. **Restore original status** — set after audit logic completes (via `try/finally`, guaranteed even on failure).

Behavior:
- Transition: `in_progress` → original status (captured before audit, restored in `finally`).
- Falls back to `open` if original status cannot be determined (e.g., `wl show` fails).
- `--do-not-persist` does NOT affect the status lifecycle.
- `stage` is NOT modified.
- If the status update fails, the error is silently caught.

### Manual Fallback

When running without `audit_runner.py`:

```bash
# Capture original status before setting in_progress
ORIG_STATUS=$(wl show <id> --json | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','open'))")
wl update <id> --status in_progress --json   # before audit
wl update <id> --status "$ORIG_STATUS" --json # after audit (success or failure)
```

Always include `--json` for machine-readable output.

## Freshness Gate

Short-circuits item-level audits when a recent, valid audit exists to avoid unnecessary model calls.

### Behavior

1. Fetch latest audit via ``wl audit-show <id> --json``; compare ``auditedAt`` against ``updatedAt + 60s``.
2. If fresh: prints ``Skipping: audit still fresh`` + existing report, exits code 0 **without** status lifecycle.
3. If stale or error: falls through to normal full audit.
4. ``--force`` bypasses the gate. Applies only to item-level audits (``cmd_issue``).

Configuration: ``AUDIT_FRESHNESS_BUFFER_SECONDS = 60`` (in ``./scripts/audit_runner.py``).

```
Skipping: audit still fresh
<existing rawOutput>
```

No status lifecycle transitions occur, and no persistence is performed.

## Safety and prompt design

- Audit executions should be read-only except for the explicit persistence step and automatic status lifecycle. Use `[READ-ONLY AUDIT]` to mark read-only phases and `[PERSIST-AUDIT]` when persisting.
- Do NOT close, create, or delete work items during an audit. Permitted state-modifying actions: (1) storing audit text via the canonical persister, (2) runner's automatic `in_progress`→`open` lifecycle. Do NOT change `stage`.
- Refuse any request to run state-modifying `wl` commands outside the authorized flow.
- If ambiguity prevents a reliable verdict, return immediately and do NOT persist.
- The runner supports `--debug-log` to append raw Pi output to a JSONL file.

## Two-Phase Audit Pipeline

```text
Phase 1: Automated Screening           Phase 2: Deep Code Analysis
  ├─ Code quality check (linters)         └─ Model verifies implementation
  ├─ Children stage check                     code against each AC
  └─ Surface-level AC verdict pass
        ↓
Decision Gate → blocking? → demote "met"→"partial", skip Phase 2
        ↓ (no blockers)
Phase 2: Deep Code Analysis
```

### Phase 1 — Automated Screening
Order: (1) code quality check, (2) children stage check (must be `in_review` or `done`), (3) surface-level AC assessment.

Blocking: critical/high code quality findings, or any non-deleted child with stage not in `in_review`/`done`.

### Decision Gate
- **Blocking found:** all "met" ACs → "partial" ("pending deep code review"), skip Phase 2, report "Ready to close: No".
- **No blockers:** proceed to Phase 2.

### Phase 2 — Deep Code Analysis
Model reads actual implementation files, verifies each AC against code behavior, checks for discrepancies, provides file:line evidence.

### Final Verdict
"met" only when BOTH phases confirm it. Disagreement → "partial".

### Ready-to-close criteria
1. All ACs `met` or `adjusted`.
2. All active children in `in_review` or `done` stage (children with empty stage excluded).
3. No critical or high code quality findings.

> **IMPORTANT:** Release process constraints are NOT audit concerns. Do NOT include merge-status, deployment, or release criteria.

### Model metadata line

When model information is available (e.g., when the runner is invoked with
``--model`` and ``--model-source`` flags), a metadata line is inserted after
``Ready to close:`` and before ``## Summary`` in issue-level and child audit
reports:

- When model and source are provided: ``Model: <model> (provider: <source>)``
- When no model info is available: ``Model: manual (no provider)``

**Project-level reports** (``_assemble_project_report``) are NOT modified.

Examples:

- ``Model: Proxy/qwen3 (provider: local)``
- ``Model: gpt-4 (provider: remote)``
- ``Model: manual (no provider)``

## Summary

<concise 2-4 sentence summary>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `<text>` | met/unmet/partial/adjusted | `<file:line — note>` |

If no ACs found: "No acceptance criteria defined."

## Variance Decisions

Only included when at least one criterion has verdict `adjusted`.

| # | Source | Criterion | Justification |
|---|--------|-----------|---------------|
| 1 | `<id>` | `<text>` | `<reason>` |

## Children Status

### `<child-title>` (`<child-id>`) — `<status>`/`<stage>`

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `<text>` | met/unmet/partial/adjusted | `<file:line>` |

If no children: "No children."

## Code Quality

Automatically added by the runner. Do NOT manually construct.

```text
No code quality issues found.
```

| # | Severity | File | Line | Message | Linter | Code |
|---|----------|------|------|---------|--------|------|

### Verdict guidance

- **met** — satisfied (both Phase 1 + Phase 2 confirm).
- **unmet** — not satisfied; blocks closure.
- **partial** — partially satisfied; blocks closure. May indicate Phase 1 blocked Phase 2 ("pending deep code review").
- **adjusted** — adapted during implementation, still satisfies user story intent. Does **not** block closure.

## Success Criteria

Synonym for "Acceptance Criteria". Use **Acceptance Criteria** as canonical heading.

## Exit Codes

- 0 – success
- 1 – Worklog/CLI/Pi failure
- 2 – argument error

## Scripts

- **Runner:** `./scripts/audit_runner.py` — `python3 ./scripts/audit_runner.py issue|project <id> [--do-not-persist] [--pi-bin] [--model] [--model-source] [--debug-log] [--json] [--force]`
- **Persister:** `./scripts/persist_audit.py` — persist from stdin, file, or CLI string

**Timeout:** `CALL_PI_TIMEOUT`=600s per Pi call. Cumulative elapsed-time guard (110s) skips remaining child audits to prevent silent kill. On timeout, returns `unmet` with evidence "Pi model call timed out."

### Code Quality Integration

Runner performs code quality checks before AC verification (invokes `../code-review/scripts/code_quality.py`):
1. Language detection → linter probing (ruff, eslint, markdownlint, shellcheck) → findings classified by severity
2. Critical/high findings → "Ready to close: No"; medium/low are warnings
3. Quality epics ("Quality Improvement - Refactoring") created/reused for findings
4. If `code_quality` module unavailable, continues with warning

- Persist from stdin: `cat report.md | python3 ./scripts/persist_audit.py --issue-id SA-123`
- Persist from a file: `python3 ./scripts/persist_audit.py --issue-id SA-123 --file report.md`
- Persist from a CLI string: `python3 ./scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

Notes:

- The runner supports an optional persistence step. By default the runner will persist the generated structured audit into the work item unless invoked with `--do-not-persist`; use `--do-not-persist` for dry runs. Alternatively, the persister script (`./scripts/persist_audit.py`) may be invoked explicitly to store the report. Both mechanisms perform the same `wl update` call and are the approved ways to persist an audit.
- The persister (and the runner when persisting) call: `wl update <issue-id> --audit-text "<report>" --json` and return a non-zero exit code on failure.
- **Child item audit persistence:** When auditing a parent work item with children, the runner also persists an individual audit report to each child work item. Each child receives a focused report covering only its own acceptance criteria. Child persistence is controlled by the same `--do-not-persist` flag — if persistence is disabled for the parent, child persistence is also skipped. Child persist failures are logged as warnings to stderr but do not prevent the parent audit from succeeding.

## Guidance for models

- Return a structured markdown report with `Ready to close:` header and canonical sections.
- **Follow the two-phase pipeline:** Phase 1 first; if blocking issues, skip Phase 2 and demote "met"→"partial" ("pending deep code review").
- **Deep code analysis is mandatory when Phase 1 passes** — read actual implementation files, verify each AC against code behavior.
- **Ready-to-close criteria:** (1) all ACs `met` or `adjusted`, (2) all active children in `in_review`/`done`, (3) no critical/high code quality findings.
- **Children in `in_review` do NOT block closure** — only pre-review stages (`idea`, `intake_complete`, `plan_complete`) block.
- **Do NOT add release-process or merge-status constraints** — they are not audit concerns.
- If ACs can't be determined unambiguously, return immediately and do NOT persist.
- **Persistence is mandatory.** Use the runner or `./scripts/persist_audit.py`. Use `[PERSIST-AUDIT]` annotation.

### Persistence Procedure (MUST FOLLOW)

1. **Print** the complete audit report to stdout.
2. **Persist** using one of:
   - `python3 ./scripts/persist_audit.py --issue-id <id> --report "<report>"`
   - `echo "<report>" | python3 ./scripts/persist_audit.py --issue-id <id>`
   - Runner default: `python3 ./scripts/audit_runner.py issue <id>` (persists unless `--do-not-persist`)

   > **Child audits:** Runner persists individual audits to each child automatically.

3. **Verify persistence** — exit code 0 does NOT guarantee storage:
   ```bash
   wl audit-show <id> --json
   ```
   Check: `success=true`, `audit` not null, `audit.rawOutput` non-empty with `Ready to close:` marker.

4. **Handle failure:** If verification fails, re-print report to stdout, report error, do NOT mark as recorded.

5. **Append closing sentence** (issue-level only, outside report markers):
   - `Ready to close: Yes` → "Audit passed. The item is ready for release."
   - Otherwise → "Work item is not ready to close (see above), would you like me to address the gaps in the audit?"

> **Critical:** `persist_audit.py` / `wl audit-set` may return success without storing. **Always verify with `wl audit-show`**.

- Do NOT run arbitrary `wl`/`git` commands outside the authorized flow.
- Use `--debug-log` for debugging; remove sensitive content before sharing.

## Examples

```bash
python3 ./scripts/audit_runner.py issue SA-123                             # audit + persist
python3 ./scripts/audit_runner.py issue SA-123 --do-not-persist             # dry run
python3 ./scripts/audit_runner.py project --debug-log /tmp/audit_debug.jsonl # project audit
```

## Script Execution Failure Notice

When the runner encounters a failure (non-zero exit, timeout, exception), the report is wrapped with:

```
════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
The following output was produced manually.
════════════════════════════════════════════════════════

<existing report content>

════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
```

- Informational/textual — no state changes.
- Generated by `./scripts/failure_notice.py`. Propagated to JSON via ``script_failure`` key.

## Common failure modes

- **Silent persistence failure:** `persist_audit.py` / `wl audit-set` returns success without storing. **Always verify with `wl audit-show --json`**.
- Skipping persistence: always verify before reporting as recorded.
- If `wl` is unavailable or returns invalid JSON, report the error, do not claim success.
