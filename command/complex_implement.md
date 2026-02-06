---
description: Implement a Worklog work item
tags:
  - workflow
  - implement
agent: patch
subtask: true
---

# Implement

## Description

You are implementing a Worklog work item identified by a provided id. You will fully implement this work item and all dependent work required to satisfy its acceptance criteria, following project rules and best practices.

## Inputs

- The supplied <work-item-id> is $1.
  - If no valid <work-item-id> is provided (ids are formatted as '<prefix>-<hash>'), ask the user to provide one.
- Optional additional freeform arguments may be provided to guide your work. Freeform arguments are found in the arguments string "$ARGUMENTS" after the <work-item-id> ($1).

## Hard requirements

- Whenever you are recommending next steps you MUST make the first one a progression to the next step in the process defined below, with a summary of what that step involves.

## Results and Outputs

- Tests written to validate acceptance criteria.
- Implementation code satisfying acceptance criteria.
- If any refactoring smells were identified new work items have been created with stage "idea".
- A merged Pull Request created against the repository's default branch.

## Prerequisites & project rules

- Use `wl` for ALL task tracking; do not create markdown TODOs.
- Keep changes between `git push` invocations minimal and scoped to the work item.
- Always run tests and validation before committing changes.
- Use a git branch + PR workflow (no direct-to-main changes).
- Ensure the working branch is pushed to `origin` before finishing.
- Do NOT close the Worklog work item until the PR is merged.

Live context commands (use to gather runtime state)

- `wl show <work-item-id> --json`
- `git status --porcelain=v1 -b`
- `git rev-parse --abbrev-ref HEAD`
- `git remote get-url origin`

## Procedure

Implement $1 using the implement skill.
