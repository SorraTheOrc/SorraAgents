---
name: implement
description: Implement a Worklog work item by writing code, tests and documentation to meet acceptance criteria, following a deterministic workflow. Trigger on user queries such as: 'Implement <work-item-id>', 'Complete <work-item-id>', 'Work on <work-item-id>'.
---

## Purpose

Provide a deterministic, step-by-step implementation workflow for completing a
Worklog work item.

Use this skill when asked to implement a specific wl work-item id
(formatted `<prefix>-<hash>`). The skill assumes the work-item id will be
supplied as an input and that the agent has access to `wl`, `git`, and the
repository.

## Instructions

- Input: a valid work-item id (e.g. `WL-123`). If missing or invalid, prompt
  for the id. Treat the work-item id as the primary required input (equivalent
  to `$1` in the canonical command implementation).

Follow the numbered steps exactly. Use the listed live commands where
applicable and record outputs in the work-item comments as you proceed.

0. Safety gate: handle dirty working tree

- Inspect the working tree with `git status --porcelain=v1 -b`.
- If uncommitted changes are limited to `.worklog/`, carry them into the new
  branch and commit there.
- If other uncommitted changes exist, present explicit choices: carry into the
  work-item branch, commit first, stash (and optionally pop later), revert/
  discard (explicit confirmation), or abort.

1. Understand the work item

- Claim the work item: run
  `wl update <id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`
  (omit `--assignee` if not applicable).
- Fetch the work item JSON: `wl show <id> --json`.
- Restate acceptance criteria and constraints extracted from the work item
  JSON in the worklog comment and in your local plan.
- Surface blockers, dependencies and missing requirements; inspect linked PRDs
  and docs.
- Confirm expected tests or validation steps.

  1.1) Definition gate (must pass before implementation)

- Verify clear scope, concrete testable acceptance criteria, constraints, and
  unknowns captured as explicit questions.
- If the work item is not well-defined, run the intake interview (see
  `.opencode/command/intake.md`) and update the work item description or
  acceptance criteria. If too large, run milestones or plan interviews to
  break it into smaller work items.

2. Create a working branch

- Inspect the current branch: `git rev-parse --abbrev-ref HEAD`.
- If the current branch is already for an ancestor work item, continue on it.
- Otherwise create a new branch named `feature/<id>-<short>` or
  `bug/<id>-<short>` (include the work item id). Never commit directly to
  `main`.

3. Implement

- If blockers or dependencies exist, select and claim the next appropriate
  work-item and recursively implement it according to this skill.
- Write tests and code to meet acceptance criteria. Keep changes minimal and
  focused. Use TDD where appropriate.
- Create linked work items for additional discovered work: `wl create
"<title>" --deps discovered-from:<id> --json`.
- When satisfied, run the full test suite and fix failing tests before
  continuing. Update documentation and summarise changes in the work item
  comments.
- Pause and request operator confirmation before proceeding to automated PR
  creation if interactive approval is required by your environment.

4. Automated self-review

- Run `audit <id>` to confirm acceptance criteria (if audit tooling exists).
- Perform sequential self-review passes: completeness, dependencies & safety,
  scope & regression, tests & acceptance, polish & handoff.
- For each pass produce a short note and limit edits to small, goal-aligned
  changes. If intent changes are discovered, create an Open Question and stop
  automated edits.
- Re-run the full test suite and fix failing tests.

5. Commit, Push and create PR

- Update work item status to completed/in-review: `wl update <id> --status
completed --stage in_review --json`.
- Push the branch to origin and create a Pull Request against the repository's
  default branch.
- Use the PR title `WIP: <work item title> (<work item id>)` and include a
  concise body summarising goal, work done, and reviewer instructions.
- Link the PR to the work item via a comment and `wl` as appropriate.

6. Human PR review

- Notify reviewers that the PR is ready.
- Address review comments and requested changes.
- After merge, proceed to cleanup.

7. Cleanup (post-merge)

- After the PR is merged, close the work item and its dependents: `wl update
<work-item-id> --status completed --stage completed --json`.
- Run the cleanup skill to prune branches and finalize local state.

## Inputs

- work-item id: required. Validate id format `<prefix>-<hash>` and prompt if
  missing.
- Optional freeform guidance in the arguments string may be used to shape the
  implementation approach.

## Outputs

- Tests and implementation code meeting acceptance criteria (committed to a
  branch and pushed to origin).
- Pull Request URL and work item comments referencing the PR and summarising
  work.

## References to Bundled Resources

- Intake/interview helpers: `.opencode/command/intake.md`,
  `.opencode/command/plan.md`.

Security note: Do not push or create PRs automatically unless the invoking
agent has explicit permission to push to the repository and open pull
requests. Require explicit confirmation before performing remote actions
(push/pr creation) when operating without an operator-approved credential.
When in doubt, produce the exact `git`/`gh`/`wl` commands for a human to run.

Privacy note: Avoid including secrets, tokens, or personally-identifiable data
in work item comments or PR bodies. If such data must be referenced, reference
it by work-item id or document path instead of pasting values. Mask or redact
any sensitive values before writing them to logs or comments.

## Examples

- Example 1 — Full implementation (canonical)
  - Input: work-item id `WL-456`.
  - Steps:
    1. Claim: `wl update WL-456 --status in_progress --stage in_progress --assignee "implement-agent" --json`
    2. `wl show WL-456 --json` and restate acceptance criteria.
    3. Create branch: `git checkout -b feature/WL-456-add-orc-parser`.
    4. Implement tests and code, run test suite.
    5. Self-review and audit: `audit WL-456`.
    6. `wl update WL-456 --status completed --stage in_review --json`.
    7. Push and create PR: `gh pr create --title "WIP: Add ORC parser (WL-456)" --body "$BODY"`.
    8. Add PR link to WL-456 comments and wait for human review.
    9. After merge, `wl update WL-456 --status completed --stage completed --json` and run cleanup.
