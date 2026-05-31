---
name: ship
description: "Canonical push-to-dev and branch-policy enforcement for agents. Provides the push-to-dev workflow, branch naming, conflict handling, and release process guidance. Trigger with: /skill:ship push-to-dev"
---

# Ship Skill

Canonical agent-side push-to-dev behaviour and branch-policy enforcement.

## When To Use

- An agent needs to push completed feature branch work into the `dev` integration branch.
- An agent needs to generate a canonical branch name for a work item.
- An agent needs to validate a branch name or check whether a branch is protected.

## How Agents Invoke This Skill

Agents reference this skill explicitly in their prompts or via skill invocation:

```
/skill:ship <action>
```

Where `<action>` is one of:

| Action | Description |
|--------|-------------|
| `push-to-dev` | Push the current feature branch into the `dev` integration branch |
| `validate-branch <name>` | Validate a branch name against the canonical pattern |
| `generate-name <work-item-id> <short-desc>` | Generate a canonical branch name |
| `check-blocked <branch>` | Check whether a branch is protected |
| `help` | Print the ship skill documentation |

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

## Prerequisites

- **Node.js** 18+ (for running the JavaScript modules)
- **git** CLI installed and configured
- **gh** CLI (for PR-based workflows — see [Release Process](#release-process))
- **wl** CLI (Worklog — for creating merge-conflict work items)

## Push-to-Dev Workflow

Agents work in feature branches and push completed work into the `dev` integration branch. This is the canonical integration action.

1. **Create a feature branch** using `makeBranchName(workItemId, shortDesc)` from `scripts/git-helpers.js`.
2. **Make changes and commit** on the feature branch.
3. **Validate** the current branch name with `validateBranchName(name)`.
4. **Push to dev** using `pushToDev()` from `scripts/ship.js`. This:
   - Validates the push target is not a protected branch
   - Rejects force-push
   - Executes `git push origin HEAD:refs/heads/dev`
   - Returns a structured result `{ success, error?, command? }`

### Protected Branches

Agents MUST NOT push directly to:
- `main`
- `master`
- `HEAD`

Use `isBranchBlocked(branch)` or `validatePushTarget(targetBranch)` to check before any push operation.

### Conflict Handling

When `pushToDev()` fails due to a non-fast-forward rejection (conflict):

1. The function returns `{ success: false, error: "..." }` — no force-push is attempted.
2. The agent should create a merge-conflict work item via `wl create` documenting the conflict.
3. The agent reports the failure to the operator.

## Branch Naming Policy

All agent-created branches MUST follow the canonical pattern:

```
wl-<work-item-id>-<short-description>
```

Where:
- `wl-` is a literal prefix
- `<work-item-id>` is the Worklog identifier (e.g., `SA-0MPDZDPZB00121IE`)
- `<short-description>` is a lowercase, hyphen-separated slug

Examples:
- `wl-SA-0MPDZDPZB00121IE-branch-naming-policy`
- `wl-SA-001-fix-login-bug`

### Validation Pattern

Branch names are validated against: `/^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/`

### Pre-push Hook Enforcement

A pre-push hook at `.githooks/pre-push` enforces this policy automatically. Before any `git push`, the hook checks the current branch against the blocked list and rejects the push if the branch is protected. To enable:

```bash
git config core.hooksPath .githooks
```

Force-push (`git push --force` / `git push -f`) is prohibited.

## Release Process

The release process promotes tested, reviewed changes from `dev` to `main`:

1. CI runs validation on `dev` on every change (smoke + critical tests).
2. A human reviewer (Release Manager) inspects CI results.
3. The Release Manager triggers the merge from `dev` → `main` using:
   - `scripts/release/merge-dev-to-main.sh` (PR-based, recommended for protected branches), or
   - Direct merge (for repos without branch protection on main)

See [docs/dev/release-process.md](../docs/dev/release-process.md) and [docs/dev/release-tests.md](../docs/dev/release-tests.md) for details.

## Integration with AGENTS.md

The per-work-item merge workflow in AGENTS.md (step 6) describes PR-based merging into `main`. This skill's `pushToDev()` function handles the **dev integration step** that happens *before* the final PR to `main`:

1. Agent implements work on a feature branch → commits → builds → tests
2. Agent calls `pushToDev()` to integrate into `dev`
3. CI runs on `dev`
4. Release Manager merges `dev` → `main` via PR (see AGENTS.md step 6)

## Files

- `scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, and re-exports from `git-helpers.js`)
- `scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`)
