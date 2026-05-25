---
name: audit
description: "Run a deterministic audit of a Worklog work item or the overall project. Trigger on user queries such as: 'What is the current status?', 'Status of the project?', 'What is the status of <work-item-id>?', 'status', 'status <work-item-id>', 'audit', 'audit <work-item-id>'"
---

# Audit

## When To Use

- User asks for project status: "status", "audit", "status of the project", "audit the project".
- User asks about a specific work item: "status <id>", "audit <id>".

## Overview

The audit skill is automated via a Python runner. **Do not perform the audit manually** — invoke the runner instead. The runner handles all `wl` CLI interaction, Pi-based code review of acceptance criteria, report assembly, and optional persistence.

## Runner Invocation

### Audit a single work item

```bash
python3 skill/audit/scripts/audit_runner.py issue <id> [--persist] [--pi-bin <path>] [--model <name>]
```

### Audit the project

```bash
python3 skill/audit/scripts/audit_runner.py project [--pi-bin <path>] [--model <name>]
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--persist` | (off) | Persist the audit report to Worklog via `wl update <id> --audit-text`. Only valid for `issue` mode. |
| `--pi-bin` | `pi` | Path to the Pi binary. |
| `--model` | `opencode-go/glm-5.1` | Pi model to use for per-criterion review. |

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — report printed to stdout. |
| `1` | Worklog / CLI / Pi failure (e.g. `wl` command error, `pi binary not found`). |
| `2` | Argument error (e.g. missing subcommand). |

## Persistence

When `--persist` is supplied in `issue` mode, the runner delegates to `skill/audit/scripts/persist_audit.py`, which runs `wl update <issue-id> --audit-text "<report>" --json`. The runner propagates the exit code of the persistence step unchanged.

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
