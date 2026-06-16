---
name: implement-single
description: |
  Write tests, docs and code for a single, specific Worklog work item.
  Unlike the `implement` skill, this skill operates on exactly one work-item
  without using `wl next` for recursive dependency resolution or sub-task
  discovery. It is designed to be invoked by Ralph's per-child loop so that
  each child is implemented, audited, and remediated independently.
  Trigger on user queries such as: 'implement-single <work-item-id>',
  'complete <work-item-id> (single)', or when Ralph delegates a single-child
  implement step.
---

## Purpose

Provide a deterministic, step-by-step implementation workflow for completing
a single Worklog work item. This skill does **not** use `wl next` for
recursive dependency resolution — it operates on exactly the work-item id
provided. Any blockers or dependencies must be resolved before this skill
is invoked.

## Inputs

- work-item id: **required**. Must be a single, explicit Worklog id (e.g.
  `SA-0MPFD4SPC000MXWH`). No sub-task discovery or `wl next` invocation.

## Outputs

- Tests and implementation code meeting acceptance criteria (committed to a
  branch and pushed to origin).
- Work item comment summarising work done.
- Work item marked as `in_review` when complete.

## Constraints

- **No `wl next` invocation.** This skill works on exactly the work-item id
  provided. Do not attempt to discover or implement child items, dependencies,
  or related work items.
- **No interactive questions.** The implementing agent must never ask the
  producer questions or pause for interactive input. If it cannot continue
  safely without explicit producer input, it must first run
  `wl update <work-item-id> --status open --json`, then return a structured
  `no_safe_path` response with the missing decision.
- **No merge.** Do not merge changes into main. Only commit to a feature branch.

## Best Practices

- Follow the steps in order and do not skip steps.
- Keep implementation focused on meeting acceptance criteria with minimal changes.
- Never edit code outside of `src/`, `tests/` and `docs/` for this project unless
  they are essential configuration files.
- Never edit code in bundled libraries such as `dist/` and `node_modules`.
- When implementing a CLI or API always provide a way to obtain a JSON
  formatted output for agents to consume.
- Use work item comments to document your process, decisions, and next steps.
- If the work item is not well-defined, do not proceed with implementation.
  Instead, first run `wl update <work-item-id> --status open --json` to mark
  the item as open, then return a `no_safe_path` response describing the
  missing information.
- Never commit directly to `main`. Always create a feature or bug branch for
  implementation.
- The required order of operations before any commit is always:
  **build → test → commit**. First build the project and verify no errors, then
  run all tests and verify they pass, and only then commit.
- When creating branches, include the work item id in the branch name for
  traceability (e.g., `feature/WL-123-add-auth`).
- When creating a commit message, review the diff and write a concise message
  summarizing the changes made and the reason for the change, referencing the
  work item id.
- When committing add a comment to the work item with the commit message and hash.
- Only create a PR when all acceptance criteria have been met, all tests have
  passed and the implementation is ready for review. Do not create PRs for
  work in progress.
- Do not escape content in the PR or work-item description; use markdown
  formatting as needed for clarity and readability.

## Steps

Execute the following steps in order. Do not skip steps.

### Step 0 — Safety gate

- Inspect `git status --porcelain=v1 -b`.
- If uncommitted changes exist, either carry them into the work item branch
  (if limited to `.worklog/`) or abort: first run
  `wl update <work-item-id> --status open --json` to mark the item as open,
  then return a structured `no_safe_path` response.

### Step 1 — Understand the work item

- Claim the work item by running
  `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`.
- Fetch the work item details: `wl show <work-item-id> --json --children`
- Restate acceptance criteria and current status along with any constraints.
- Surface blockers, dependencies and missing requirements.
- Inspect linked PRDs, plans or docs referenced in the work item.

### Step 2 — Create a working branch

- If the prompt specifies a `parent_branch` to use, check out that branch
  via `git checkout <parent_branch>` and proceed to Step 3. Do NOT create
  a new branch when a parent branch is provided.
- Otherwise, inspect the current branch name via `git rev-parse --abbrev-ref HEAD`.
- If the current branch was created for this work item, continue on it.
- Otherwise create or switch to a branch named
  `feature/<work-item-id>-<short>` or `bug/<work-item-id>-<short>`.
- Never commit directly to `main`.

### Step 3 — Implement

- Write tests first (test-driven development approach):
  - Create at least one new test file before adding or editing implementation code.
  - Tests created in this step are allowed to fail on first run; the agent must then implement code to make them pass before committing.
  - If tests cannot be completed due to external constraints (e.g., unavailable external service, missing infrastructure), create harnesses or mocks that enable the tests to run. The tests should fail due to the external constraint, not because of missing implementation logic.
  - When a harness, mock, or test placeholder is used, include an explicit note in the work item comment and in the test file header stating the reason for the limitation and marking it as a temporary placeholder.
- Write implementation code to meet acceptance criteria.
- Make minimal, focused changes.
- Follow project style and conventions.
- Add comments to the work item describing any significant design decisions.

### Step 4 — Optional refactor step

After implementation completes and before building/testing, an automated
refactor step may be invoked to detect and remediate code smells:

- The refactor step analyzes only files modified in the current session
  (git diff against parent branch).
- **Session-introduced smells** are fixed immediately in the same run.
- **Pre-existing smells** create Worklog work items with structured
  REFACTOR comments in the source files.
- The step can be skipped with the ``--no-refactor`` flag.
- See ``skill/refactor/SKILL.md`` for full documentation.

### Step 5 — Build, test and commit

- Build the project and verify no errors.
- Run the entire test suite. Fix any failing tests.
- Commit changes with a message referencing the work item id.
- Add a comment to the work item with the commit hash.

### Step 6 — Push and mark in-review

- Push the branch to `origin`.
- Close your response to the operator with:
  `<work-item-id>: <concise-summary-of-changes>\n\nWork committed to dev`
- Mark the work item as in-review:
  `wl update <work-item-id> --status open --stage in_review --json`
- Do not create a PR or merge. Ralph will handle PR/merge externally.

## Status Transition Matrix

The following table documents the expected status and stage transitions at each workflow phase for the `implement-single` skill.

| Phase | Command | Status | Stage |
|-------|---------|--------|-------|
| Start (Step 1 - Claim) | `wl update <id> --status in_progress --stage in_progress --assignee "<AGENT>" --json` | in_progress | in_progress |
| Complete (Step 6) | `wl update <id> --status open --stage in_review --json` | open | in_review |
| Abort - dirty tree (Step 0) | `wl update <id> --status open --json` | open | (unchanged) |
| Abort - no_safe_path (Step 0) | `wl update <id> --status open --json` | open | (unchanged) |

Abort/failure transitions use `--status open` while keeping the stage unchanged.

## Scripts (canonical runner & modules)

This skill does not ship a canonical CLI runner. When present, prefer any
repository-provided implement helper script to ensure deterministic behavior.

Usage (work-item example):

```bash
# Fetch the single work item for context
wl show SA-0MPYMFZXO0004ZU4 --json --children

# After completing implementation, mark the item in_review
wl update SA-0MPYMFZXO0004ZU4 --status open --stage in_review --json
```

End.
