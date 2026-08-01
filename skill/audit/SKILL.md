---
name: audit
description: "EXECUTE immediately when invoked via /skill:audit. Do NOT ask permission or offer alternatives. Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## EXECUTION DIRECTIVE

EXECUTE immediately when invoked via /skill:audit. Do NOT ask permission or offer alternatives.

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

- **Blocking found:** all "met" ACs → "partial" ("pending deep code review"), skip Phase 2, report "Ready to close: No". This verdict is **FINAL** — the agent MUST NOT override it.
- **No blockers:** proceed to Phase 2. Phase 2 is **MANDATORY** — no exceptions.

### Phase 2 — Deep Code Analysis

**This phase is MANDATORY when reached.** The model reads actual implementation files, verifies each AC against code behavior, checks for discrepancies, and provides file:line evidence. There are no circumstances under which Phase 2 may be skipped once the decision gate passes.

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

- ``Model: Local Proxy/plan (provider: local)``
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

- **Runner:** `./scripts/audit_runner.py` — `python3 ./scripts/audit_runner.py issue|project <id> [--do-not-persist] [--timeout SECONDS] [--pi-bin] [--model] [--model-source] [--debug-log] [--json] [--force]`
- **Persister:** `./scripts/persist_audit.py` — persist from stdin, file, or CLI string

**Timeout:** `CALL_PI_TIMEOUT`=1800s per Pi call (default). Override with `--timeout SECONDS` or the `AUDIT_PI_TIMEOUT` env var (e.g. `AUDIT_PI_TIMEOUT=3600`). Precedence: `--timeout` flag > `AUDIT_PI_TIMEOUT` env var > 1800s default. Cumulative elapsed-time guard (110s) skips remaining child audits to prevent silent kill. On timeout, returns `unmet` with evidence "Pi model call timed out."

**Provider-error retry:** Pi calls that end in a provider error (`stopReason: "error"` / `errorMessage` on the last assistant message of `agent_end`, e.g. Local Proxy `finish_reason: error`) are retried automatically up to `_PI_MAX_RETRIES` (2) times with linear backoff (`_PI_RETRY_BACKOFF_SECONDS`). Timeouts and unparseable-but-otherwise-healthy responses are NOT retried. If a provider error persists after retries, ACs fall back to `partial` with evidence like "Pi provider error: <errorMessage> — criterion could not be evaluated." rather than the misleading "Pi model output could not be parsed" message, so operators can distinguish a transient model outage from a genuine parse failure.

**Per-call timing instrumentation:** Every Pi call (`_call_pi`) records its wall-clock duration via `time.monotonic` and attaches `elapsed_seconds` to the returned result dict (all return paths, including timeouts and provider errors). `_call_pi_and_maybe_log` emits a per-call timing line to stderr in the form:

```text
Per-call timing: issue_id=<id> context=<context> elapsed_seconds=<seconds>
```

where `<context>` is the call type (e.g. `parent`, `phase2_deep`, `phase2_child:<i>`, `child:<id>`, `project`). This establishes a performance baseline for Phase 2 deep analysis (N+1 sequential agent-mode calls: one parent + one per active child) and makes regressions visible. The same `elapsed_seconds` value is written into `--debug-log` JSONL entries alongside `issue_id`, `context`, and `provider_error`.

**File-scope manifest (Phase 2):** Each Phase 2 prompt (parent `phase2_deep` and every child `phase2_child:<i>`) now includes a **FILE SCOPE** section built from the work item's **Key Files** section, the **git changed-file list** (`git diff --name-only HEAD` + `git status --porcelain`), **Phase 1 evidence file:line references** (so the model verifies named files rather than re-discovering them), and a lightweight **repository index** (top-level layout with file counts). The prompt instructs the model to read ONLY in-scope files and to avoid unbounded `find`/`grep -r`/`ls -R` exploration. This bounds the dominant Phase 2 cost (unbounded repo exploration) without changing verdict semantics. If git is unavailable, the manifest degrades gracefully to the Key Files/evidence/index entries that can still be determined. See `docs/dev/audit-phase2-performance-evaluation.md` (SA-0MSAHR63100415PM) for the underlying evaluation.

**Child verdict reuse (Phase 2):** When a child's own fresh audit already produced a ready verdict (`child_audit_ready=True`, from `cmd_issue`'s auto-triggered child audit), the parent Phase 2 **skips** the duplicated child deep-analysis call (`phase2_child:<i>`) and reuses the child's existing `ac_results`. Children without a ready verdict (not ready / stale / no audit) still get parent deep analysis. This eliminates duplicated child verification work without changing verdict semantics.

**Tools-enabled invocation (Phase 2 only):** Phase 2 deep analysis calls Pi with
`enable_tools=True`, which appends
`--tools read,bash,grep,find,ls --exclude-tools ask_question` to the pi command.
This gives the model file-reading capabilities to verify ACs against
implementation code. Non-Phase-2 calls (Phase 1 screening, project-level audit)
remain in bare LLM pipe mode (`enable_tools=False`).

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

- **Persistence + readback verification is an invariant of the runner.** Unless `--do-not-persist` is given, the runner ALWAYS persists the audit and then performs a readback verification via `wl audit-show --json` to confirm the stored audit is retrievable. If either step fails, the runner exits non-zero. Use `--do-not-persist` for dry runs. The `--require-persist` flag has been removed — persist+verify is now unconditional.
- The persister (and the runner when persisting) call: `wl audit-set <issue-id> --ready-to-close <yes|no> --summary <text> --raw-output "<report>" --json` and return a non-zero exit code on failure. After a successful return code, the runner calls `wl audit-show <issue-id> --json` and exits non-zero if the stored audit is null or has empty `rawOutput`.
- **Child item audit persistence:** When auditing a parent work item with children, the runner also persists an individual audit report to each child work item. Each child receives a focused report covering only its own acceptance criteria. Child persistence is controlled by the same `--do-not-persist` flag — if persistence is disabled for the parent, child persistence is also skipped. Child persist failures are logged as warnings to stderr but do not prevent the parent audit from succeeding. Child readback verification is out of scope for the current release.

## Guidance for models

### Authority and Runner Verdicts (CRITICAL)

- **The audit runner (`audit_runner.py`) is the CANONICAL audit path.** Its verdict is **authoritative** and MUST NOT be overridden by a subsequent model-driven (manual) audit.
- **If the runner produced an audit report with "Ready to close: No", "partial", or "pending deep code review", you MUST NOT produce a contradictory override audit** claiming the item is ready to close. The runner's verdict stands.
- **You MAY re-audit only if explicitly requested by the operator with `--force` or a clear directive to re-run.** Even then, you MUST respect the runner's original verdict and MUST NOT demote a runner-produced "ready to close: Yes" verdict without fresh, documented evidence.
- When the runner has already run and produced a report, **do NOT run the manual audit path at all** unless forced. The runner's two-phase pipeline is complete and authoritative.

### Two-Phase Pipeline (MANDATORY)

- Return a structured markdown report with `Ready to close:` header and canonical sections.
- **Phase 2 deep code analysis is MANDATORY when Phase 1 passes — under NO circumstances may it be skipped.** Read actual implementation files, verify each AC against code behavior, and provide file:line evidence.
- The runner's decision gate is the sole arbiter of whether Phase 2 is skipped:
  - If the runner **blocks Phase 2** (due to critical/high code quality findings, or children not in `in_review`/`done`), the Phase 1 verdict of "partial" ("pending deep code review") is FINAL. You MUST NOT run Phase 2 yourself, and you MUST NOT override the verdict.
  - If the runner **passes to Phase 2**, Phase 2 MUST execute. There is no exception.
- **Blocking issues are narrowly defined** — only the following block Phase 2:
  1. **Critical or high severity** code quality findings from the linter (not medium or low).
  2. **Any active child work item** whose stage is not `in_review` or `done` (stages `idea`, `intake_complete`, `plan_complete`, or empty string on a non-deleted child). Deleted children are excluded.
- **Nothing else blocks Phase 2.** Ambiguity in ACs, medium-severity linting warnings, or agent preference are NOT valid reasons to skip Phase 2.
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
   - Runner default: `python3 ./scripts/audit_runner.py issue <id>` (persists **and verifies** unless `--do-not-persist`)

   > **Readback verification:** After persisting, the runner always reads back the stored audit via
   > `wl audit-show <id> --json` and checks that the `audit` object exists and `rawOutput` is not
   > empty. This is an invariant — not a configurable step. If readback fails, the runner exits non-zero.

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

### Agent-mode response parsing

- Phase 2 deep analysis now runs Pi in agent mode (with `--tools`). The agent-mode
  JSON-stream output may contain additional event types (`agent_start`, `turn_start`,
  `message_start`, `tool_execution_start`, `tool_execution_end`, `agent_end`) not
  present in bare LLM pipe mode. The `_extract_pi_text()` and `_parse_pi_json_line()`
  functions handle these transparently by extracting text content from
  `message_update` events (same as bare LLM mode).
- If response parsing fails, check debug logs (use `--debug-log`) to see the raw
  agent output. The runner automatically falls back to Phase 1 results on Phase 2
  failure.
- The `_extract_json_array()` function strips prose text before/after JSON arrays,
  so it works correctly regardless of whether the model wraps its output in
  explanatory text (common in agent mode).
