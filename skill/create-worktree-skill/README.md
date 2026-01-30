Create Worktree Skill
=====================

This skill provides an audited, agent-friendly workflow for creating ephemeral worktrees and branches for working on a work-item.

Key behavior
- Create unique worktrees under `.worktrees/` using `mktemp`
- Create or checkout feature branches following the repo convention
- Initialize Worklog in the worktree non-interactively and run `wl sync`
- Avoid copying runtime `.worklog` files between worktrees

Usage
-----

Run the script:

```
skill/create-worktree-skill/scripts/run.sh <work_item_id> <agent_name> [short_suffix]
```

Examples and CI
---------------
- CI note: ensure a repo-level `wl init` or restore an initialized `.worklog` state prior to running integration tests that exercise this skill.
