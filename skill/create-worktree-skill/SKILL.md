---
name: Create Worktree Skill
description: |
  Create an ephemeral, isolated worktree and branch for an agent to work on an identified issue.
---

## Purpose

Provide a simple, deterministic way for agents to create isolated worktrees and branches to work on an identified issue without contaminating runtime files at the repository root.

## Instructions

1. Prepare inputs
   - Required inputs:
     - work_item_id (string) — the work-item id (e.g. `SA-0ML0502B21WHXDYA`)
   - Optional (recommended):
     - agent_name (string) — short identifier for the agent (e.g. `testA`). If omitted the script derives a name from environment/git/whoami; passing an explicit agent_name is recommended for clarity and auditability.

2. Execute the skill
   - Run the canonical script: `skill/create-worktree-skill/scripts/run.sh <work_item_id> [agent_name]`

3. Observe outputs
   - The script prints status lines and writes diagnostics to `/tmp` on failure (e.g. `/tmp/wl_init_out`, `/tmp/wl_init_err`). It exits non-zero on unrecoverable failures.

## References to Bundled Resources

- scripts/run.sh — canonical orchestration script at `skill/create-worktree-skill/scripts/run.sh`.

## Examples

Example invocations:

```
# preferred: pass agent name explicitly
skill/create-worktree-skill/scripts/run.sh SA-0ML0502B21WHXDYA testA

# agent name omitted: script derives a value
skill/create-worktree-skill/scripts/run.sh SA-0ML0502B21WHXDYA
```

Expected result: a new worktree under `.worktrees/` is created (named `<agent>-<work_item_id>`), a branch `feature/<work_item_id>` (or a unique variant if the branch is checked out elsewhere) is created/checked-out, Worklog is initialized in the worktree if necessary, and `wl sync` publishes the state so other worktrees can see the branch.

## Security note

- Do not commit runtime `.worklog` DB files or `node_modules/` into the repository branches; the skill intentionally avoids copying runtime artifacts between worktrees.

## Examples for tests and CI

- Use the included integration test to validate the skill in CI. Ensure CI performs repo-level `wl init` or restores an initialized `.worklog` state before running the integration test.
