---
name: ship
description: "Canonical dev-to-main release workflow with automated gating. Use when: '/skill:ship release'."
---

# Ship Skill

Canonical agent-side dev-to-main release execution with automated gating.

## Purpose

Provide a single, deterministic release workflow: `dev` is promoted to `main` via a gated, PR-based merge. Helper functions (branch naming, validation, unmerged-branch detection, audit readiness, etc.) are internal details.

## When To Use

Execute a release (promote `dev` to `main`). Triggers: "ship it", "shipit", "ship", "release", "promote dev", "merge dev to main", "release the changes" — all map to `release`.

## How Agents Invoke This Skill

```
/skill:ship release
```

## Prerequisites

**Node.js** 18+, **git**, **gh** CLI, **wl** CLI, **jq** CLI

## Internal Scripts and Modules

All scripts are internal implementation details — the only user-facing action is `release`. Full inventory: [docs/dev/ship-skill-reference.md](../../docs/dev/ship-skill-reference.md). Key scripts: `run-release.js` (release wrapper + gating + dev sync), `release/merge-dev-to-main.sh` (canonical merge), `ship.js` (`pushToDev`), `git-helpers.js` (branch naming/policy), `check-unmerged-branches.js`, `check-audit-gate.js`, `check-critical-items.js`, `check-worklog-refs.js`, `discord-notify.js` (post-release Discord notification), `remediate-spurious-closes.js`.

> **Path resolution:** all `$(skill_path ship)/scripts/...` references in this
> document are resolved at runtime by the **`skill_path` shell shim**
> (installed at `~/.pi/agent/bin/skill_path`). When pasting code blocks into
> bash, `$(skill_path ship)` expands via the shim to the absolute skill
> directory — it works from any project CWD. Never use `./scripts/...` or
> `../` relative paths in commands. The shim mirrors the pi `skill_path`
> tool's search order (`~/.pi/agent/skills/<name>`, then `<cwd>/.pi/skills/<name>/`).

## Usage

```bash
# Execute a release (dev → main merge)
node $(skill_path ship)/scripts/run-release.js
```

For programmatic access to internal helpers (used by the implement workflow),
import the modules from the skill directory resolved via `skill_path` (e.g.
`$(skill_path ship)/scripts/ship.js`):

```javascript
import { pushToDev } from '<skill-path>/scripts/ship.js';  // <skill-path> = $(skill_path ship) resolved via the tool
const result = pushToDev('origin');
if (!result.success) { /* handle failure, e.g. create a merge-conflict work item */ }
import { makeBranchName, validateBranchName, isBranchBlocked } from '<skill-path>/scripts/git-helpers.js';  // <skill-path> = $(skill_path ship)
makeBranchName('SA-001', 'fix-login-bug');     // → 'wl-SA-001-fix-login-bug'
validateBranchName('wl-SA-001-fix-login-bug'); // → { valid: true }
isBranchBlocked('main');                       // → true
```

## Gating

The `release` action runs five gating checks before merging `dev` to `main`:

1. **Unmerged branches check** — abort if feature branches pending; exit 3.
2. **Audit readiness gate** — verifies **top-level** `in_review` items (`parentId == null`) pass audits; exit 6. Child items are covered by their parent's audit and never block. Missing/transient audits (timeout, provider error, FailureNotice) are **auto-remediated conservatively**: the gate re-runs `audit_runner.py issue <id>` and re-checks `wl audit-show`, blocking only if the item still fails after the re-run; successfully-remediated items are reported separately. Genuine "not ready to close" verdicts block immediately with **no** re-audit attempt. A remediation-runner failure is treated as blocking with the manual remediation command surfaced — never silently passed.
3. **Critical-items gate** — abort if non-terminal critical items exist; exit 7.
4. **Worklog refs gate** — abort if worklog refs remain in merged code; exit 8.
5. **Producer-review gate** — abort if **top-level** items need producer review; exit 9. Child items (covered by their parent's review) never block.

All gates bypass with `--skip-checks`. CI is **optional**: PR status checks must pass if present; none → merge proceeds without waiting.

### Code Freeze

While a release runs, the ship skill sets a **Code Freeze marker** at `.worklog/code-freeze.json` (contract WL-0MSBU4KMA004PKSR), written **before** gating and cleared on **every** exit path (success, failure, abort, `--dry-run`, gating failures) via `try/finally` + an `EXIT` trap. While present, the implement skill refuses to start new implementation (fail-open: missing/corrupt marker never blocks). Stale markers can be removed by deleting the file. Schema: [docs/dev/ship-skill-reference.md](../../docs/dev/ship-skill-reference.md).

### Exit Codes

| Code | Meaning |
|------|---------|
| 1 | General error |
| 2 | Missing release script |
| 3 | Unmerged branches found |
| 4 | PR merge failed |
| 5 | Dev sync failed |
| 6 | Audit gate failure — top-level `in_review` item(s) lack a passing audit after conservative auto-remediation (missing/transient audits are re-run automatically; genuine "not ready to close" verdicts block immediately) |
| 7 | Critical-items gate failure |
| 8 | Worklog-ref gate failure |
| 9 | Producer-review gate failure — top-level `in_review` item(s) flagged for producer review (`needsProducerReview != false`) |
| 10 | Release script timed out (`SHIP_RELEASE_TIMEOUT_MS`, default 600s) |
| 11 | Release merge verification failed (no verified dev→main merge) |

## Release Process

```bash
node $(skill_path ship)/scripts/run-release.js
```

1. **Unmerged branches check** — abort if branches pending; `--skip-checks` bypasses.
2. **Pre-flight checks** — verify `gh`, `wl`, clean worktree.
3. **Critical-priority items check** — exit 7 if non-terminal critical items exist.
4. **Merge commit** — fetch dev/main, `--no-ff` merge commit.
5. **PR creation** — push `release/dev-to-main-<timestamp>`, create PR to `main`.
6. **Status check wait & merge** — if the PR has status checks, wait for them (default 10 min), then `gh pr merge --merge --delete-branch`; no checks → merge immediately; `--force` skips the wait.
7. **Audit logging** — record merge hash, PR URL in worklog.
8. **Sync dev with main** — `syncDevWithMain()`: fetch, checkout dev, merge origin/main, push. Release ops run from **main checkout**, not worktrees.
9. **Verify the release merge (gating)** — `verifyReleaseMerge(version)` (SA-0MSJ2XMQL006CVQS): close only after the release landed on main — tag `v<version>` exists on origin AND is an ancestor of `origin/main`; else exit 11, no items closed.
10. **Discord notification (non-blocking)** — `sendReleaseNotification({version, prUrl, projectRoot})` posts version, tag (`vX.Y.Z`), release date, PR URL, and the new version's changelog section from `CHANGELOG.md` to a configured Discord channel via webhook. Runs only after merge verification (never on `--dry-run` or failed releases). Failure (network, HTTP error, timeout) logs a warning and never changes the release exit code. See [Discord release notification](#discord-release-notification).
11. **Close work items (non-blocking)** — `closeWorkItemsAfterRelease(version)`: close `in_review`/`completed` items only when `needsProducerReview === false`; others skipped + logged.

### Discord release notification

After a successful, verified release the ship skill posts release details + changelog to a Discord channel (SA-0MSQ6K7Z1002H14Z).

**Config schema** — the webhook URL is read from `discord.webhook_url`:

```yaml
# <project>/.worklog/config.yaml (per-project, takes precedence)
discord:
  webhook_url: https://discord.com/api/webhooks/<id>/<token>
```

```yaml
# ~/.pi/agent/config.yaml (global fallback — preferred for shared setups)
discord:
  webhook_url: https://discord.com/api/webhooks/<id>/<token>
```

- **Precedence (AC2):** per-project `.worklog/config.yaml` first; global `~/.pi/agent/config.yaml` fallback. Neither set → the step is skipped with an info log and the release completes normally (no error).
- **Non-blocking (AC3):** a failed or slow webhook POST logs a warning and does not change the release exit code; an already-landed release is never failed by a notification failure.
- **Discord limits (AC4):** the embed description (changelog) is truncated to ≤ 4096 chars with an ellipsis marker.
- **Secret:** the webhook URL contains an auth token — do **not** commit it to a repository. Prefer the global `~/.pi/agent/config.yaml` (outside any repo); a repo may override via its own `.worklog/config.yaml` but must then keep that file out of version control or accept the exposure.

#### Test isolation (mandatory)

Close-work-items unit tests must **never mutate the live worklog** (SA-0MSJ2XMQL006CVQS): `closeWorkItemsAfterRelease` accepts injectable `getCandidateItemsFn`/`runCloseCommand` boundaries; tests must inject fakes (or mock `wl`) and never call it with the default boundary outside a real, verified release.

### Remediation sweep: test-spuriously-closed work items

If the test-isolation bug recurs (items closed "Shipped in v1.0.0"/"v1.2.3" that never shipped), run the idempotent sweep from the main checkout: `node $(skill_path ship)/scripts/remediate-spurious-closes.js` — deletes close comments authored by `worklog` with exactly those reasons and restores each item to `status=completed, stage=in_review`. Legitimate close comments (real versions) are never touched; re-running after success is a no-op. Details: [docs/dev/ship-skill-reference.md](../../docs/dev/ship-skill-reference.md).

## Fallback: Human Release Manager

For repos where the automated merge is unsuitable, follow [`docs/dev/release-process.md`](../../docs/dev/release-process.md).

| Approach | Description | When to use |
|----------|-------------|-------------|
| **Automated script** | `node $(skill_path ship)/scripts/run-release.js` manually | Script available |
| **Direct merge** | `git checkout main && git merge origin/dev --no-ff` | No branch protection on main |
| **Manual PR** | Temp branch with merge result, open a PR | Human review desired |

### Pre-merge checklist

1. No open merge conflicts `dev`↔`main`. 2. No open critical items (automated; `--skip-checks` bypasses). 3. Configured CI checks pass (step 6); no CI → satisfied. 4. `CHANGELOG.md` auto-generated by the release script.

### Cached test verification at release time

Verifying the full suite is green before promoting `dev` to `main` is an **optional pre-release step** driven by the [test skill](../test/SKILL.md) (`/skill:test`). Route repeat verifications through the **cached runner** (`test_cache.py`, SA-0MSGN5OJ4002OZKY):

```bash
python3 $(skill_path test)/scripts/run_tests.py --scope full --json                    # fresh full-suite run (populates cache)
python3 $(skill_path test)/scripts/run_tests.py --summary --suite all                   # read-only summary (shows cached scope), never executes
python3 $(skill_path test)/scripts/run_tests.py --scope full --force --json            # fresh full-suite run for the final gate
```

The run and final-gate commands use ``--scope full`` explicitly: the release
gate must be backed by **full-suite** evidence. A ``changed``-scope (partial)
cached result is never sufficient for a release — and ``--summary`` reports
the cached scope so a partial summary is not mistaken for full-suite
verification — see scope-aware test execution
([test-skill reference](../../docs/dev/test-skill-reference.md)).

Cached results are valid for the same git state within the 2-hour TTL; a changed tree, expired TTL, or corrupt entry always triggers a fresh run. See [`docs/dev/release-tests.md`](../../docs/dev/release-tests.md).

## Preferred execution behaviour (policy)

- Always invoke `$(skill_path ship)/scripts/run-release.js` for dev→main merges; do NOT substitute ad-hoc git commands.
- Manual fallback only in narrow edge cases: script missing, fails with operator-okayed fallback, or human explicitly requests manual steps.
- Script unavailable → refuse automatic release and direct operator to `docs/dev/release-process.md`.

## Preconditions & safety

- Never force-push or rewrite history on `main` or `dev`; never bypass required status checks unless `--force` is explicitly instructed.
- Always log merge audit via `wl comment add`; never push directly to `main` — all merges go through a PR satisfying branch protection.

## Integration with AGENTS.md

The implement workflow uses `pushToDev()` internally; the ship `release` action promotes `dev` to `main`. See [AGENTS.md](../../AGENTS.md) and [[concepts/git-worktree-best-practices-for-agent-workflows]].

## Outputs

GitHub PR `release/dev-to-main-<timestamp>` → `main`; worklog audit comment (merge hash + PR URL); operator notification summarising the merge.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary.
