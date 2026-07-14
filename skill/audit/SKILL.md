---
name: audit
description: "Provide concise project / work item status and run Worklog helpers to augment results. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## Overview

Provide a concise, human-friendly summary of project status or a specific work item. This skill exposes a canonical runner for automated use and a structured markdown report format consumed by orchestrators such as Ralph.

## When To Use

### 1. Scan for a work item ID

Search the user's request for a pattern matching `[A-Z]{2}-[A-Z0-9]+` (e.g., `CG-0MM1OPSGM052NPUN`, `WL-123`, `SA-0ABC...`).

- **If found:** Store it as `<work-item-id>` and proceed to step 3 (item-level audit).
- **If not found:** Proceed to step 2 (project-level audit).

> **Pre-flight affirmation:** State to yourself: `"Proceeding with audit of: <work-item-id>"` or `"Proceeding with project-level audit."` If the ID pattern matched but looks ambiguous, re-scan the request.

### 2. No ID found (project-level audit)

Run `wl list --json`, `wl in_progress --json`, `wl blocked --json`, and compile a project-level summary. Applicable when:

- User asks general project status (e.g., "What is the current status?", "Status of the project?", "status", "audit the project", "audit").

> **Verify absence before proceeding:** Confirm that no line in the user's message contains a string matching `[A-Z]{2}-[A-Z0-9]+\b`. If one is found, return to step 1 and extract it.

### 3. ID found (item-level audit)

Run `wl show <id> --children --json` and compile a focused report on that specific work item. Applicable when:

- User asks about a specific work item id (e.g., "What is the status of wl-123?", "status wl-123", "audit wl-123").

## Status Lifecycle

The canonical audit runner automatically manages the work item's `status` field during audit execution. This provides visibility into in-progress audits and prevents concurrent audit attempts.

### Lifecycle

1. **`in_progress`** — Set at the start of `cmd_issue()`, before any code quality checks, Pi calls, or report assembly.
2. **`open`** — Set after all audit logic completes, including persistence. This is guaranteed to run even on failure or unhandled exceptions via a `try/finally` block.

### Behavior

- The status transition is `in_progress` → `open` for every audit run, regardless of success or failure.
- The `--do-not-persist` flag does NOT affect status lifecycle — status changes occur regardless of persist mode.
- Status changes are performed via the injectable `runner` using `wl update <id> --status <value>`.
- The `stage` field is NOT modified by the status lifecycle.
- If the `open` status update fails (e.g., runner error), the failure is silently caught to avoid masking the main audit result.

### Manual Fallback (Running Without the Runner)

When running an audit manually (i.e., without the `audit_runner.py` script), the
status lifecycle must be managed by hand to match the runner's behavior:

1. **Before starting the audit**, set status to `in_progress`:

   ```bash
   wl update <id> --status in_progress --json
   ```

2. **After the audit completes** (whether successful or failed), set status to `open`:

   ```bash
   wl update <id> --status open --json
   ```

> **Important:** Always include the `--json` flag with `wl update` commands to
> ensure machine-readable output. The audit runner's `_run_wl()` function
> expects JSON output from all `wl` commands.

This ensures the same `in_progress` → `open` transition regardless of
whether the automated runner or a manual process performs the audit.

### Rationale

The status lifecycle was added to solve the problem of concurrent audit attempts and visibility into audit state. Before this feature, there was no way to determine whether an audit was in progress. The deterministic `try/finally` approach guarantees cleanup regardless of outcome.

## Freshness Gate

The audit runner includes a **recent-audit freshness gate** that short-circuits
item-level audits when a recent, valid audit already exists. This prevents
unnecessary model calls and reduces audit time for unchanged work items.

### Behavior

1. Before setting any status lifecycle, `cmd_issue()` fetches the latest audit
   via ``wl audit-show <id> --json`` and compares the audit's ``auditedAt``
   timestamp against the work item's ``updatedAt`` timestamp plus a
   60-second buffer.
2. If the audit's ``auditedAt`` is more recent than ``updatedAt + 60s``, the
   runner prints ``Skipping: audit still fresh``, displays the existing audit
   report (``rawOutput``), and exits with code 0 **without** entering the
   status lifecycle (no ``in_progress`` transition).
3. If no prior audit exists, the audit is stale, or the freshness check fails
   (e.g., ``wl audit-show`` command error), the runner falls through to the
   normal full audit pipeline.
4. The ``--force`` flag bypasses the freshness gate and always runs a full
   audit, even if a recent audit exists.

The gate applies **only to item-level audits** (``cmd_issue``). Project-level
audits (``cmd_project``) are unaffected.

### Configuration

The freshness buffer is a hardcoded constant:

```python
AUDIT_FRESHNESS_BUFFER_SECONDS = 60
```

This is unlikely to change. If needed, modify the constant in
``./scripts/audit_runner.py``.

### Failure handling

If ``wl audit-show`` fails (e.g., no audit data, network error), the gate
gracefully falls through to the normal pipeline. The freshness check never
introduces new failure modes.

### Skip indicator

When the gate short-circuits, the runner prints:

```
Skipping: audit still fresh
<existing rawOutput>
```

No status lifecycle transitions occur, and no persistence is performed.

## Safety and prompt design

- Audit executions should be read-only except for the explicit, single persistence step that stores the structured audit into the associated work item and the automatic status lifecycle management performed by the runner. Use the designation `[READ-ONLY AUDIT]` in Pi prompts to mark read-only phases, and use `[PERSIST-AUDIT]` when performing the authorized persistence operation.
- Do NOT close, create, or delete any work items during an audit. The ONLY permitted state-modifying actions for this skill are: (1) storing the audit text via the canonical persister (./scripts/persist_audit.py) or the runner's built-in persistence, and (2) the runner's automatic status lifecycle (in_progress → open, see Status Lifecycle section). Do NOT perform other `wl`, `git`, or arbitrary state-modifying commands. Do NOT change work item stage — only the `status` field is managed by the lifecycle.
- When persisting, use the canonical persister script or the runner's built-in persistence option. If asked to run arbitrary `wl`, `git`, or other state-modifying commands outside the authorized persister flow, refuse and report the request to the operator.
- The model should return a structured markdown report. If ambiguity prevents a reliable verdict on acceptance criteria, return immediately and do NOT persist the audit. Persistence must be an explicit, deliberate step — do not persist partial or ambiguous audits.
- To aid debugging, the canonical runner supports a `--debug-log` flag which appends raw Pi output to a JSONL file (see Scripts section).

## Two-Phase Audit Pipeline

The audit follows a strict two-phase pipeline:

```text
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

> **IMPORTANT:** The release process (e.g., "must be merged to main first") is **not** an audit concern. Do NOT include release-process constraints, merge-status conditions, or any other deployment/release criteria in audit verdicts. An item is ready to close based solely on the three criteria above. Adding constraints outside these criteria is a prompt violation.

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
| 1 | `<criterion text>` | met/unmet/partial/adjusted | `<file_path:line_number — one-line note>` |

<If no acceptance criteria were found, write: "No acceptance criteria defined.">

## Variance Decisions

When one or more acceptance criteria have verdict `adjusted`, a **Variance Decisions** section appears after the Acceptance Criteria Status table. This section documents the adjustments made and the justification for accepting them.

| # | Source | Criterion | Justification |
|---|--------|-----------|---------------|
| 1 | parent or child (`<id>`) | `<criterion text>` | `<justification>` |

<This section is only included when at least one criterion has verdict `adjusted`; otherwise it is omitted.>

**Variance decision template:**

- **AC`<#>` adjusted to allow `<description of adjustment>`.**
- **Justification:** `<why the variance is acceptable — user story intent preserved, quality standards met>`

## Children Status

### `<child-title>` (`<child-id>`) — `<status>`/`<stage>`

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `<criterion text>` | met/unmet/partial/adjusted | `<file_path:line_number — one-line note>` |

<If there are no children, write: "No children.">

## Code Quality

This section is automatically added by the audit runner when code quality checks are enabled. It appears after the Children Status section.

*If no code quality issues found:*

```text
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

- Runner: `./scripts/audit_runner.py`
  - Usage: `python3 ./scripts/audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>] [--model-source <remote|local>] [--debug-log <file>] [--force]`
  - Usage: `python3 ./scripts/audit_runner.py project [--pi-bin pi] [--model <name>] [--model-source <remote|local>] [--debug-log <file>]`
  - Flags:
    - `--do-not-persist` — do not run persistence (useful for dry runs)
    - `--pi-bin` — path to the `pi` binary
    - `--model` — Pi model name (default: resolved from `.ralph.json`; falls back to `opencode-go/glm-5.1`)
    - `--model-source` — model source: `remote` or `local` (default: `local`)
    - `--debug-log` — append Pi debug output to a JSONL file (helpful for triage)
    - `--json` — emit machine-readable JSON output
    - `--force` — bypass the freshness gate and force a full audit even if a recent audit exists (item-level only)

  **Timeout behavior:**
  - Each internal Pi model call has a default timeout of `CALL_PI_TIMEOUT`=600s
    (configurable via the module constant in `audit_runner.py`). This generous
    timeout allows large audit prompts to complete while still providing a
    safety net against indefinite hangs. The cumulative elapsed-time guard
    (110s threshold for skipping remaining child audits) provides the primary
    protection against the parent bash-tool execution timeout (~120s).
  - On timeout, the runner returns an `unmet` verdict for the affected criteria
    with evidence reading "Pi model call timed out after Ns. Manual audit required."
  - If the cumulative audit time exceeds 110s (e.g., when auditing many children),
    remaining child audits are skipped with a "Skipped due to audit timeout"
    diagnostic, preventing a silent external kill.

- Persister: `./scripts/persist_audit.py`

### Code Quality Integration

The audit runner automatically performs code quality checks alongside acceptance criteria verification. The pipeline is:

1. **Code quality check** runs before AC verification (invokes `../code-review/scripts/code_quality.py`)
2. **Language detection** scans for Python, TypeScript, Markdown, Shell, JavaScript/Node.js, and C# files
3. **Linter probing** checks for available linters (ruff, eslint, markdownlint, shellcheck, dotnet-format)
4. **Findings classified** by severity (critical, high, medium, low)
5. **Quality epics created**: a "Quality Improvement - Refactoring" epic is created or reused, with child tasks for each finding
6. **Blocking logic**: critical/high findings result in "Ready to close: No"; medium/low findings are warnings only
7. **Report section**: findings appear in a `### Code Quality` section after Children Status

If the `code_quality` module is unavailable, the audit continues with a warning instead of crashing.

- Persist from stdin: `cat report.md | python3 ./scripts/persist_audit.py --issue-id SA-123`
- Persist from a file: `python3 ./scripts/persist_audit.py --issue-id SA-123 --file report.md`
- Persist from a CLI string: `python3 ./scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

Notes:

- The runner supports an optional persistence step. By default the runner will persist the generated structured audit into the work item unless invoked with `--do-not-persist`; use `--do-not-persist` for dry runs. Alternatively, the persister script (`./scripts/persist_audit.py`) may be invoked explicitly to store the report. Both mechanisms perform the same `wl update` call and are the approved ways to persist an audit.
- The persister (and the runner when persisting) call: `wl update <issue-id> --audit-text "<report>" --json` and return a non-zero exit code on failure.
- **Child item audit persistence:** When auditing a parent work item with children, the runner also persists an individual audit report to each child work item. Each child receives a focused report covering only its own acceptance criteria. Child persistence is controlled by the same `--do-not-persist` flag — if persistence is disabled for the parent, child persistence is also skipped. Child persist failures are logged as warnings to stderr but do not prevent the parent audit from succeeding.

## Guidance for models

- Return a structured markdown report only. Use the header `Ready to close:` and the canonical sections above.
- **Follow the two-phase pipeline.** Phase 1 (automated screening) must complete successfully before Phase 2 (deep code analysis) begins. If Phase 1 has blocking issues, do NOT proceed to Phase 2 — instead demote all "met" verdicts to "partial" with evidence "pending deep code review".
- **Deep code analysis is mandatory when Phase 1 passes.** Read the actual implementation files, not just the work item descriptions. Verify each AC against what the code actually does.
- **Apply the ready-to-close criteria strictly.** A work item is ready to close when:
  1. All acceptance criteria (parent + children) are `met` or `adjusted`.
  2. All active children are in `in_review` or `done` stage.
  3. No critical or high code quality findings.
- **Children in `in_review` stage do NOT block closure.** Children with `status: in_progress` but `stage: in_review` are acceptable and should result in "Ready to close: Yes" (provided all other criteria are met). Only children in pre-review stages (`idea`, `intake_complete`, `plan_complete`, etc.) block closure.
- **DO NOT add constraints outside the defined criteria.** The release process ("must be merged to main", merge status, deployment status) is not an audit concern. Do not include any release-process, deployment, or merge-status conditions in your verdict. If a child is in `in_review` stage, that is sufficient — do not demand that it be merged or released first.
- **Phase 1 children stage check logic:** Check that every non-deleted child with a non-empty stage has stage `in_review` or `done`. Children with `status: in_progress` but `stage: in_review` are acceptable and do NOT count as blocking. This mirrors the logic in `audit_runner.py`'s `_has_phase1_blocking_issues`.
- If the model cannot determine acceptance criteria verdicts unambiguously, return immediately and do NOT persist or claim the audit was recorded.
- You MUST persist the audit report to the work item after producing a structured report. Persistence is **mandatory**, not optional. Use one of the approved persistence mechanisms: the canonical runner (which persists by default unless invoked with `--do-not-persist`) or the persister script (`./scripts/persist_audit.py`). When performing the authorized persistence step, annotate the prompt with `[PERSIST-AUDIT]` and ensure the report is final and complete.

### Persistence Procedure (MUST FOLLOW)

After producing a structured audit report, you MUST:

1. **Print the complete audit report to stdout** so the operator can see it.
2. **Persist the report** using one of these methods:
   - `python3 ./scripts/persist_audit.py --issue-id <id> --report "<report text>"` — pass report inline
   - Pipe to stdin: `echo "<report text>" | python3 ./scripts/persist_audit.py --issue-id <id>`
   - Use the runner: `python3 ./scripts/audit_runner.py issue <id> --do-not-persist=false` (runner persists by default)

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

6. **Append the closing sentence** (issue-level audits only). After persisting
   and verifying the audit, append a single closing line to your final
   user-facing output. This sentence is **not** part of the persisted audit
   report — it is a UX affordance for the human or agent reading the output.

   - If the report begins with ``Ready to close: Yes``:
     ``Work item is ready to close, would you like me to close it?``
   - Otherwise (``Ready to close: No``, wrapped reports, or no verdict line):
     ``Work item is not ready to close (see above), would you like me to address the gaps in the audit?``

   > **Constraints:**
   > - The closing sentence must appear **outside** any structured-report
   >   markers (``--- AUDIT REPORT START ---`` / ``--- AUDIT REPORT END ---``)
   >   to avoid breaking the AMPA scheduler parser.
   > - It must **not** be included in the text passed to ``persist_audit.py``
   >   or ``wl audit-set``.
   > - It applies **only to issue-level audits**; project-level audits are
   >   unchanged.

If you skip persistence, the audit will be invisible to downstream orchestrators (e.g., Ralph) and may cause infinite retry loops. Persistence is the FINAL step of every audit.

> **Critical:** The `persist_audit.py` script and `wl audit-set` command have been observed returning exit code 0 or `success: true` even when the audit was not actually stored in the database. **Always verify with `wl audit-show`** — never trust the exit code alone.

- Do NOT perform arbitrary state-modifying `wl`/`git` commands outside the authorized persister/runner flow. If asked to run such commands, refuse and surface the request to the operator.
- For debugging, the `--debug-log` flag captures raw Pi output. Use it sparingly and remove sensitive content before sharing.

## Examples

- Run an issue audit and persist:

  python3 ./scripts/audit_runner.py issue SA-123

- Run an issue audit without persisting (dry run):

  python3 ./scripts/audit_runner.py issue SA-123 --do-not-persist

- Run a project audit and write debug output:

  python3 ./scripts/audit_runner.py project --debug-log /tmp/audit_debug.jsonl

## Script Execution Failure Notice

When the audit runner's automated script encounters a failure (e.g., a Pi
subprocess call fails with a non-zero exit code, times out, or raises a
runtime exception), the report output is wrapped with a **Script Execution
Failure Notice** as both the **first and last lines** of the report.

The notice uses the following format:

```
════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
The following output was produced manually.
════════════════════════════════════════════════════════

<existing report content>

════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
The following output was produced manually.
════════════════════════════════════════════════════════
```

This notice is:

- **Purely informational/textual** — no workflow state changes are made
- **Additive** — the existing report format sections (Ready to close, Summary,
  Acceptance Criteria Status, Children Status, Code Quality) are preserved
- Generated by the shared utility module at `./scripts/failure_notice.py`
- Propagated to JSON mode output via the ``script_failure`` key

Failure types detected:

- Non-zero exit code from subprocess calls (with captured stderr)
- Timeout exceptions (subprocess.TimeoutExpired)
- Unavailable dependencies (FileNotFoundError)
- Runtime exceptions caught during script execution

## Common failure modes

- **Silent persistence failure:** `persist_audit.py` or `wl audit-set` returns exit code 0 / `success: true` but the audit is not actually stored in the database. **Always verify with `wl audit-show --json`** and check that `audit.rawOutput` is populated.
- Skipping persistence: always ensure the audit was persisted and verified before reporting the audit as recorded.
- If `wl` is not available or returns invalid JSON, report the error and do not claim success.
