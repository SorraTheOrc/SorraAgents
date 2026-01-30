# Agent worktree bug

Use this template when reporting issues discovered while running the agent that creates isolated worktrees.

**Summary**

Brief summary of the problem.

**Steps to reproduce**

1. Command run (include flags):
2. Environment (local/CI, OS, git version):
3. Any additional context or files changed before running:

**Expected behavior**

Describe what you expected to happen.

**Actual behavior**

Describe what actually happened. Include error messages, stack traces, logs.

**Logs / attachments (required)**

- Agent stdout/stderr
- `git status --porcelain=v1 -b` from both caller and worktree
- `git worktree list --porcelain`
- Short reproduction script or CI job id

**Additional notes**

If you created a minimal reproduction, link to it or attach it here.
