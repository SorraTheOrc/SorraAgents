---
name: ralph
description: "Run an iterative implement→audit loop for a target work item until scope reaches in_review and audit passes."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>`.

## Behavior

1. Validate input id is provided.
2. Validate target stage is `plan_complete`; fail fast with actionable message otherwise.
3. Scope is target + direct children only.
4. Run iterative loop:
   - delegate implement pass via `pi run "implement <id> ..."`
   - run `pi run "/audit <id>"`
   - persist structured report via `wl update <id> --audit-text "..."`
   - append AMPA-style comment once per unique audit payload
   - if audit has unmet/partial criteria, feed remediation into next implement pass
5. Exit on success, cancellation, or max attempts.
6. On success run checks; offer merge and only execute merge when `--confirm-merge` is provided.

## CLI

Run deterministic script:

```bash
python skill/ralph/scripts/ralph_loop.py <work-item-id> [--max-attempts 10] [--check-cmd "pytest -q"] [--confirm-merge]
```
