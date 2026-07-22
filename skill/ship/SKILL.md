---
name: ship
description: "Canonical push-to-dev and branch-policy enforcement for agents. Provides the push-to-dev workflow, branch naming, conflict handling, and release process guidance. Trigger with: /skill:ship push-to-dev"
---

# Ship Skill

Canonical agent-side push-to-dev behaviour and branch-policy enforcement, with
automated dev-to-main release execution.

## Purpose

Provide deterministic push-to-dev and release workflow: agents push feature branches into `dev`; the release process promotes `dev` to `main` via a gated, PR-based merge.

## When To Use

- Push completed work into the `dev` integration branch.
- Generate a canonical branch name for a work item.
- Validate a branch name or check branch protection.
- Execute a release (dev → main merge).

Triggers: "release", "merge dev to main", "ship it", "promote dev", "release the changes"

## How Agents Invoke This Skill

```
/skill:ship <action>
```

| Action | Description |
|--------|-------------|
| `check-unmerged-branches` | List local branches not yet merged into `dev` |
| `push-to-dev` | Push current branch into `dev` |
| `validate-branch <name>` | Validate against canonical pattern |
| `generate-name <work-item-id> <short-desc>` | Generate canonical branch name |
| `check-blocked <branch>` | Check if a branch is protected |
| `release` | Execute dev → main release |
| `help` | Print documentation |

## Prerequisites

- **Node.js** 18+, **git**, **gh** CLI, **wl** CLI (Worklog), **jq** CLI

## Scripts and Modules

- `./scripts/ship.js` — Push-to-dev module (`pushToDev`, `pushToBranch`, `validatePushTarget`, `checkUnmergedBranches`, `checkAuditReadyToClose`)
- `./scripts/git-helpers.js` — Branch naming/policy (`makeBranchName`, `validateBranchName`, `isBranchBlocked`)
- `./scripts/check-unmerged-branches.js` — Unmerged branch detection
- `./scripts/check-audit-gate.js` — Audit readiness gating
- `./scripts/run-release.js` — Release wrapper (includes gating, post-release dev sync)
- `./scripts/release/merge-dev-to-main.sh` — Canonical release merge script

## Usage

```javascript
// Push completed work into dev
import { pushToDev } from './scripts/ship.js';

const result = pushToDev('origin');
if (!result.success) {
  // handle failure — e.g., create a merge-conflict work item
}

// Generate a canonical branch name
import { makeBranchName, validateBranchName, isBranchBlocked } from './scripts/git-helpers.js';

const branchName = makeBranchName('SA-001', 'fix-login-bug');
// Returns: 'wl-SA-001-fix-login-bug'

const validation = validateBranchName('wl-SA-001-fix-login-bug');
// Returns: { valid: true }

const blocked = isBranchBlocked('main');
// Returns: true
```

## Unmerged Branches Gating Step

Before performing any operation that integrates branches (push-to-dev, release),
the ship skill automatically checks for local branches that are not yet merged
into `dev`. This is a **gating step** to prevent accidentally pushing when there
are unmerged feature branches that should be dealt with first.

The check works as follows:

1. Runs `git branch --no-merged dev` to list all local branches not merged into `dev`.
2. Excludes `dev` itself, protected branches (`main`, `master`), and the
   **current branch** (the branch being worked on).
3. For each remaining branch matching the canonical `wl-<work-item-id>-<slug>` pattern,
   extracts the work item ID and queries Worklog (wl) for its title, status, and stage.
4. Returns a structured report listing each unmerged branch with its associated
   work item details.

If unmerged branches are found, the operation is blocked with a report that shows:

- The branch name
- The associated work item title and ID (if the branch follows the canonical pattern)
- The work item's status and stage

### Using checkUnmergedBranches Programmatically

```javascript
import { checkUnmergedBranches } from './scripts/check-unmerged-branches.js';
const report = checkUnmergedBranches();
// Returns: { hasUnmergedBranches, message, unmergedBranches: [{branch, workItemId, title, status, stage}] }
```

### Gating in pushToDev and pushToBranch

Both `pushToDev()` and `pushToBranch()` (when targeting `dev`) automatically
run the unmerged branches check before executing the push. If unmerged branches
are found, the push is rejected with the report in the error message.

To bypass the gating check, resolve or merge the unmerged branches first.

### Audit Readiness Gating

Verifies all `in_review`/`completed` work items have passing audits before release:

1. Query candidates: `wl list --stage in_review --json` + `wl list --status completed --json` (dedup, exclude `done`).
2. For each, check `wl audit-show <id> --json` → `audit.readyToClose`.
3. Items with `readyToClose: false`, `null`, or missing audit → blocking.
4. Blocking items abort release with exit code 6.
5. Gate respects `--skip-checks` flag.

#### Using checkAuditReadyToClose Programmatically

```javascript
import { checkAuditReadyToClose } from './scripts/check-audit-gate.js';
const report = await checkAuditReadyToClose();
// report: { hasBlockingItems, message, blockingItems: [{workItemId, title, reason, summary, remediation}] }
```

#### Exit Code

The audit gate uses exit code 6 to distinguish from other failure modes:

| Code | Meaning |
|------|---------|
| 1 | General error |
| 2 | Missing release script |
| 3 | Unmerged branches found |
| 4 | PR merge failed |
| 5 | Dev sync failed |
| **6** | **Audit gate failure (items not ready to close)** |
| **7** | **Critical-items gate failure (critical items not in terminal state)** |

### Critical-Item Gating

Checks whether any critical-priority items are not in a terminal state (`status=completed`, `stage=in_review` or `done`) before release:

1. Query: `wl list --priority critical --json`.
2. For each item, check `isTerminalState()`: status must be `completed` AND stage must be `in_review` or `done`.
3. Non-terminal items → blocking, abort release with exit code 7.
4. Gates respects `--skip-checks`.

#### Using checkCriticalItems Programmatically

```javascript
import { checkCriticalItems } from './scripts/check-critical-items.js';
const report = checkCriticalItems();
// Returns: { hasBlockingItems, message, blockingItems: [{workItemId, title, currentStatus, currentStage}] }
```

#### Exit Code

The critical-items gate uses exit code 7 to distinguish from other failure
modes:

| Code | Meaning |
|------|---------|
| **7** | **Critical-items gate failure (critical items not in terminal state)** |

### Gating in run-release.js

Runs three gating checks before release:

1. Unmerged branches check (exit code 3)
2. Audit readiness gate (exit code 6)
3. Critical-priority items gate (exit code 7)

Any failure aborts the release. Bypass all checks: `node ./scripts/run-release.js --skip-checks`

## Push-to-Dev Workflow

See [[concepts/git-worktree-best-practices-for-agent-workflows]] for the full worktree workflow and [AGENTS.md](../../AGENTS.md#implement-the-work-item) for the top-level policy.

1. Create a worktree (use `makeBranchName` from `./scripts/git-helpers.js`).
2. Make changes and commit.
3. Validate branch name with `validateBranchName()`.
4. Check for unmerged branches (automatic in `pushToDev()`).
5. Push: `pushToDev()` from `./scripts/ship.js` — validates target, rejects force-push, gates unmerged branches, executes push.

### Protected Branches

Agents MUST NOT push to `main`, `master`, or `HEAD`. Use `isBranchBlocked()` or `validatePushTarget()`.

### Conflict Handling

`pushToDev()` returns `{ success: false, error }` on non-fast-forward rejection. Record conflict details in a work-item comment and resolve manually.

## Branch Naming Policy

All branches MUST follow: `wl-<work-item-id>-<short-description>` (e.g., `wl-SA-001-fix-login-bug`).

Validation pattern: `/^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/`

### Pre-push Hook

Located at `.githooks/pre-push`. Enforce via `git config core.hooksPath .githooks`. Force-push is prohibited.

## Release Process

```bash
node ./scripts/run-release.js
```

Steps:

1. **Unmerged branches check** — aborts with report if branches pending; `--skip-checks` bypasses.
2. **Pre-flight checks** — verifies `gh`, `wl`, clean worktree.
3. **Critical-priority items check** — aborts with exit 7 if non-terminal critical items exist.
4. **CI verification** — checks `dev-full-suite` is green (hard gate); `--force` bypasses.
5. **Merge commit** — fetch latest dev/main, create `--no-ff` merge commit.
6. **PR creation** — push to `release/dev-to-main-<timestamp>`, create PR targeting `main`.
7. **Status check wait & merge** — waits for required checks (default 10 min), then `gh pr merge --merge --delete-branch`. `--force` skips wait.
8. **Audit logging** — records merge hash, CI run IDs, PR URL in worklog.
9. **Sync dev with main** — `syncDevWithMain()`: fetch, checkout dev, merge origin/main, push.
   > Release ops run from **main checkout**, not worktrees.
10. **Close work items (non-blocking)** — `closeWorkItemsAfterRelease(version)`: closes `in_review`/`completed` items, logs warnings on individual failures.

### Fallback: Human Release Manager

For repos where the automated merge is unsuitable, follow [`docs/dev/release-process.md`](../docs/dev/release-process.md).

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | `node ./scripts/run-release.js` manually | Script available |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Temp branch with merge result, open a PR | Human review desired |

### Pre-merge checklist

1. CI (`dev-full-suite`, `dev-smoke`) is green on `dev` HEAD.
2. No open merge conflicts between `dev` and `main`.
3. No open critical work items (automated by critical-items gate; `--skip-checks` bypasses).
4. `CHANGELOG.md` is generated automatically by the release script.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for local test commands.

## Preferred execution behaviour (policy)

- Always invoke `./scripts/run-release.js` for dev→main merges.
- Do NOT substitute ad-hoc git commands for the canonical script.
- Fallback to manual commands only in narrow edge cases: script missing, script fails with operator-okayed fallback, or human explicitly requests manual steps.
- If the release script is unavailable, refuse automatic release and direct operator to `docs/dev/release-process.md`.

## Preconditions & safety

- Never force-push or rewrite history on `main` or `dev`.
- Never bypass the CI-green gate unless `--force` is explicitly instructed.
- Always log merge audit to worklog via `wl comment add`.
- Agents must never push directly to `main`. All merges go through a PR satisfying branch protection rules.

## Integration with AGENTS.md

`pushToDev()` handles the **dev integration step** before the final PR to `main`. See [AGENTS.md](../../AGENTS.md) and [[concepts/git-worktree-best-practices-for-agent-workflows]] for the full workflow.

## Outputs

- GitHub PR from `release/dev-to-main-<timestamp>` to `main`.
- Worklog audit comment with merge hash, CI run IDs, PR URL.
- Operator notification summarising the merge.
