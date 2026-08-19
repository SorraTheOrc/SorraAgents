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
| `./skill/ship/scripts/run-release.js` | Release wrapper (includes gating, post-release dev sync) |
| `./skill/ship/scripts/release/merge-dev-to-main.sh` | Canonical release merge script |
| `./skill/ship/scripts/ship.js` | Push-to-dev helper (`pushToDev`, `pushToBranch`, `validatePushTarget`) — used by the implement workflow |
| `./skill/ship/scripts/git-helpers.js` | Branch naming/policy (`makeBranchName`, `validateBranchName`, `isBranchBlocked`) |
| `./skill/ship/scripts/check-unmerged-branches.js` | Unmerged branch detection |
| `./skill/ship/scripts/check-audit-gate.js` | Audit readiness and producer-review gating (`getCandidateItems`, `getTopLevelCandidateItems`, `checkAuditReadyToClose`, `checkProducerReviewStatus`, `resolveAuditRunner`) |
| `./skill/ship/scripts/check-critical-items.js` | Critical-items gating |
| `./skill/ship/scripts/check-worklog-refs.js` | Worklog refs gating |
| `./skill/ship/scripts/discord-notify.js` | Post-release Discord notification (`sendReleaseNotification`, config resolution, changelog extraction/truncation, embed payload, non-blocking webhook POST) |
| `./skill/ship/scripts/remediate-spurious-closes.js` | Idempotent remediation sweep for test-suite-spuriously-closed work items (SA-0MSJ2XMQL006CVQS) |

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
| 6 | Audit gate failure — top-level `in_review` item(s) lack a passing audit after conservative auto-remediation (missing/transient audits are re-run automatically via `audit_runner.py`; genuine "not ready to close" verdicts block immediately) |
| 7 | Critical-items gate failure (critical items not in terminal state) |
| 8 | Worklog-ref gate failure (worklog refs present) |
| 9 | Producer-review gate failure — top-level `in_review` item(s) flagged for producer review (`needsProducerReview != false`) |
| 10 | Release script timed out (killed after `SHIP_RELEASE_TIMEOUT_MS`, default 600s) |
| 11 | Release merge verification failed (close-work-items step refused — no verified dev→main merge) |

## Audit & Producer-Review Gates

`check-audit-gate.js` provides the two readiness gates evaluated during a
release (before the dev→main merge):

### Candidate scoping (top-level only)

Both gates operate on **top-level** `in_review` items (`parentId == null`),
never children — a child's audit and producer review are covered by its
parent's, so a child-level gap must never spuriously block a release.

- `getCandidateItems()` — queries `wl list --stage in_review --json`, piped
  through `jq` so only `{id, title, needsProducerReview, parentId}` crosses
  into the execSync buffer (ENOBUFS-safe single query, SA-0MSLW5P7J0068UFZ).
- `getTopLevelCandidateItems()` — sibling of `getCandidateItems()` that
  filters to `parentId == null`; used by both gates. Orphaned `in_review`
  items (no parent) are top-level and remain gated.

### Audit readiness gate (exit 6)

`checkAuditReadyToClose()` checks each top-level item via
`wl audit-show <id> --json` and classifies it:

- **Genuine "not ready to close"** verdict — blocks immediately, **no**
  re-audit attempt.
- **Missing audit** or **transient** audit (timeout, provider error,
  FailureNotice per `isTimeoutOrTransientAudit`) — **conservative
  auto-remediation**: the gate re-runs the audit via
  `resolveAuditRunner()` (in-repo `skill/audit/scripts/audit_runner.py`
  preferred, global `~/.pi/agent/skills/audit/scripts/audit_runner.py`
  fallback) with `audit_runner.py issue <id>`, then re-checks
  `wl audit-show`. The item blocks **only if it still fails after the
  re-run**; successful remediations are reported in `remediatedItems`.
- **Remediation-runner failure/timeout** — treated as blocking for that item
  with the manual remediation command surfaced; never silently passed.

The gate never mutates state destructively: it only invokes
`audit_runner.py` (which persists per its own contract) and `wl audit-show`
(read-only) — never `wl update`/`wl close` directly. Command boundaries
(`getCandidateItemsFn`, `runAuditShow`, `runAuditCommand`,
`resolveAuditRunnerFn`) are injectable so unit tests are hermetic (no live
`wl`/`audit_runner` runs).

### Producer-review gate (exit 9)

`checkProducerReviewStatus(items)` blocks on any top-level item with
`needsProducerReview != false` (`true`, `null`, or `undefined`). The
`run-release.js` Step 3.6 call site passes `getTopLevelCandidateItems()`, so
child items never block this gate either.

## Release Process
## Release Process

```bash
node ./skill/ship/scripts/run-release.js
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
10. **Discord release notification (non-blocking)** — `sendReleaseNotification({version, prUrl, projectRoot})` (SA-0MSQ6K7Z1002H14Z): posts release details + changelog to a configured Discord channel. Runs only after Step 9 (merge verification) succeeds — never on `--dry-run` or failed releases. See [Discord release notification](#discord-release-notification) below.
11. **Close work items (non-blocking)** — `closeWorkItemsAfterRelease(version)`: closes `in_review`/`completed` items, filtering to only close items with `needsProducerReview === false`. Items with `needsProducerReview = true`, `null`, or `undefined` are skipped and logged as "Skipped (needs producer review)". Logs warnings on individual close failures.

### Discord release notification

After a successful, verified release (`verifyReleaseMerge` passed, Step 9), `run-release.js` invokes `sendReleaseNotification()` from `discord-notify.js` alongside the close-work-items step. It is a post-release, non-blocking concern.

**Hook point:** in `run-release.js`, inside the `if (version)` block after `verifyReleaseMerge(version)` succeeds, wrapped in its own `try/catch` — a defect in the notification module can never fail the release.

**Config schema** (key: `discord.webhook_url`):

| Priority | File | Notes |
|----------|------|-------|
| 1 (project) | `<project>/.worklog/config.yaml` | Takes precedence; committed to the repo in most projects — do **not** store the webhook secret here |
| 2 (global) | `~/.pi/agent/config.yaml` | Global fallback, outside any repo — preferred location for the secret |

Neither set → the step logs an info message, skips, and the release completes normally (not an error).

**Behaviour:**
- Message content: released version, git tag `vX.Y.Z`, release date, PR URL, and the new version's changelog section from `CHANGELOG.md` (sections are `## vX.Y.Z (YYYY-MM-DD)` blocks; the date is read from the section header, falling back to today).
- Discord limits: the embed description (changelog) is truncated to ≤ 4096 chars with an ellipsis marker (`truncateForDiscord`).
- Non-blocking: webhook POST uses built-in `fetch` with `AbortSignal.timeout` (default 10 s). Fetch rejection, HTTP error status (incl. 429), timeout, missing/corrupt config, and missing changelog section all log a warning and return a non-failure result — the release exit code is unchanged.
- No new runtime dependencies (Node 18+ built-in `fetch`); no `DISCORD_*` environment variables.
- The webhook URL is a **secret** (contains an auth token) — prefer the global fallback file and never commit it to a repository.

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
node ./skill/ship/scripts/remediate-spurious-closes.js
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
