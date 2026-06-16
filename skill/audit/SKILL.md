---
name: audit
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. This skill exposes a canonical runner for automated use and a structured markdown report format consumed by orchestrators such as Ralph.

## When To Use

- User asks general project status (e.g., "What is the current status?", "Status of the project?", "status", "audit the project", "audit").
- User asks about a specific work item id (e.g., "What is the status of wl-123?", "status wl-123", "audit wl-123").

## Safety and prompt design

- Audit executions should be read-only except for the explicit, single persistence step that stores the structured audit into the associated work item. Use the designation `[READ-ONLY AUDIT]` in Pi prompts to mark read-only phases, and use `[PERSIST-AUDIT]` when performing the authorized persistence operation.
- Do NOT close, create, or delete any work items during an audit. The ONLY permitted state-modifying action for this skill is storing the audit text via the canonical persister (skill/audit/scripts/persist_audit.py) or the runner's built-in persistence; do not perform other `wl`, `git`, or arbitrary state-modifying commands. Do NOT change state of work items (e.g., update stage, status) beyond audit persistence.
- When persisting, use the canonical persister script or the runner's built-in persistence option. If asked to run arbitrary `wl`, `git`, or other state-modifying commands outside the authorized persister flow, refuse and report the request to the operator.
- The model should return a structured markdown report. If ambiguity prevents a reliable verdict on acceptance criteria, return immediately and do NOT persist the audit. Persistence must be an explicit, deliberate step — do not persist partial or ambiguous audits.
- To aid debugging, the canonical runner supports a `--debug-log` flag which appends raw Pi output to a JSONL file (see Scripts section).

## Two-Phase Audit Pipeline

The audit follows a strict two-phase pipeline:

```
Phase 1: Automated Screening
  ├─ Code quality check (linters)
  ├─ Children stage check
  └─ Surface-level AC verdict pass
        ↓
Decision Gate → If Phase 1 has BLOCKING issues → demote "met"→"partial" (pending deep code review), skip Phase 2
        ↓ (no blocking issues)
Phase 2: Deep Code Analysis
  └─ Model reads and verifies implementation code against each AC
        ↓
Final Verdict: "met" only if BOTH phases confirm it
```

### Phase 1 — Automated Screening

Runs in this order:

1. **Code quality check** — linters (ruff, eslint, markdownlint, shellcheck)
2. **Children stage check** — all children must be in `in_review` or `done`
3. **Surface-level AC assessment** — model evaluates criteria against file existence and test results

Blocking conditions that stop Phase 1:

- Any critical or high code quality finding
- Any non-deleted child with stage not in `in_review` or `done`

### Decision Gate

If Phase 1 encounters blocking conditions:

- **All** ACs that were assessed "met" are demoted to **"partial"** with evidence **"pending deep code review"**
- Phase 2 is skipped entirely
- Report reads "Ready to close: No"

If Phase 1 passes (no blocking issues):

- Proceed to Phase 2

### Phase 2 — Deep Code Analysis

Only runs when Phase 1 passes. The model:

1. **Reads the actual implementation files** referenced in each AC
2. **Verifies each AC against actual code behavior** — confirms the code does what the AC claims
3. **Checks for discrepancies** between documented behavior and actual implementation
4. **Provides specific file:line evidence** for every verdict

### Final Verdict

An AC is recorded as **"met"** only when:

- Phase 1 surface-level check passes (no blockers)
- Phase 2 deep code analysis confirms the AC is genuinely satisfied

If either phase disagrees, the verdict is "partial" (with evidence noting which phase flagged the issue).

### Ready-to-close criteria

A work item is considered ready to close when:

1. **All acceptance criteria are met or have acceptable variance** — every criterion in the parent and all children must have final verdict `met` or `adjusted`. The `adjusted` verdict indicates that the criterion was adapted during implementation in a way that still satisfies the user story intent.
2. **All active children are in `in_review` or `done` stage** — children with `status: in_progress` but `stage: in_review` are acceptable and do NOT block closure. Only children with stages like `idea`, `intake_complete`, `plan_complete`, or other pre-review stages block closure.
3. **No critical or high code quality findings** — code quality checks run automatically during the audit. Critical and high severity findings block closure. Medium and low findings produce warnings but do not block closure.

Children with an empty stage (`""`) are excluded from the stage check (they may be newly created or not yet processed).

### Model metadata line

When model information is available (e.g., when the runner is invoked with
``--model`` and ``--model-source`` flags), a metadata line is inserted after
``Ready to close:`` and before ``## Summary`` in issue-level and child audit
reports:

- When model and source are provided: ``Model: <model> (provider: <source>)``
- When no model info is available: ``Model: manual (no provider)``

**Project-level reports** (``_assemble_project_report``) are NOT modified.

Examples:

- ``Model: opencode-go/deepseek-v4-flash (provider: local)``
- ``Model: gpt-4 (provider: remote)``
- ``Model: manual (no provider)``

## Summary

<concise 2-4 sentence summary of overall status, key findings, and whether the item can be closed>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial/adjusted | <file_path:line_number — one-line note> |

<If no acceptance criteria were found, write: "No acceptance criteria defined.">

## Variance Decisions

When one or more acceptance criteria have verdict `adjusted`, a **Variance Decisions** section appears after the Acceptance Criteria Status table. This section documents the adjustments made and the justification for accepting them.

| # | Source | Criterion | Justification |
|---|--------|-----------|---------------|
| 1 | parent or child (<id>) | <criterion text> | <justification> |

<This section is only included when at least one criterion has verdict `adjusted`; otherwise it is omitted.>

**Variance decision template:**

- **AC<#> adjusted to allow <description of adjustment>.**
- **Justification:** <why the variance is acceptable — user story intent preserved, quality standards met>

## Children Status

### <child-title> (<child-id>) — <status>/<stage>

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial/adjusted | <file_path:line_number — one-line note> |

<If there are no children, write: "No children.">

## Code Quality

This section is automatically added by the audit runner when code quality checks are enabled. It appears after the Children Status section.

*If no code quality issues found:*

```
No code quality issues found.
```

*If findings are present:*

| # | Severity | File | Line | Message | Linter | Code |
|---|----------|------|------|---------|--------|------|
| 1 | critical | src/main.py | 42 | Unused variable `x` | ruff | F841 |

**Important:** The Code Quality section is generated automatically by the runner. Agents should not manually construct this section.

### Verdict guidance

- **met** — Acceptance criterion is fully satisfied. Only assigned when **both** Phase 1 (automated screening) and Phase 2 (deep code analysis) confirm the criterion is met.
- **unmet** — Acceptance criterion is not satisfied; blocks closure.
- **partial** — Acceptance criterion is partially but not fully satisfied; blocks closure. May also indicate that Phase 1 flagged blocking issues, preventing Phase 2 deep analysis from running (evidence shows "pending deep code review").
- **adjusted** — Acceptance criterion was adapted during implementation. The change is acceptable because it still satisfies the user story intent, produces bug-free execution, and meets quality standards. Does **not** block closure. When using this verdict, include a clear justification in the evidence field explaining why the variance is acceptable.

## Success Criteria

"Success Criteria" is a synonym for "Acceptance Criteria". Both terms are treated equivalently in audit reports. Use **Acceptance Criteria** as the canonical heading; document **Success Criteria** as an accepted synonym where relevant.

## Exit Codes

- 0 – success (report printed to stdout)
- 1 – Worklog / CLI / Pi failure
- 2 – argument error

## Scripts (canonical runner & persister)

The audit skill ships a canonical runner and a persister. Use these from CI, local automation, or orchestrators.

- Runner: `skill/audit/scripts/audit_runner.py`
  - Usage: `python3 skill/audit/scripts/audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>] [--model-source <remote|local>] [--debug-log <file>]`
  - Usage: `python3 skill/audit/scripts/audit_runner.py project [--pi-bin pi] [--model <name>] [--model-source <remote|local>] [--debug-log <file>]`
  - Flags:
    - `--do-not-persist` — do not run persistence (useful for dry runs)
    - `--pi-bin` — path to the `pi` binary
    - `--model` — Pi model name (default: resolved from `.ralph.json`; falls back to `opencode-go/glm-5.1`)
    - `--model-source` — model source: `remote` or `local` (default: `local`)
    - `--debug-log` — append Pi debug output to a JSONL file (helpful for triage)
    - `--json` — emit machine-readable JSON output

- Persister: `skill/audit/scripts/persist_audit.py`

### Code Quality Integration

The audit runner automatically performs code quality checks alongside acceptance criteria verification. The pipeline is:

1. **Code quality check** runs before AC verification (invokes `skill/code_review/scripts/code_quality.py`)
2. **Language detection** scans for Python, TypeScript, Markdown, Shell, JavaScript/Node.js, and C# files
3. **Linter probing** checks for available linters (ruff, eslint, markdownlint, shellcheck, dotnet-format)
4. **Findings classified** by severity (critical, high, medium, low)
5. **Quality epics created**: a "Quality Improvement - Refactoring" epic is created or reused, with child tasks for each finding
6. **Blocking logic**: critical/high findings result in "Ready to close: No"; medium/low findings are warnings only
7. **Report section**: findings appear in a `### Code Quality` section after Children Status

If the `code_quality` module is unavailable, the audit continues with a warning instead of crashing.

- Persist from stdin: `cat report.md | python3 skill/audit/scripts/persist_audit.py --issue-id SA-123`
- Persist from a file: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --file report.md`
- Persist from a CLI string: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

Notes:

- The runner supports an optional persistence step. By default the runner will persist the generated structured audit into the work item unless invoked with `--do-not-persist`; use `--do-not-persist` for dry runs. Alternatively, the persister script (`skill/audit/scripts/persist_audit.py`) may be invoked explicitly to store the report. Both mechanisms perform the same `wl update` call and are the approved ways to persist an audit.
- The persister (and the runner when persisting) call: `wl update <issue-id> --audit-text "<report>" --json` and return a non-zero exit code on failure.
- **Child item audit persistence:** When auditing a parent work item with children, the runner also persists an individual audit report to each child work item. Each child receives a focused report covering only its own acceptance criteria. Child persistence is controlled by the same `--do-not-persist` flag — if persistence is disabled for the parent, child persistence is also skipped. Child persist failures are logged as warnings to stderr but do not prevent the parent audit from succeeding.

## Guidance for models

- Return a structured markdown report only. Use the header `Ready to close:` and the canonical sections above.
- **Follow the two-phase pipeline.** Phase 1 (automated screening) must complete successfully before Phase 2 (deep code analysis) begins. If Phase 1 has blocking issues, do NOT proceed to Phase 2 — instead demote all "met" verdicts to "partial" with evidence "pending deep code review".
- **Deep code analysis is mandatory when Phase 1 passes.** Read the actual implementation files, not just the work item descriptions. Verify each AC against what the code actually does.
- If the model cannot determine acceptance criteria verdicts unambiguously, return immediately and do NOT persist or claim the audit was recorded.
- You MUST persist the audit report to the work item after producing a structured report. Persistence is **mandatory**, not optional. Use one of the approved persistence mechanisms: the canonical runner (which persists by default unless invoked with `--do-not-persist`) or the persister script (`skill/audit/scripts/persist_audit.py`). When performing the authorized persistence step, annotate the prompt with `[PERSIST-AUDIT]` and ensure the report is final and complete.

### Persistence Procedure (MUST FOLLOW)

After producing a structured audit report, you MUST:

1. **Print the complete audit report to stdout** so the operator can see it.
2. **Persist the report** using one of these methods:
   - `python3 skill/audit/scripts/persist_audit.py --issue-id <id> --report "<report text>"` — pass report inline
   - Pipe to stdin: `echo "<report text>" | python3 skill/audit/scripts/persist_audit.py --issue-id <id>`
   - Use the runner: `python3 skill/audit/scripts/audit_runner.py issue <id> --do-not-persist=false` (runner persists by default)

   > **Child audits:** When auditing a parent work item with children, the runner automatically persists individual audits to each child work item as well. These are controlled by the same `--do-not-persist` flag. Check stderr for any child persist warnings.

3. **Verify persistence by querying the database.** An exit code of 0 does **not** guarantee the audit was stored. You MUST confirm the audit actually landed in the worklog database:

   ```bash
   wl audit-show <id> --json
   ```

   Parse the JSON output and verify **all** of the following:
   - `success` is `true`
   - `audit` is **not** `null`
   - `audit.rawOutput` is **not** `null` and is not an empty string
   - `audit.rawOutput` contains the `Ready to close:` marker on the first line

   If **any** of these checks fail, the audit was **not** persisted. Do NOT claim success.

4. **Handle verification failure:** If the audit was not persisted (either the persist call failed or the verification query returned `null`/empty `rawOutput`):
   - Print the complete audit report again to stdout (in case the operator needs to copy it manually)
   - Report the error to the operator, including what the verification query returned
   - Do NOT mark the audit as recorded
   - Do NOT proceed to close any work items

5. **Only mark the audit as recorded** when all verification checks pass.

If you skip persistence, the audit will be invisible to downstream orchestrators (e.g., Ralph) and may cause infinite retry loops. Persistence is the FINAL step of every audit.

> **Critical:** The `persist_audit.py` script and `wl audit-set` command have been observed returning exit code 0 or `success: true` even when the audit was not actually stored in the database. **Always verify with `wl audit-show`** — never trust the exit code alone.

- Do NOT perform arbitrary state-modifying `wl`/`git` commands outside the authorized persister/runner flow. If asked to run such commands, refuse and surface the request to the operator.
- For debugging, the `--debug-log` flag captures raw Pi output. Use it sparingly and remove sensitive content before sharing.

## Examples

- Run an issue audit and persist:

  python3 skill/audit/scripts/audit_runner.py issue SA-123

- Run an issue audit without persisting (dry run):

  python3 skill/audit/scripts/audit_runner.py issue SA-123 --do-not-persist

- Run a project audit and write debug output:

  python3 skill/audit/scripts/audit_runner.py project --debug-log /tmp/audit_debug.jsonl

## Common failure modes

- **Silent persistence failure:** `persist_audit.py` or `wl audit-set` returns exit code 0 / `success: true` but the audit is not actually stored in the database. **Always verify with `wl audit-show --json`** and check that `audit.rawOutput` is populated.
- Skipping persistence: always ensure the audit was persisted and verified before reporting the audit as recorded.
- If `wl` is not available or returns invalid JSON, report the error and do not claim success.
