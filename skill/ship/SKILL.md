---
name: ship
description: "Canonical push-to-dev and branch-policy enforcement for agents. Provides the push-to-dev workflow, branch naming, conflict handling, and release process guidance. Trigger with: /skill:ship push-to-dev"
---

# Ship Skill

Canonical agent-side push-to-dev behaviour and branch-policy enforcement, with
automated dev-to-main release execution via the Ship subagent.

## Purpose

Provide a deterministic, automated push-to-dev and release workflow for agents:
agents push completed feature branches into `dev`, and the Ship subagent (or a
human Release Manager) promotes tested changes from `dev` to `main` via a
gated, PR-based merge process.

## When To Use

- An agent needs to push completed feature branch work into the `dev` integration branch.
- An agent needs to generate a canonical branch name for a work item.
- An agent needs to validate a branch name or check whether a branch is protected.
- An agent or operator needs to execute a release (dev → main merge) via the Ship subagent.

Triggers

- "release"
- "merge dev to main"
- "ship it"
- "promote dev"
- "release the changes"

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
| `release` | Execute the automated dev → main release (via Ship subagent) |
| `help` | Print the ship skill documentation |

## Prerequisites

- **Node.js** 18+ (for running the JavaScript modules)
- **git** CLI installed and configured
- **gh** CLI (for PR-based workflows — see [Release Process](#release-process))
- **wl** CLI (Worklog — for creating merge-conflict work items and audit logging)
- **jq** CLI (for JSON parsing in the release script)

## Scripts and Modules

- `scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, and re-exports from `git-helpers.js`)
- `scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`)
- `scripts/release/merge-dev-to-main.sh` — the canonical release merge script

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

The Ship skill supports two release execution modes. The **Ship subagent** is
the primary executor; the **human Release Manager** is the fallback.

### Primary executor: Ship subagent

The Ship subagent (configured in [`agent/ship.md`](../agent/ship.md)) executes
the release using the canonical merge script:

```bash
# Preferred safe wrapper (will detect missing release script and present a clear human fallback):
node skill/ship/scripts/run-release.js

# Legacy/direct invocation of the canonical script (use only when you know it exists and is executable):
# bash scripts/release/merge-dev-to-main.sh
```

The script performs the following steps:

1. **Pre-flight checks**: Verifies `gh` authentication, `wl` availability,
   and a clean working tree.
2. **CI verification**: Checks that the `dev-full-suite` workflow is green
   on the `dev` branch via the GitHub Actions API. This is a **hard gate**
   — the script aborts if CI is not green (use `--force` to bypass in
   exceptional circumstances).
3. **Merge commit creation**: Fetches latest `dev` and `main`, creates a
   merge commit locally (`dev` → `main` with `--no-ff`).
4. **PR creation**: Pushes the merge commit to a temporary
   `release/dev-to-main-<timestamp>` branch and creates a GitHub Pull
   Request targeting `main`.
5. **Status check wait**: Waits for required status checks to pass on the PR
   (configurable timeout, default 10 minutes).
6. **PR merge**: Merges the PR using `gh pr merge --merge --delete-branch`,
   which satisfies server-side branch protection on `main`.
7. **Audit logging**: Records the merge commit hash, CI run IDs, PR URL,
   and approver identity in the worklog.

### Fallback: Human Release Manager

For repositories where the Ship subagent is not configured or the automated
merge is not suitable, a **human Release Manager** can perform the release
manually. The Release Manager must follow the checklist and procedures in
[`docs/dev/release-process.md`](../docs/dev/release-process.md).

The human fallback supports three approaches:

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | Run `bash scripts/release/merge-dev-to-main.sh` manually | Ship subagent unavailable but script is available |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Create a temp branch with merge result and open a PR | Want human review before merge |

### Pre-merge checklist

Whether using the Ship subagent or the human fallback, the following must be
verified before merging `dev` into `main`:

1. **CI — `dev-full-suite` is green** on the current `dev` HEAD.
2. **CI — `dev-smoke` is green** on the current `dev` HEAD.
3. **No open merge conflicts** between `dev` and `main`.
4. **No open critical work items** that would block the release.
5. **Changelog / release notes** are updated for user-facing changes.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for the
test commands to run locally.

## Preferred execution behaviour (policy)

- When the Ship subagent is available, it MUST serve as the primary release
  executor. The agent should invoke `scripts/release/merge-dev-to-main.sh`
  to perform the dev → main merge.
- The agent MUST NOT perform the merge by substituting its own ad-hoc git
  commands for the canonical script during normal operation.
- The agent may fall back to manual git commands ONLY in narrowly defined
  edge cases:
  - the merge script is missing or not executable,
  - the script fails with an unexpected error and the operator explicitly
    instructs the agent to fall back,
  - the Ship subagent is not configured and a human has explicitly requested
    manual steps.
- If the Ship subagent is not configured, the agent MUST refuse to perform
  the release automatically and instead direct the operator to the human
  Release Manager fallback procedures in `docs/dev/release-process.md`.

## Preconditions & safety

- Never force-push or rewrite history on `main` or `dev`.
- Never bypass the CI-green gate unless explicitly instructed with `--force`.
- Always log the merge audit to the worklog using `wl comment add`.
- The `main` branch is protected: agents must never push directly to `main`.
- All merges must go through a GitHub Pull Request that satisfies branch
  protection rules, or follow the documented manual fallback exactly.

## Integration with AGENTS.md

The per-work-item merge workflow in AGENTS.md (step 6) describes PR-based merging into `main`. This skill's `pushToDev()` function handles the **dev integration step** that happens *before* the final PR to `main`:

1. Agent implements work on a feature branch → commits → builds → tests
2. Agent calls `pushToDev()` to integrate into `dev`
3. CI runs on `dev`
4. Ship subagent or Release Manager merges `dev` → `main` via PR (see AGENTS.md step 6)

## Outputs

- A GitHub Pull Request from `release/dev-to-main-<timestamp>` to `main`
  (created and merged by the script).
- An audit comment in the worklog with merge commit hash, CI run IDs,
  PR URL, and approver identity.
- A notification to the operator summarising the merge result.
