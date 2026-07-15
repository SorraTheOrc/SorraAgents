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

- Follow the steps in order; do not skip.
- Keep implementation focused on meeting ACs with minimal changes.
- Never edit code outside `src/`, `tests/`, `docs/` unless essential config files.
- Never edit bundled libraries (`dist/`, `node_modules`).
- When implementing a CLI or API, provide JSON formatted output.
- Use work item comments to document process, decisions, and next steps.
- If not well-defined, mark as open and return a `no_safe_path` response.
- Follow AGENTS.md policies for branch naming, commit discipline, build→test→commit order, and PR policy. See [AGENTS.md](../../AGENTS.md#implement-the-work-item).

## Steps

Execute the following steps in order. Do not skip steps.

### Step 0 — Set status and safety gate

- **Before any other step**, claim the work item:
  `wl update <work-item-id> --status in_progress --json`
- Detect git context: `git rev-parse --is-inside-work-tree`
- Run `git status --porcelain=v1 -b`.
- **Inside a worktree:**
  - `.worklog/` changes only → carry forward.
  - Other changes → abort via `wl update <work-item-id> --status open --json` and return `no_safe_path`.
- **In the main checkout:**
  - `.worklog/` changes only → carry forward.
  - Other stale files → report, proceed to Step 2 for isolation.
  - If dirty files prevent branch creation → abort via `wl update ... --status open --json`.

### Step 1 — Understand the work item

- Claim: `wl update <work-item-id> --status in_progress --stage in_progress --assignee "<AGENT>" --json`
- Fetch details: `wl show <work-item-id> --json --children`
- Restate ACs, current status, constraints.
- Surface blockers, dependencies, missing requirements.
- Inspect linked PRDs, plans, or docs.

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

- **Write tests first** (TDD): create at least one test file before editing implementation code. Tests may fail initially; implement code to make them pass. If external constraints prevent complete tests, use harnesses/mocks and document the limitation.
- Write implementation code to meet ACs.
- Make minimal, focused changes.
- Follow project style and conventions.
- Comment on significant design decisions in the work item.

### Step 4 — Optional refactor step

After implementation completes and before building/testing, an automated
refactor step may be invoked to detect and remediate code smells:

- The refactor step analyzes only files modified in the current session
  (git diff against parent branch).
- **Session-introduced smells** are fixed immediately in the same run.
- **Pre-existing smells** create Worklog work items with structured
  REFACTOR comments in the source files.
- The step can be skipped with the ``--no-refactor`` flag.
- See ``../refactor/SKILL.md`` for full documentation.

### Step 5 — Build, test and commit

- Build the project and verify no errors.
- Run the entire test suite. Fix any failing tests.
- Commit changes with a message referencing the work item id.
- Add a comment to the work item with the commit hash.

### Step 6 — Push and mark in-review

- Push the branch to `origin`.
- Respond with: `<work-item-id>: <concise-summary>\n\nWork committed to dev`
- Mark in-review: `wl update <work-item-id> --status completed --stage in_review --json`
- Do NOT create a PR or merge. Ralph handles PR/merge externally.

## Status Transition Matrix

| Phase | Command | Status | Stage |
|-------|---------|--------|-------|
| Start (Step 0) | `wl update <id> --status in_progress --json` | in_progress | (unchanged) |
| Claim (Step 1) | `wl update <id> --status in_progress --stage in_progress ...` | in_progress | in_progress |
| Complete (Step 6) | `wl update <id> --status completed --stage in_review --json` | completed | in_review |
| Abort (dirty/no_safe_path) | `wl update <id> --status open --json` | open | (unchanged) |

Abort/failure transitions always use `--status open`.

## Scripts

This skill does not ship a canonical CLI runner. Prefer any repository-provided implement helper script.

```bash
# Fetch work item
wl show <work-item-id> --json --children
# Mark in_review when complete
wl update <work-item-id> --status completed --stage in_review --json
```

End.
