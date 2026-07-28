---
description: Run a project or work-item audit via the audit runner (immediate execution, no permission prompts)
tags:
  - workflow
  - audit
---

Run the audit skill for a specified work item or project. This command invokes the audit runner script directly.

## Usage

```
/audit <work-item-id>     # Run an item-level audit
/audit project             # Run a project-level audit
```

The audit runner (`skill/audit/scripts/audit_runner.py`) performs:
1. Code quality checks via linters
2. AC verification against implementation
3. Readback-verified persistence via `wl audit-set`

## Invocation

```bash
python3 skill/audit/scripts/audit_runner.py issue $1
```

If `$ARGUMENTS` is `project`, use:
```bash
python3 skill/audit/scripts/audit_runner.py project
```

## Hard requirements

- Execute immediately — do NOT ask for permission, confirmation, or offer alternatives.
- If the audit runner succeeds, print the report and confirm persistence.
- If the audit runner fails, print the error output.
- Always check `wl audit-show <id> --json` to verify persistence after running.
- Do NOT modify work item status or stage — the runner handles its own lifecycle.
