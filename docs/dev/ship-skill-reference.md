# Ship skill — implementation reference

Deep implementation-reference detail relocated from `skill/ship/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers and release operators. Workflow semantics are unchanged —
every command/flag documented here is still valid.

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
| `./scripts/remediate-spurious-closes.js` | Idempotent remediation sweep for test-suite-spuriously-closed work items (SA-0MSJ2XMQL006CVQS) |

## Usage
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
| 11 | Release merge verification failed (close-work-items step refused — no verified dev→main merge) |

## Release Process
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
9. **Verify the release merge (gating)** — `verifyReleaseMerge(version)` (SA-0MSJ2XMQL006CVQS): the close step only runs after the release actually landed on main. Both conditions must hold:
   - the released version tag `v<version>` exists on origin (`git ls-remote`), and
   - the tag commit is an ancestor of `origin/main` (`git merge-base --is-ancestor`).
   If verification fails, the release aborts with **exit code 11** and **no work items are closed** — a spurious "Shipped" record cannot be created without a real dev→main merge.
10. **Close work items (non-blocking)** — `closeWorkItemsAfterRelease(version)`: closes `in_review`/`completed` items, filtering to only close items with `needsProducerReview === false`. Items with `needsProducerReview = true`, `null`, or `undefined` are skipped and logged as "Skipped (needs producer review)". Logs warnings on individual close failures.

#### Test isolation (mandatory)

The close-work-items unit tests must **never mutate the live worklog**. Root cause
of SA-0MSJ2XMQL006CVQS: `tests/unit/test-close-work-items-after-release.mjs`
called the real `closeWorkItemsAfterRelease('1.0.0'/'1.2.3')` export against the
live worklog, spuriously closing ~360 real work items with "Shipped in v1.0.0"/
"v1.2.3" reasons on every suite run. `closeWorkItemsAfterRelease` therefore
accepts injectable `getCandidateItemsFn`/`runCloseCommand` boundaries; tests must
inject fakes (or mock `wl` on PATH) and must never invoke the close function
with the default worklog boundary outside a real, verified release flow.

### Remediation sweep: test-spuriously-closed work items
### Remediation sweep: test-spuriously-closed work items

If the test-isolation bug ever recurs (work items closed with reason
"Shipped in v1.0.0" or "Shipped in v1.2.3" that never shipped), run the
idempotent sweep helper from the main checkout:

```bash
node ./scripts/remediate-spurious-closes.js
```

The sweep scans every work item, deletes close comments authored by `worklog`
with content exactly `Closed with reason: Shipped in v1.0.0` / `v1.2.3`, and
restores each affected item to `status=completed, stage=in_review` (the valid
status/stage pair for a release-ready item — the worklog rejects
`open`/`in_review`). Legitimate close comments (real versions such as v0.1.11)
are never touched. Re-running after a successful sweep is a no-op.

## Fallback: Human Release Manager
### Cached test verification at release time

Verifying the full project suite is green before promoting `dev` to `main` is
an **optional pre-release verification step** driven by the
[test skill](../../skill/test/SKILL.md) (`/skill:test` — run → triage → evaluate →
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

See [`docs/dev/release-tests.md`](release-tests.md) for local test commands.

## Preferred execution behaviour (policy)
