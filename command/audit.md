---
description: Run a full audit of a Worklog work item using the audit skill
tags:
  - workflow
  - audit
subtask: false
---

# Audit

## Description

Run a full audit for a provided Worklog work item using the audit skill (skill/status), returning a concise status summary, blockers, and next steps.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Results and Outputs

- A concise audit report for the work item (status, blockers, dependencies, and readiness to close).
- A final "Summary" section as required by the audit skill.

## Hard requirements

- Use the audit skill (skill/status) as the authoritative workflow for this command.
- Do not run destructive commands or change repository files.
- Ask for a valid work item id if $1 is missing or invalid.
- Respect ignore boundaries; do not include or quote ignored files.

## Behavior

The command delegates to the status skill and presents its results verbatim, ensuring the required "Summary" section is included and the output is concise and actionable.

## Process (must follow)

1. Validate inputs (agent responsibility)

- Check $1 for correct format; if invalid, ask a clarifying question and offer to proceed once a valid work-item-id is provided.
- Parse "$ARGUMENTS" for any scope hints (e.g., focus on blockers, dependencies, or readiness to close).

2. Execute audit (agent responsibility)

- Invoke the status skill (skill/status) for the provided work item.
- Ensure the audit includes: title, status, assignee, priority, description, blockers, dependencies, comments summary, related links, and open child work items.
- Ensure the audit concludes with the required "Summary" section.

3. Report results

- Provide the audit output.

## Traceability & idempotence

- The command is read-only; re-running yields updated audit results without creating new artifacts.

## Example invocation

```
/audit <work-item-id>
```

## Notes for implementors

- This command is a thin wrapper over the audit skill (skill/status); do not diverge from that skill's required output format.
