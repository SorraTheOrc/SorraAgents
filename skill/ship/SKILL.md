---
name: ship
description: "Canonical dev-to-main release workflow. Provides the release process (dev → main merge) with automated gating for unmerged branches, audit readiness, critical items, worklog refs, and producer review. Trigger with: /skill:ship release"
---

# Ship Skill

Canonical agent-side dev-to-main release execution with automated gating.

## Purpose

Provide a single, deterministic release workflow: `dev` is promoted to `main` via a gated, PR-based merge. All helper functions (branch naming, validation, unmerged-branch detection, audit readiness, etc.) are internal implementation details.

## When To Use

- Execute a release (promote `dev` to `main`).

Triggers: "ship it", "shipit", "ship", "release", "promote dev", "merge dev to main", "release the changes" — all map to the `release` action.

## How Agents Invoke This Skill

```
/skill:ship release
```

The `release` action runs the full dev→main release pipeline (see Release Process below).

## Prerequisites

- **Node.js** 18+, **git**, **gh** CLI, **wl** CLI (Worklog), **jq** CLI

## Internal Scripts and Modules

All scripts below are internal implementation details — they are not exposed as user-facing actions. The only user-facing action is `release`.

| Script | Purpose |
|--------|---------|
| `./scripts/run-release.js` | Release wrapper (includes gating, post-release dev sync) |
| `./scripts/release/merge-dev-to-main.sh` | Canonical release merge script |
| `./scripts/ship.js` | Push-to-dev helper (`pushToDev`, `pushToBranch`, `validatePushTarget`) — used by the implement workflow |
| `./scripts/git-helpers.js` | Branch naming/policy (`makeBranchName`, `validateBranchName`, `isBranchBlocked`) |
| `./scripts/check-unmerged-branches.js` | Unmerged branch detection |
| `./scripts/check-audit-gate.js` | Audit readiness and producer-review gating |
| `./scripts/check-critical-items.js` | Critical-items gating |
| `./scripts/check-worklog-refs.js` | Worklog refs gating |

## Usage

```bash
# Execute a release (dev → main merge)
node ./scripts/run-release.js
```

For programmatic access to internal helpers (used by the implement workflow):

```javascript
// Push completed work into dev (internal to implement workflow)
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

## Gating

The `release` action runs five gating checks before merging `dev` to `main`:

1. **Unmerged branches check** — aborts with report if feature branches pending; exit code 3; `--skip-checks` bypasses.
2. **Audit readiness gate** — verifies all `in_review`/`completed` items pass audits; exit code 6; `--skip-checks` bypasses. Timed-out or transient audits (provider error, script execution failure) are reported as warnings and do **not** block the release — only genuine "not ready to close" verdicts block.
3. **Critical-items gate** — aborts if non-terminal critical items exist; exit code 7; `--skip-checks` bypasses.
4. **Worklog refs gate** — aborts if worklog refs are still present in merged code; exit code 8.
5. **Producer-review gate** — aborts if items need producer review; exit code 9; `--skip-checks` bypasses.

CI is **optional**: if the PR has status checks (e.g., a CI workflow is configured) they must pass before merge; if no status checks exist, the merge proceeds without waiting. See [Release Process](#release-process) step 6.

Bypass all checks: `node ./scripts/run-release.js --skip-checks`

### Code Freeze

While a release is running, the ship skill sets a **Code Freeze marker** at
`.worklog/code-freeze.json` (cross-repo contract WL-0MSBU4KMA004PKSR):

```json
{ "active": true, "reason": "ship release in progress", "startedAt": "<ISO>", "pid": <pid> }
```

The marker is written **before** the gating checks run and cleared on **every**
exit path (success, failure, abort, `--dry-run`, and gating failures) via a
`try/finally` in `run-release.js` and an `EXIT` trap in
`merge-dev-to-main.sh`. While the marker is present, the implement skill
refuses to start new implementation work (fail-open: a missing/corrupt marker
never blocks implementation). A stale marker from a crashed release can be
removed manually by deleting `.worklog/code-freeze.json`.

### Exit Codes

| Code | Meaning |
|------|---------|
| 1 | General error |
| 2 | Missing release script |
| 3 | Unmerged branches found |
| 4 | PR merge failed |
| 5 | Dev sync failed |
| 6 | Audit gate failure (items not ready to close) |
| 7 | Critical-items gate failure (critical items not in terminal state) |
| 8 | Worklog-ref gate failure (worklog refs present) |
| 9 | Producer-review gate failure (items need producer review) |
| 10 | Release script timed out (killed after `SHIP_RELEASE_TIMEOUT_MS`, default 600s) |

## Release Process

```bash
node ./scripts/run-release.js
```

Steps:

1. **Unmerged branches check** — aborts with report if branches pending; `--skip-checks` bypasses.
2. **Pre-flight checks** — verifies `gh`, `wl`, clean worktree.
3. **Critical-priority items check** — aborts with exit 7 if non-terminal critical items exist.
4. **Merge commit** — fetch latest dev/main, create `--no-ff` merge commit.
5. **PR creation** — push to `release/dev-to-main-<timestamp>`, create PR targeting `main`.
6. **Status check wait & merge** — if the PR has status checks, waits for them to pass (default 10 min), then `gh pr merge --merge --delete-branch`. If the PR has **no** status checks (no CI configured), the merge proceeds immediately. `--force` skips the wait.
7. **Audit logging** — records merge hash, PR URL in worklog.
8. **Sync dev with main** — `syncDevWithMain()`: fetch, checkout dev, merge origin/main, push.
   > Release ops run from **main checkout**, not worktrees.
9. **Close work items (non-blocking)** — `closeWorkItemsAfterRelease(version)`: closes `in_review`/`completed` items, filtering to only close items with `needsProducerReview === false`. Items with `needsProducerReview = true`, `null`, or `undefined` are skipped and logged as "Skipped (needs producer review)". Logs warnings on individual close failures.

### Fallback: Human Release Manager

For repos where the automated merge is unsuitable, follow [`docs/dev/release-process.md`](../docs/dev/release-process.md).

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | `node ./scripts/run-release.js` manually | Script available |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Temp branch with merge result, open a PR | Human review desired |

### Pre-merge checklist

1. No open merge conflicts between `dev` and `main`.
2. No open critical work items (automated by critical-items gate; `--skip-checks` bypasses).
3. If CI status checks are configured on the PR, they must pass (automated by step 6). If no CI is configured, this is satisfied automatically.
4. `CHANGELOG.md` is generated automatically by the release script.

### Cached test verification at release time

Verifying the full project suite is green before promoting `dev` to `main` is
an **optional pre-release verification step** driven by the
[test skill](../test/SKILL.md) (`/skill:test` — run → triage → evaluate →
loop until green, quiet pytest contract). Release Managers may invoke it to
confirm the suite is green before merging; the release itself does not
depend on it unless the operator chooses to gate on it.

Repeated full-suite verification at the same HEAD is expensive (minutes per
run). Route release-time test checks through the **cached runner** so
repeat verifications reuse the prior run instead of re-executing (see
`test_cache.py` at the repo root, SA-0MSGN5OJ4002OZKY):

```bash
# Fresh full run (populates the cache)
python3 ../test/scripts/run_tests.py --json

# Subsequent verification at the same state reuses the cache (fast)
python3 ../test/scripts/run_tests.py --json

# Read-only summary query — never executes the suite
python3 ../test/scripts/run_tests.py --summary --suite all

# Force a genuinely fresh run for the final release gate
python3 ../test/scripts/run_tests.py --force --json
```

Cached results are valid for the same git state within the 2-hour TTL; a
changed tree, expired TTL, or corrupt entry always triggers a fresh run.
This optional release test gate is wired via SA-0MSBXQZCG0078SEW.

See [`docs/dev/release-tests.md`](../docs/dev/release-tests.md) for local test commands.

## Preferred execution behaviour (policy)

- Always invoke `./scripts/run-release.js` for dev→main merges.
- Do NOT substitute ad-hoc git commands for the canonical script.
- Fallback to manual commands only in narrow edge cases: script missing, script fails with operator-okayed fallback, or human explicitly requests manual steps.
- If the release script is unavailable, refuse automatic release and direct operator to `docs/dev/release-process.md`.

## Preconditions & safety

- Never force-push or rewrite history on `main` or `dev`.
- Never bypass required status checks unless `--force` is explicitly instructed.
- Always log merge audit to worklog via `wl comment add`.
- Agents must never push directly to `main`. All merges go through a PR satisfying branch protection rules.

## Integration with AGENTS.md

The implement workflow uses `pushToDev()` internally to push feature branches into `dev`. The ship skill's `release` action promotes `dev` to `main`. See [AGENTS.md](../../AGENTS.md) and [[concepts/git-worktree-best-practices-for-agent-workflows]] for the full workflow.

## Outputs

- GitHub PR from `release/dev-to-main-<timestamp>` to `main`.
- Worklog audit comment with merge hash and PR URL.
- Operator notification summarising the merge.
