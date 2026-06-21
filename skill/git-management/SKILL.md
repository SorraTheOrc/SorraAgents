---
name: git-management
description: "Unified git management skill that orchestrates the full feature-branch lifecycle — create, commit, push, PR, merge, cleanup — for both AI agents and human operators."
---

# Git Management Skill

Unified git management skill that orchestrates the full feature-branch lifecycle
for AI agents and human operators.

## Purpose

Provide a deterministic, safe, and auditable set of git operations covering the
complete feature-branch lifecycle:

1. **Worktree creation** — create a worktree with a feature branch for isolated agent work.
2. **Commit** — stage changes and create conventional, Worklog-linked commit messages.
3. **Push** — push to remote with safety checks (no force-push, no direct-to-main).
4. **PR creation** — create GitHub Pull Requests from feature branches.
5. **Merge** — guarded merge with CI status verification.
6. **Cleanup** — post-merge branch pruning and worktree cleanup via existing infrastructure.
7. **Workflow orchestration** — single entry point for the full lifecycle.

## When To Use

- An agent needs to create a feature branch from a work item.
- An agent needs to commit changes with proper formatting and work-item references.
- An agent needs to push to a remote branch with safety validation.
- An agent or operator needs to create a PR, merge it, and clean up.
- An agent or operator needs a single command to drive the full lifecycle.

## Prerequisites

- **git** CLI installed and configured
- **Node.js** 18+ (for JavaScript scripts)
- **gh** CLI (for PR/merge operations — optional for push-only workflows)
- **wl** CLI (Worklog — for work-item context)

## How Agents Invoke This Skill

```
/skill:git-management <action>
```

Where `<action>` is one of:

| Action | Description |
|--------|-------------|
| `create-branch <work-item-id> <short-desc>` | Create a worktree with a canonical feature branch (see [[concepts/git-worktree-best-practices-for-agent-workflows]]) |
| `commit --message <msg> --work-item <id> [--all] [--dry-run]` | Stage and commit with conventional format |
| `push [--remote <name>] [--into-dev] [--dry-run]` | Push current branch with safety checks |
| `create-pr [--base <branch>] [--title <title>]` | Create a GitHub PR from the current branch |
| `merge-pr <pr-number> [--delete-source]` | Merge an approved PR with CI checks |
| `cleanup [--dry-run] [--days <n>]` | Post-merge branch pruning and worktree cleanup |
| `workflow <work-item-id> <short-desc> [--dry-run] [--phase <name>]` | Full lifecycle orchestration |
| `help` | Print this documentation |

## Safety Constraints

### Force-push prohibition

- No script in this skill performs force-push or history-rewrite operations.
- Any request for `--force` or equivalent is rejected with a non-zero exit status.

### Protected branch protection

- Direct pushes to `main`, `master`, or `HEAD` are always rejected.
- Branch deletion of protected branches is always skipped.
- Use `isBranchBlocked()` from `skill/ship/scripts/git-helpers.js` for validation.

### Branch naming

- All agent-created branches MUST follow `wl-<work-item-id>-<short-desc>`.
- Use `makeBranchName()` from `skill/ship/scripts/git-helpers.js` for generation.
- Use `validateBranchName()` for validation.

### Worktree naming

- Worktree names follow the same pattern as branch names: `wl-<work-item-id>-<slug>`.
- Worktrees are created under `.worklog/worktrees/`.
- See [[concepts/git-worktree-best-practices-for-agent-workflows]] for the naming and lifecycle conventions, and [AGENTS.md](../../AGENTS.md) for the top-level policy.

### CI gate for merges

- `merge-pr` verifies CI/status checks before merging.
- Merges are skipped or fail if CI is pending or failing.

## Script Contract

All scripts in `skill/git-management/scripts/` follow these conventions:

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (missing arguments, invalid state) |
| 2 | Safety violation (protected branch, force-push request) |
| 3 | Prerequisite not met (missing `git`, `gh`, `wl`, or dirty worktree) |

### Output

- **Human-readable**: Progress messages, warnings, and errors go to stderr.
- **Structured**: When `--json` is passed, the primary result is JSON on stdout.
- **JSON schema**: All JSON output includes `success` (boolean), and on failure,
  `error` (string) and optionally `code` (exit code category).

### Common flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Validate and plan without mutating the repository |
| `--json` | Output structured JSON instead of human-readable text |
| `--verbose` | Include detailed debugging output |
| `--quiet` | Suppress non-error output |

### Prerequisite checks

Each script checks its prerequisites before performing operations:

- `create-branch`: requires `git`, valid work-item ID format
- `commit`: requires `git`, non-empty worktree changes (unless `--all` with no changes is allowed)
- `push`: requires `git`, valid branch name, remote configured
- `create-pr`: requires `git`, `gh` CLI, authenticated GitHub session
- `merge-pr`: requires `git`, `gh` CLI, valid PR number
- `cleanup`: requires `git`, access to cleanup infrastructure scripts
- `workflow`: requires all prerequisites for the phases it will execute

## Delegation to Existing Infrastructure

This skill delegates to existing scripts where possible:

- **Branch naming/policy**: `skill/ship/scripts/git-helpers.js` (`makeBranchName`, `validateBranchName`, `isBranchBlocked`)
- **Push validation**: `skill/ship/scripts/ship.js` (`validatePushTarget`, `validateForcePush`, `pushToDev`, `pushToBranch`)
- **Post-merge cleanup**: `skill/cleanup/scripts/` (Python cleanup scripts)

This skill does NOT duplicate the logic in these modules; it wraps and orchestrates them.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/create-branch.mjs` | Create and check out a canonical feature branch |
| `scripts/commit.mjs` | Stage changes and commit with conventional format |
| `scripts/push.mjs` | Push to remote with safety checks |
| `scripts/create-pr.mjs` | Create a GitHub Pull Request |
| `scripts/merge-pr.mjs` | Guarded merge with CI verification |
| `scripts/cleanup.mjs` | Delegate to cleanup infrastructure |
| `scripts/workflow.mjs` | Full lifecycle orchestration |
| `scripts/git-mgmt-helpers.mjs` | Shared helpers for script I/O, argument parsing, and output formatting |

## Integration with Agent Policies

- **Patch agent** (`agent/patch.md`): Respects the ask-first policy for push operations.
  The `push` script can be invoked in dry-run mode for review before execution.
- **Ship agent** (`agent/ship.md`): Provides scripted implementations of the
  branch/PR workflows described in the Ship agent documentation.
