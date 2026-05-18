---
name: ralph
description: "Run an iterative implement→audit loop for a target work item until scope reaches in_review and audit passes."
---

# Ralph

Use this skill when the operator asks to run `ralph <work-item-id>`.

## Command invocation and ID detection

The skill accepts a work-item id provided inline in the user's command. Supported invocation forms include:

- `/ralph <WORKITEM>`
- `ralph <WORKITEM>`
- `run ralph <WORKITEM>`

A work-item id is any short token matching the Worklog id pattern used in your environment (for example `WL-1234`, `CG-0MP12H40Q003Y7OU`, or an 8+ char identifier). When an id is present in the command the skill will use it and will not prompt for an id. If no id is detected the skill will ask the operator to provide one (or permission to create one).

## Behavior

1. Detect a work-item id in the invocation if present; otherwise ask the operator for an id (or permission to create one).
2. Validate the target stage is `plan_complete` or `in_review`; fail fast with an actionable message otherwise.
   - At `plan_complete`: run full implement→audit loop.
   - At `in_review`: skip the first implement pass and audit immediately. If audit fails, start the implement→audit loop.
3. Scope is target + direct children only.
4. Run iterative loop:
   - delegate implement pass via `pi -p --mode json --model <model> "implement <id> ..."`
   - run `pi -p --mode json --model <model> "/audit <id>"`
   - persist structured report via `wl update <id> --audit-text "..."` (uses --audit-file for large payloads)
   - append AMPA-style comment once per unique audit payload
   - if audit fails, instruct the implement pass with a short remediation prompt: "Address all the gaps identified in the audit." (the agent can read the audit stored on the work item)
5. Exit on success, cancellation, or max attempts.
6. On success run checks; offer merge and only execute merge when `--confirm-merge` is provided.

## CLI

Run deterministic script locally:

```bash
python skill/ralph/scripts/ralph_loop.py <work-item-id> [--max-attempts 10] [--check-cmd "pytest -q"] [--confirm-merge] [--verbose] [--quiet] [--model opencode-go/glm-5.1]
```

Example:

```bash
python skill/ralph/scripts/ralph_loop.py CG-0MP12H40Q003Y7OU --verbose --check-cmd "npm test -s"
```
