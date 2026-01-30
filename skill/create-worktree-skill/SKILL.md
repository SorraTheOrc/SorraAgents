---
name: Create Worktree Skill
description: |
  Create an ephemeral, isolated worktree and branch for an agent to make a small, auditable repo change and publish it with `wl sync`.
---

## Purpose

Provide a simple, deterministic way for agents to create isolated worktrees and branches so they can make small, auditable repository changes without contaminating runtime files at the repository root.

## Instructions

1. Prepare inputs
   - Required inputs:
     - work_item_id (string) — the work-item id (e.g. `SA-0ML0502B21WHXDYA`)
     - agent_name (string) — short identifier for the agent (e.g. `testA`)
   - Note: `short_suffix` is optional when invoking the script; if omitted the script derives a suffix from the work-item id or uses `it` as a default.

2. Execute the skill
   - Run the canonical script: `skill/create-worktree-skill/scripts/run.sh <work_item_id> <agent_name> [short_suffix]`

3. Observe outputs
   - The script prints status lines and writes diagnostics to `/tmp` on failure (e.g. `/tmp/wl_init_out`, `/tmp/wl_init_err`). It exits non-zero on unrecoverable failures.

## References to Bundled Resources

- scripts/run.sh — canonical orchestration script at `skill/create-worktree-skill/scripts/run.sh`.

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
