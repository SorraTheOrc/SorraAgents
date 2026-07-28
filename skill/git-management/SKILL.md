---
name: git-management
description: "Unified git management skill that orchestrates the full feature-branch lifecycle — create, commit, push, PR, merge, cleanup — for both AI agents and human operators."
---

# Git Management Skill

Deterministic, safe feature-branch lifecycle for agents and operators.

## Operations

1. **Worktree creation** — isolated feature branch worktrees
2. **Commit** — conventional, Worklog-linked commit messages
3. **Push** — with safety checks (no force-push, no direct-to-main)
4. **PR creation** — GitHub PRs from feature branches
5. **Merge** — guarded with CI verification
6. **Cleanup** — post-merge branch/worktree pruning
7. **Workflow** — single entry point for full lifecycle

## Prerequisites

`git`, `node` 18+, `gh` CLI (optional for push-only), `wl` CLI

## Invocation

```
/skill:git-management <action>
```

| Action | Description |
|--------|-------------|
| `create-branch <id> <desc>` | Create worktree with canonical feature branch |
| `commit -m <msg> --work-item <id> [--all] [--dry-run]` | Stage + commit |
| `push [--remote <name>] [--into-dev] [--dry-run]` | Push with safety checks |
| `create-pr [--base <b>] [--title <t>]` | Create GitHub PR |
| `merge-pr <num> [--delete-source]` | Merge with CI checks |
| `cleanup [--dry-run] [--days <n>]` | Post-merge cleanup |
| `workflow <id> <desc> [--dry-run] [--phase <n>]` | Full lifecycle |
| `help` | This documentation |

## Safety Constraints

- **No force-push**: Any `--force` request is rejected (exit 2)
- **Protected branches**: Pushes to `main`, `master`, `HEAD` rejected. Validated via `isBranchBlocked()` from `../ship/scripts/git-helpers.js`
- **Branch naming**: `wl-<work-item-id>-<short-desc>` generated via `makeBranchName()`
- **Worktree naming**: Same pattern, under `.worklog/worktrees/`. See [[concepts/git-worktree-best-practices-for-agent-workflows]] and [AGENTS.md](../../AGENTS.md)
- **CI gate**: `merge-pr` verifies CI checks before merge; skips/fails if pending/failing

## Script Contract

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Safety violation |
| 3 | Missing prerequisite |

### Output

Human-readable to stderr; JSON to stdout when `--json` is passed. JSON includes `success` (bool), `error` (string on failure), `code` (exit code).

### Common flags

`--dry-run`, `--json`, `--verbose`, `--quiet`

### Prerequisite checks per action

`create-branch`: git + valid id. `commit`: git + changes. `push`: git + valid branch + remote. `create-pr`/`merge-pr`: git + gh. `cleanup`: git + cleanup scripts. `workflow`: all required phase prerequisites.

## Delegation

- **Branch naming/policy**: `../ship/scripts/git-helpers.js`
- **Push validation**: `../ship/scripts/ship.js`
- **Post-merge cleanup**: `../cleanup/scripts/`

This skill wraps and orchestrates, does not duplicate.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/create-branch.mjs` | Feature branch worktree |
| `scripts/commit.mjs` | Stage + conventional commit |
| `scripts/push.mjs` | Safe push |
| `scripts/create-pr.mjs` | GitHub PR |
| `scripts/merge-pr.mjs` | Guarded merge |
| `scripts/cleanup.mjs` | Delegate cleanup |
| `scripts/workflow.mjs` | Full lifecycle |
| `scripts/git-mgmt-helpers.mjs` | Shared helpers |

## Integration

- **Patch agent** (`agent/patch.md`): Respects ask-first push policy; dry-run available
- **Ship agent** (`agent/ship.md`): Scripted branch/PR workflows
