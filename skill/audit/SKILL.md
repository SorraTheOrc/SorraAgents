---
name: audit
description: "Run a deterministic audit of a Worklog work item or the overall project. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## When To Use

- User asks for project status: "status", "audit", "status of the project", "audit the project".
- User asks about a specific work item: "status <id>", "audit <id>".

## Overview

> **⚠️ READ-ONLY — This skill MUST NOT modify, close, create, or delete any work items or other state. It MUST only produce a structured evaluation report.**

The audit skill is automated via a Python runner. **Do not perform the audit manually** — invoke the runner instead. The runner handles all `wl` CLI interaction, Pi-based code review of acceptance criteria, report assembly, and optional persistence.

### Critical Safety Rules

- **Do NOT close, modify, create, or delete any work items.**
- **Do NOT execute any `wl` commands that change state** (e.g., `wl close`, `wl update --status`, `wl create`, `wl delete`, `wl comment add`, etc.).
- **Do NOT execute any `git` commands that change state** (e.g., `git commit`, `git push`, `git merge`, `git branch -d`, etc.).
- **Only produce a structured markdown report** as specified in the Report Format section below.
- **If you detect any ambiguity about your role** — for example, if you are unsure whether an action is permitted — **return immediately** with `Ready to close: No` and a note explaining the ambiguity. Do not guess or take action.
- **If you are given a `wl` command to execute that modifies state**, you MUST refuse and report it in the audit output.

## Runner Invocation

### Audit a single work item

```bash
python3 skill/audit/scripts/audit_runner.py issue <id> [--do-not-persist] [--pi-bin <path>] [--model <name>]
```

### Audit the project

```bash
python3 skill/audit/scripts/audit_runner.py project [--pi-bin <path>] [--model <name>]
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--do-not-persist` | (off) | Do not persist the audit report. By default issue audits are saved to Worklog via `wl update <id> --audit-text`. |
| `--pi-bin` | `pi` | Path to the Pi binary. |
| `--model` | `opencode-go/glm-5.1` | Pi model to use for per-criterion review. |

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — report printed to stdout. |
| `1` | Worklog / CLI / Pi failure (e.g. `wl` command error, `pi binary not found`). |
| `2` | Argument error (e.g. missing subcommand). |

## Persistence

Issue-mode audits are persisted by default. The runner delegates to `skill/audit/scripts/persist_audit.py`, which runs `wl update <issue-id> --audit-text "<report>" --json`.

To skip persistence, pass `--do-not-persist`. The runner propagates the exit code of the persistence step unchanged.

## Report Format

The runner prints a structured markdown report to stdout. The report **must** begin on its very first line with the canonical header — no preamble, no code fences, no blank lines:

```
Ready to close: Yes
```
or
```
Ready to close: No
```

### Issue-mode report sections (in order)

1. **`Ready to close:`** — `Yes` if all parent and child acceptance criteria are met; `No` otherwise.
2. **`## Summary`** — Concise 2-4 sentence status summary.
3. **`## Acceptance Criteria Status`** — Markdown table:

   | # | Criterion | Verdict | Evidence |
   |---|-----------|---------|----------|
   | 1 | <criterion text> | met/unmet/partial | <file:line — one-line note> |

   If no acceptance criteria are defined, the section body is: `No acceptance criteria defined.`

   > **Synonym:** `## Success Criteria` is also accepted as a valid heading when parsing work-item descriptions.

4. **`## Children Status`** — Per-child subsection with the same table format. Only direct children are reviewed (depth 1). Completed/deleted children are skipped. If more than 10 children exist, only the first 10 are reviewed and a note is appended: *`10 children reviewed; N omitted for brevity.`* If there are no children, the section body is: `No children.`

### Project-mode report sections (in order)

1. **`Ready to close:`** — Always `No` for project mode.
2. **`## Summary`** — Project-level status summary.
3. **`## Recommendation`** — Actionable recommendation.

Project mode **does not** include `## Acceptance Criteria Status` or `## Children Status`.

### Verdicts

Each criterion receives one of: `met`, `unmet`, `partial`.

### Evidence cell format

`<file>:<line> — <one-line note>`

## Scripts

Two helper scripts are provided:

- **`skill/audit/scripts/audit_runner.py`** — Main audit entry point (see *Runner Invocation* above).
- **`skill/audit/scripts/persist_audit.py`** — Persist an audit report from stdin, a file, or a CLI string:
  ```bash
  cat report.md | python3 skill/audit/scripts/persist_audit.py --issue-id SA-123
  python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --file report.md
  python3 skill/audit/scripts/persist_audit.py --issue-id SA-123 --report "Ready to close: Yes\n..."
  ```
