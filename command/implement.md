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

## Behavior

The command implements the procedural workflow below. Each numbered step is part of the canonical execution path; substeps describe concrete checks or commands that implementors or automation should run.

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

0. Safety gate: handle dirty working tree

- Inspect `git status --porcelain=v1 -b`.
- If uncommitted changes are limited to `.worklog/`, carry them into the new working branch and commit there.
- If other uncommitted changes exist, pause and present explicit choices: carry them into the work item branch, commit first, stash (and optionally pop later), revert/discard (explicit confirmation), or abort.

1. Understand the work item

- Claim by running `wl update $1 --status in_progress --stage in_progress --assignee "<AGENT>" --json` (omit `--assignee` if not applicable).
- Fetch the work item JSON if not already present: `wl show $1 --json` and `wl show $1 --json`.
- Restate acceptance criteria and constraints from the work item JSON.
- Surface blockers, dependencies and missing requirements.
- Inspect linked PRDs, plans or docs referenced in the work item.
- Confirm expected tests or validation steps.

  1.1) Definition gate (must pass before implementation)

- Verify:
  - Clear scope (in/out-of-scope).
  - Concrete, testable acceptance criteria.
  - Constraints and compatibility expectations.
  - Unknowns captured as explicit questions.
- If the work item is not well-defined, run the intake interview to update the existing work item (see `command/intake.md`) and update the work item `description` or `acceptance` fields with the intake output.
- If the work item is too large to implement in one pass, run the milestones interview for items that should be epics (see `command/milestones.md`) or the plan interview (see `command/plan.md`) to break it into smaller work items, create those work items, link them as blockers/dependencies, and pick the highest-priority work item to implement next.
- If you ran the intake interview, update the current work item with the new definition and inform the user of your actions and ask if you should restart the implementation review.
- If you ran the milestone interview convert this work item to an epic and inform the user that implementation should move to first milestone work item created.
- if you ran the plan interview you can proceed.

2. Create a working branch

- inspect the current branch name via `git rev-parse --abbrev-ref HEAD`.
- If the current branch was created for a work item that is an ancestor of $1, continue on that branch (that is if the name has an ancestor work item id).
- Otherwise create a new branch named `feature/$1-<short>` or `bug/$1-<short>` (include the work item id).
- Never commit directly to `main`.

3. Implement

- If the work item has any open or in-progress blockers or dependencies:
  - Select te most appropriate work item to work on next (blocker > dependency; most critical first).
  - Claim the work item by running `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`
  - Recursively implement that work item as described in this procedure.
  - When a work item is completed commit the work and update the stage: `wl update <work-item-id> --stage in_review --json`
- Write tests and code to ensure all acceptance criteria defined in or related to the current work item are met:
  - Make minimal, focused changes that satisfy acceptance criteria.
  - Follow a test-driven development approach where applicable.
  - Ensure code follows project style and conventions.
  - Add comments to the work item describing any significant design decisions, code edits or tradeoffs.
  - If additional work is discovered, create linked work items: `wl create "<title>" --deps discovered-from:$1 --json`
- Once all acceptance criteria for the primary work item and all blockers and dependents are met:
  - Run the entire test suite.
    - Fix any failing tests before continuing.
  - Update or create relevant documentation.
  - Summarize changes made in the work item description or comments.
  - Do not proceed to the next step until the user confirms it is OK to do so.

4. Automated self-review

- Audit the work item to confirm all acceptance criteria are met: `audit $1`.
  - If the audit reveals any unmet acceptance criteria, inform the user of the findings and return to step 3 to address them.
- Perform sequential self-review passes: completeness, dependencies & safety, scope & regression, tests & acceptance, polish & handoff.
- For each pass, produce a short note and limit edits to small, goal-aligned changes. If intent changes are discovered, create an Open Question and stop automated edits.
- Run the entire test suite.
  - Fix any failing tests before continuing.

5. Commit, Push and create PR

- Commit the work item to completed/in-review with `wl update $1 --status completed --stage in_review --json`
- Push the branch to `origin`.
- Create a Pull Request against the repository's default branch.
  - Use a title in the form of "WIP: <work item title> (<work item id>)" and a body that contains a concise summary of the goal and of the work done and reviewer instructions.
  - Link the PR to the work item via a comment .

6. Human PR review

- Notify the human reviewer(s) that the PR is ready for review.
- Address any review comments or requested changes.
- Once merged, proceed to the next step.

7. Cleanup

- Only take the following actions after the PR is merged:
- Close the work item and all its dependents and blockers by running `wl update <work-item-id> --status done --stage done --json`.
- Cleanup using the cleanup skill

## Exit codes & errors

- Exit non-zero when missing or invalid arguments.
- Error messages (verbatim where useful):
  - `Error: missing work item id. Run implement <work-item-id>.`
  - `Error: work item $1 is not actionable — missing acceptance criteria. Run the intake interview to update the work item before implementing.`

S
