---
name: Create Worktree Skill
description: |
  Create an ephemeral worktree, create or check out a branch for a work-item, make a sample commit, and publish changes via `wl sync`.
  This skill encapsulates the safe, non-interactive flow required by agents to create isolated worktrees under `.worktrees/`, initialize Worklog in the new worktree, and ensure repo-level visibility.
---

## Purpose

Provide a reusable, auditable skill that performs the following agent-level workflow:

- Create a unique ephemeral worktree under `.worktrees/` (avoid copying runtime `.worklog` files)
- Create or check out a branch named using the repository convention (e.g. `feature/SA-<id>-<suffix>`)
- Make a small agent commit (agent metadata + sample file)
- Run `wl init --json` when necessary in the worktree, then `wl sync` to publish changes

This skill is intended to be used by agents that need to perform repository-level work on behalf of a work-item without contaminating the repository root runtime files.

## Instructions

1. Prepare inputs
   - Required inputs:
     - work_item_id (string) — the work-item id (e.g. `SA-0ML0502B21WHXDYA`)
     - agent_name (string) — short identifier for the agent (e.g. `testA`)
     - short_suffix (string) — short suffix for branch naming (e.g. `it`)
   - Optional environment variables:
     - WORKLOG_SKIP_POST_PULL=1 — skipped by the script when creating worktrees to avoid hooks running `wl sync` prematurely

2. Execute the skill
   - Run the bundled script: `skill/create-worktree-skill/run.sh <work_item_id> <agent_name> <short_suffix>`
   - The script will:
     - Create a unique directory under `.worktrees/` using `mktemp`
     - Create/checkout a branch (or a unique variant if the branch is already checked out)
     - Initialize Worklog in the new worktree non-interactively using `wl init --json` with defaults copied from repo `.worklog/config.yaml` when present
     - Commit agent metadata and a sample file
     - Run `wl sync` and retry init if the worktree reports uninitialized

3. Observe outputs
   - The script prints status lines and writes diagnostic files under `/tmp` on failure (e.g. `/tmp/wl_init_out`, `/tmp/wl_init_err`). The script exits non-zero on unrecoverable failures.

## References to Bundled Resources

- scripts/run.sh — executable orchestration script (the canonical implementation lives at `skill/create-worktree-skill/run.sh`). Note: the executable script in the repository root is the authoritative implementation; the scripts/ folder can host helpers if needed.
- tests/integration/agent-worktree-visibility.sh — integration harness that exercises the happy path

## Examples

- Example invocation (happy path):

```
skill/create-worktree-skill/run.sh SA-0ML0502B21WHXDYA testA it
```

Expected result: a new unique worktree under `.worktrees/` is created, a branch `feature/SA-0ML0502B21WHXDYA-it(-<uniq>)` is created/checked-out, a sample commit is made, and `wl sync` publishes the commit so other worktrees can see it.

## Security note

- Do not commit runtime `.worklog` DB files or `node_modules/` into the repository branches; the skill intentionally avoids copying runtime artifacts between worktrees.

## Examples for tests and CI

- Use the included integration test to validate the skill in CI. Ensure CI performs repo-level `wl init` or restores an initialized `.worklog` state before running the integration test.
