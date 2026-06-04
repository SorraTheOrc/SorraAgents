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

## Structured report (canonical)

The audit report MUST be a structured markdown block that begins with the exact header `Ready to close:` on the first line. Downstream orchestrators parse this block; do not include any prefix text before it.

Ready to close: Yes/No

## Summary

<concise 2-4 sentence summary of overall status, key findings, and whether the item can be closed>

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial | <file_path:line_number — one-line note> |

<If no acceptance criteria were found, write: "No acceptance criteria defined.">

## Children Status

### <child-title> (<child-id>) — <status>/<stage>

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | <criterion text> | met/unmet/partial | <file_path:line_number — one-line note> |

<If there are no children, write: "No children.">

## Success Criteria

"Success Criteria" is a synonym for "Acceptance Criteria". Both terms are treated equivalently in audit reports. Use **Acceptance Criteria** as the canonical heading; document **Success Criteria** as an accepted synonym where relevant.

## Exit Codes

- 0 – success (report printed to stdout)
- 1 – Worklog / CLI / Pi failure
- 2 – argument error

## Scripts (canonical runner & persister)

The audit skill ships a small, canonical runner and a persister. Use these from CI, local automation, or orchestrators.

- Runner: `skill/audit/scripts/audit_runner.py`
  - Usage: `python3 skill/audit/scripts/audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>] [--debug-log <file>]`
  - Usage: `python3 skill/audit/scripts/audit_runner.py project [--pi-bin pi] [--model <name>] [--debug-log <file>]`
  - Flags:
    - `--do-not-persist` — do not run persistence (useful for dry runs)
    - `--pi-bin` — path to the `pi` binary
    - `--model` — Pi model name
    - `--debug-log` — append Pi debug output to a JSONL file (helpful for triage)

- Persister: `skill/audit/scripts/persist_audit.py`
  - Persist from stdin: `cat report.md | python3 skill/audit/scripts/persist_audit.py --issue-id SA-123`
  - Persist from a file: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --file report.md`
  - Persist from a CLI string: `python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."`

Notes:
- The runner supports an optional persistence step. By default the runner will persist the generated structured audit into the work item unless invoked with `--do-not-persist`; use `--do-not-persist` for dry runs. Alternatively, the persister script (`skill/audit/scripts/persist_audit.py`) may be invoked explicitly to store the report. Both mechanisms perform the same `wl update` call and are the approved ways to persist an audit.
- The persister (and the runner when persisting) call: `wl update <issue-id> --audit-text "<report>" --json` and return a non-zero exit code on failure.

## Guidance for models

- Return a structured markdown report only. Use the header `Ready to close:` and the canonical sections above.
- If the model cannot determine acceptance criteria verdicts unambiguously, return immediately and do NOT persist or claim the audit was recorded.
- If the report is unambiguous and persistence is requested, you MAY persist the audit using one of the approved persistence mechanisms: the canonical runner (which persists by default unless invoked with `--do-not-persist`) or the persister script (`skill/audit/scripts/persist_audit.py`). When performing the authorized persistence step, annotate the prompt with `[PERSIST-AUDIT]` and ensure the report is final and complete.
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

- The most common problem is skipping persistence: always ensure `wl update --audit-text` executed successfully before reporting the audit as recorded.
- If `wl` is not available or returns invalid JSON, report the error and do not claim success.

