---
name: ship
description: "Canonical push-to-dev and branch-policy enforcement for agents. Provides the push-to-dev workflow, branch naming, conflict handling, and release process guidance. Trigger with: /skill:ship push-to-dev"
---

# Ship Skill

Canonical agent-side push-to-dev behaviour and branch-policy enforcement, with
automated dev-to-main release execution.

## Purpose

Provide a deterministic, automated push-to-dev and release workflow for agents:
agents push completed feature branches into `dev`, and the release process
promotes tested changes from `dev` to `main` via a
gated, PR-based merge process.

## When To Use

- An agent needs to push completed feature branch work into the `dev` integration branch.
- An agent needs to generate a canonical branch name for a work item.
- An agent needs to validate a branch name or check whether a branch is protected.
- An agent or operator needs to execute a release (dev → main merge).

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
| `check-unmerged-branches` | Check for local branches not yet merged into `dev` and report their work item details |
| `push-to-dev` | Push the current feature branch into the `dev` integration branch |
| `validate-branch <name>` | Validate a branch name against the canonical pattern |
| `generate-name <work-item-id> <short-desc>` | Generate a canonical branch name |
| `check-blocked <branch>` | Check whether a branch is protected |
| `release` | Execute the automated dev → main release |
| `help` | Print the ship skill documentation |

## Prerequisites

- **Node.js** 18+ (for running the JavaScript modules)
- **git** CLI installed and configured
- **gh** CLI (for PR-based workflows — see [Release Process](#release-process))
- **wl** CLI (Worklog — for creating merge-conflict work items and audit logging)
- **jq** CLI (for JSON parsing in the release script)

## Scripts and Modules

- `skill/ship/scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, `checkUnmergedBranches`, and re-exports from `git-helpers.js`)
- `skill/ship/scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`)
- `skill/ship/scripts/check-unmerged-branches.js` — Unmerged branch detection module (exports: `checkUnmergedBranches`, `getUnmergedBranchNames`, `extractWorkItemId`, `getWorkItemStatus`)
- `skill/ship/scripts/run-release.js` — Safe wrapper to invoke the release process (exports: `runRelease`, `syncDevWithMain`, `parsePRUrl`, `waitForPRMerge`; includes unmerged branches gating check, post-release dev sync)
- `skill/ship/scripts/release/merge-dev-to-main.sh` — Canonical release merge script (installed in the skill directory)

## Usage

```javascript
// Push completed work into dev
import { pushToDev } from 'skill/ship/scripts/ship.js';

const result = pushToDev('origin');
if (!result.success) {
  // handle failure — e.g., create a merge-conflict work item
}

// Generate a canonical branch name
import { makeBranchName, validateBranchName, isBranchBlocked } from 'skill/ship/scripts/git-helpers.js';

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
import { checkUnmergedBranches } from 'skill/ship/scripts/check-unmerged-branches.js';

const report = checkUnmergedBranches();

if (report.hasUnmergedBranches) {
  console.log(report.message);
  // Example output:
  //   Found 2 local branch(es) not yet merged into 'dev':
  //
  //   1. Branch: wl-SA-001-fix-login-bug
  //      Work Item: Fix login bug (SA-001)
  //      Status: in_progress
  //      Stage: in_review
  //
  //   Would you like to merge these branches into dev first before proceeding?
}
```

The report is also available as structured data:

```javascript
// report.unmergedBranches is an array of:
// {
//   branch: string,          // The branch name
//   workItemId: string|null,  // Extracted work item ID (null if not a wl- branch)
//   title: string|null,       // Work item title from Worklog
//   status: string|null,      // Work item status
//   stage: string|null,       // Work item stage
//   error?: string            // Any error from querying Worklog
// }
```

### Gating in pushToDev and pushToBranch

Both `pushToDev()` and `pushToBranch()` (when targeting `dev`) automatically
run the unmerged branches check before executing the push. If unmerged branches
are found, the push is rejected with the report in the error message.

To bypass the gating check, resolve or merge the unmerged branches first.

### Gating in run-release.js

The release wrapper (`run-release.js`) also runs the unmerged branches check
before executing the release script. If unmerged branches are found, the release
is aborted. To bypass, use the `--skip-checks` flag:

```bash
node skill/ship/scripts/run-release.js --skip-checks
```

## Push-to-Dev Workflow

Agents work in feature branches and push completed work into the `dev` integration branch. This is the canonical integration action.

1. **Create a feature branch** using `makeBranchName(workItemId, shortDesc)` from `skill/ship/scripts/git-helpers.js`.
2. **Make changes and commit** on the feature branch.
3. **Validate** the current branch name with `validateBranchName(name)`.
4. **Check for unmerged branches** using `checkUnmergedBranches()` (also run automatically by `pushToDev()`).
5. **Push to dev** using `pushToDev()` from `skill/ship/scripts/ship.js`. This:
   - Validates the push target is not a protected branch
   - Rejects force-push
   - Checks for unmerged branches (gating step)
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
2. The agent should record the conflict details in a comment on the owning work item and resolve manually.
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

Execute the release using the canonical merge script:

```bash
node skill/ship/scripts/run-release.js
```

The script performs the following steps:

1. **Unmerged branches check**: Runs `checkUnmergedBranches()` to verify no local
   branches are pending merge into `dev`. If unmerged branches are found, the
   release is aborted with a report. Use `--skip-checks` to bypass.
2. **Pre-flight checks**: Verifies `gh` authentication, `wl` availability,
   and a clean working tree.
3. **CI verification**: Checks that the `dev-full-suite` workflow is green
   on the `dev` branch via the GitHub Actions API. This is a **hard gate**
   — the script aborts if CI is not green (use `--force` to bypass in
   exceptional circumstances).
4. **Merge commit creation**: Fetches latest `dev` and `main`, creates a
   merge commit locally (`dev` → `main` with `--no-ff`).
5. **PR creation**: Pushes the merge commit to a temporary
   `release/dev-to-main-<timestamp>` branch and creates a GitHub Pull
   Request targeting `main`.
6. **Status check wait & PR merge**: Waits for required status checks to pass
   on the PR (configurable timeout, default 10 minutes), then merges the PR
   using `gh pr merge --merge --delete-branch`. When using `--force`, the PR
   is merged immediately without waiting.
7. **Audit logging**: Records the merge commit hash, CI run IDs, PR URL,
   and approver identity in the worklog.
8. **Sync dev with main**: After the release is complete, the script
   automatically switches back to the `dev` branch, merges `origin/main` into
   it (fast-forward), and pushes the updated `dev` to origin. This ensures
   `dev` stays in sync with `main` after each release.

   The dev sync is performed by `syncDevWithMain()` which:
   - Runs `git fetch origin --prune`
   - Runs `git checkout dev`
   - Runs `git merge origin/main`
   - Runs `git push origin dev`

### Fallback: Human Release Manager

For repositories where the automated merge is not suitable, a **human Release Manager** can perform the release
manually. The Release Manager must follow the checklist and procedures in
[`docs/dev/release-process.md`](../docs/dev/release-process.md).

The human fallback supports three approaches:

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | Run `node skill/ship/scripts/run-release.js` manually | Script is available in the skill directory |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Create a temp branch with merge result and open a PR | Want human review before merge |

### Pre-merge checklist

The following must be verified before merging `dev` into `main`:

1. **CI — `dev-full-suite` is green** on the current `dev` HEAD.
2. **CI — `dev-smoke` is green** on the current `dev` HEAD.
3. **No open merge conflicts** between `dev` and `main`.
4. **No open critical work items** that would block the release.
5. **Changelog / release notes** are updated for user-facing changes.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for the
test commands to run locally.

## Preferred execution behaviour (policy)

- The agent should invoke `skill/ship/scripts/run-release.js`
  to perform the dev → main merge.
- The agent MUST NOT perform the merge by substituting its own ad-hoc git
  commands for the canonical script during normal operation.
- The agent may fall back to manual git commands ONLY in narrowly defined
  edge cases:
  - the merge script is missing or not executable,
  - the script fails with an unexpected error and the operator explicitly
    instructs the agent to fall back,
  - a human has explicitly requested manual steps.
- If the release script is not available, the agent MUST refuse to perform
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
4. Release Manager merges `dev` → `main` via PR (see AGENTS.md step 6)

## Outputs

- A GitHub Pull Request from `release/dev-to-main-<timestamp>` to `main`
  (created and merged by the script).
- An audit comment in the worklog with merge commit hash, CI run IDs,
  PR URL, and approver identity.
- A notification to the operator summarising the merge result.
