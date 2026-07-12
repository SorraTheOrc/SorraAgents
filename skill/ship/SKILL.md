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

- `./scripts/ship.js` — Push-to-dev behaviour module (exports: `pushToDev`, `pushToBranch`, `validatePushTarget`, `validateForcePush`, `DEV_BRANCH`, `PROTECTED_BRANCHES`, `checkUnmergedBranches`, `checkAuditReadyToClose`, and re-exports from `git-helpers.js`)
- `./scripts/git-helpers.js` — Branch naming and policy helpers (exports: `makeBranchName`, `validateBranchName`, `isBranchBlocked`, `BLOCKED_BRANCHS`, `BRANCH_NAME_PATTERN`)
- `./scripts/check-unmerged-branches.js` — Unmerged branch detection module (exports: `checkUnmergedBranches`, `getUnmergedBranchNames`, `extractWorkItemId`, `getWorkItemStatus`)
- `./scripts/check-audit-gate.js` — Audit readiness gating module (exports: `checkAuditReadyToClose`, `getAuditStatus`, `getCandidateItems`)
- `./scripts/run-release.js` — Safe wrapper to invoke the release process (exports: `runRelease`, `syncDevWithMain`, `parsePRUrl`, `waitForPRMerge`; includes unmerged branches gating check, audit readiness gating, post-release dev sync)
- `./scripts/release/merge-dev-to-main.sh` — Canonical release merge script (installed in the skill directory)

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

### Audit Readiness Gating

In addition to the unmerged branches check, the ship skill includes an **audit
readiness gate** that verifies all `in_review` and `completed` work items have
passing audits before a release is performed.

This gate:

1. Queries `wl list --stage in_review --json` and `wl list --status completed --json`
   (deduplicating by ID, excluding items already in `stage: done`) to collect
   candidate work items.
2. For each item, calls `wl audit-show <id> --json` and checks `audit.readyToClose`.
3. Items where `audit.readyToClose` is `false`, `audit` is `null` (no audit
   exists), or the audit is otherwise absent are flagged as **blocking**.
4. If any blocking items are found, the release is aborted with exit code 6
   and a structured report is printed showing which items block and why.
5. If all queried items have `audit.readyToClose: true`, the gate passes
   silently and the release proceeds.

The gate respects the existing `--skip-checks` flag to bypass when explicitly
requested.

#### Using checkAuditReadyToClose Programmatically

```javascript
import { checkAuditReadyToClose } from './scripts/check-audit-gate.js';

const report = await checkAuditReadyToClose();

if (report.hasBlockingItems) {
  console.log(report.message);
  // Example output:
  //   ⚠️  Audit gate check failed — 1 of 3 work item(s) are not ready to close:
  //
  //   1. My Feature (SA-001)
  //      Reason: No audit found
  //      Remediation:
  //        # Re-run audit for SA-001:
  //        wl audit-show SA-001 --json
  //        python3 ../audit/scripts/audit_runner.py issue SA-001
}
```

The report contains:

```javascript
// report.blockingItems is an array of:
// {
//   workItemId: string,          // The work item ID
//   title: string,               // The work item title
//   reason: string,               // Why the item is blocking ("No audit found" or "Audit verdict: not ready to close")
//   summary: string|null,         // The audit summary (if available)
//   remediation: string           // Actionable shell commands to re-run the audit
// }
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

### Gating in run-release.js

The release wrapper (`run-release.js`) runs both the unmerged branches check
(exit code 3) and the audit readiness gate (exit code 6) before executing the
release script. If either check fails, the release is aborted. To bypass both,
use the `--skip-checks` flag:

```bash
node ./scripts/run-release.js --skip-checks
```

## Push-to-Dev Workflow

Agents work in worktrees with feature branches and push completed work into the `dev` integration branch. This is the canonical integration action.

See [[concepts/git-worktree-best-practices-for-agent-workflows]] for the full worktree workflow (create, use, push, clean up) and [AGENTS.md](../../AGENTS.md#implement-the-work-item) for the top-level policy.

1. **Create a worktree with a feature branch** inside `.worklog/worktrees/` using the naming convention from `makeBranchName(workItemId, shortDesc)` in `./scripts/git-helpers.js`.
2. **Make changes and commit** inside the worktree.
3. **Validate** the current branch name with `validateBranchName(name)`.
4. **Check for unmerged branches** using `checkUnmergedBranches()` (also run automatically by `pushToDev()`).
5. **Push to dev** from inside the worktree using `pushToDev()` from `./scripts/ship.js`. This:
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
node ./scripts/run-release.js
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

   **Note**: Release operations run from the **main checkout** (not a
   worktree). Implementation agents use worktrees for feature work; the
   release/ship process operates on the canonical `dev` branch in the main
   checkout. After pushing from a worktree, agents should clean up the
   worktree and return to the main checkout.

9. **Close work items (non-blocking)**: After syncing `dev` with `main`, the
   script closes all work items that were validated by the audit readiness
   gate (Step 2). It reads the released version from the git tag (created by
   `merge-dev-to-main.sh`) or falls back to `package.json`, then runs
   `wl close <id> --reason "Shipped in v<version>" --json` for each
   candidate item.

   This step is **non-blocking** — if closing an individual item fails (e.g.,
   permission issue), the error is logged as a warning and the script
   continues with the remaining items. The release exit code is not affected
   by close failures. Empty candidate sets (no items to close) are handled
   gracefully.

   The close step is performed by `closeWorkItemsAfterRelease(version)`
   which:
   - Calls `getCandidateItems()` from `check-audit-gate.js` to get items in
     `in_review` stage or `completed` status (excluding `stage: done`).
   - Iterates over each item and runs `wl close` with the shipped version.
   - Logs successes and warnings.
   - Returns a structured result `{ success, message, closedCount, errorCount }`.

### Fallback: Human Release Manager

For repositories where the automated merge is not suitable, a **human Release Manager** can perform the release
manually. The Release Manager must follow the checklist and procedures in
[`docs/dev/release-process.md`](../docs/dev/release-process.md).

The human fallback supports three approaches:

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | Run `node ./scripts/run-release.js` manually | Script is available in the skill directory |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Create a temp branch with merge result and open a PR | Want human review before merge |

### Pre-merge checklist

The following must be verified before merging `dev` into `main`:

1. **CI — `dev-full-suite` is green** on the current `dev` HEAD.
2. **CI — `dev-smoke` is green** on the current `dev` HEAD.
3. **No open merge conflicts** between `dev` and `main`.
4. **No open critical work items** that would block the release.
5. **CHANGELOG.md** is generated automatically by the release script from
   completed / in_review work items. Verify the generated section covers all
   relevant user-facing changes.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for the
test commands to run locally.

## Preferred execution behaviour (policy)

- The agent should invoke `./scripts/run-release.js`
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

The per-work-item merge workflow in [AGENTS.md](../../AGENTS.md) describes PR-based merging into `main`. This skill's `pushToDev()` function handles the **dev integration step** that happens *before* the final PR to `main`:

1. Agent implements work inside a worktree → commits → builds → tests
2. Agent calls `pushToDev()` from within the worktree to integrate into `dev`
3. After pushing, the agent cleans up the worktree (see [[concepts/git-worktree-best-practices-for-agent-workflows]])
4. Agent switches to the main checkout's `dev` branch
5. CI runs on `dev`
6. Release Manager merges `dev` → `main` via PR (see AGENTS.md step 6)

## Outputs

- A GitHub Pull Request from `release/dev-to-main-<timestamp>` to `main`
  (created and merged by the script).
- An audit comment in the worklog with merge commit hash, CI run IDs,
  PR URL, and approver identity.
- A notification to the operator summarising the merge result.
