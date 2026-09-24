# Ship Skill Reference

## Overview

The ship skill automates the `dev` → `main` release workflow and provides
related tooling (`pushToDev`, audit gate, branch checks).  All scripts are
internal — the only user-facing action is `release`.

## Configuration Schema

### Per-project configuration (`<project>/.worklog/config.yaml`)

```yaml
# Required keys (set once per project):
projectName: MyProject       # Human-readable project name
prefix: LP                   # Worklog item prefix
```

This file is version-controlled and must contain **only non-secret settings**.
Never store the Discord webhook (or any other secret) here.

### Per-project private configuration (`<project>/.worklog/config.private.yaml`)

```yaml
# ── Optional: Discord release notification (SA-0MSQ6K7Z1002H14Z) ──
# Gitignored — never committed. Copy from .worklog/config.private.yaml.example.
discord:
  webhook_url: https://discord.com/api/webhooks/<id>/<token>
```

The `discord.webhook_url` field is a **secret** containing an auth token.
`.worklog/config.private.yaml` is explicitly gitignored, so it must never be
committed. A placeholder template is provided at
`.worklog/config.private.yaml.example`.

### Global configuration fallback (`~/.pi/agent/config.yaml`)

```yaml
# ── Optional: Discord release notification (SA-0MSQ6K7Z1002H14Z) ──
discord:
  webhook_url: https://discord.com/api/webhooks/<id>/<token>
```

### Config precedence (AC2)

1. Per-project `.worklog/config.private.yaml` → `discord.webhook_url` (gitignored secret)
2. Per-project `.worklog/config.yaml` → `discord.webhook_url` (tracked, non-secret)
3. Global `~/.pi/agent/config.yaml` → `discord.webhook_url` (fallback)
4. Neither set → notification skipped (info log, release proceeds)

The first file that defines `discord.webhook_url` wins. The existing global
value can be migrated to `.worklog/config.private.yaml` for per-project
isolation; the global fallback remains supported.

### Non-blocking semantics (AC3)

A notification failure (network error, HTTP error, timeout) logs a warning and
**never** changes the release exit code.  An already-landed release is never
failed by a notification failure.

### Discord embed limits (AC4)

The changelog section in the embed description is truncated to 4 096
characters with an ellipsis marker (`…`) when it exceeds the limit.

## Release Workflow

### Step-by-step

| Step | Description | Blocking? |
|------|-------------|-----------|
| 1 | Pre-flight checks (`gh`, `wl`, clean worktree) | Yes |
| 2 | Critical-priority items check (exit 7 if non-terminal) | Yes |
| 2.5 | Final validation sweep — top-level + uncovered children, parent-coverage aware (exit 12) | Yes |
| 3 | Merge commit (`--no-ff`) | Yes |
| 4 | PR creation (`release/dev-to-main-<timestamp>`) | Yes |
| 5 | Status check wait & merge (default 10 min) | Yes |
| 6 | Audit logging (merge hash, PR URL) | No |
| 7 | Sync dev with main (`syncDevWithMain()`) | No |
| 8 | Verify release merge (gating — tag exists, ancestor of main) | Yes |
| 8.5 | Discord notification (non-blocking) | No |
| 9 | Close work items (non-blocking) | No |

### Step 3.7: Final validation sweep (exit 12)

The final validation sweep (`check-final-validation.js`, SA-0MTMSPKEX003JGIX,
SA-0MU2OY1N9000XL2H) complements the scoped audit gate. It queries **every**
`in_review` item — no `parentId` filter — but resolves each child's scope
first (parent-coverage / out-of-scope rules):

**Child scope precedence (first match wins):**

1. Parent **does not exist** (deleted) → `excluded` (out of release scope).
2. Parent stage is **not** `in_review` → `excluded`.
3. Nearest `in_review` ancestor with a **passing** audit (`readyToClose === true`) → `covered`
   (the child is skipped; its own audit/flag is not consulted). The Step-2
   audit gate is authoritative for top-level readiness, so a passing parent
   audit covers even when the conservative time-gate heuristic would label it
   stale.
4. Otherwise → `uncovered` (evaluate the child's own audit/flag).

A missing, transient, or failing (`readyToClose !== true`) `in_review`
ancestor does not provide coverage; the walk continues up the chain so a higher
passing `in_review` ancestor can still cover the child. Cycles are broken
conservatively (`uncovered`).

**Blocking (exit 12):** a top-level item or an **uncovered child** with:

1. a **missing** audit (no audit record);
2. a **stale** audit (audit predates the item's last update, per the same
   freshness buffer/tolerance constants as the audit runner);
3. a **failing** audit (a fresh "not ready to close" verdict); or
4. a producer-review flag (`needsProducerReview === true`).

Covered and excluded children are reported in `coveredChildren` /
`excludedChildren` and never block. Missing, stale, and transient audits are
auto-remediated conservatively by re-running `audit_runner.py issue <id>` and
re-checking `wl audit-show`; successfully-remediated items are unblocked and
reported separately. Genuine "not ready to close" verdicts block immediately
with **no** re-audit attempt. The gate never calls `wl update` directly.
`--skip-checks` bypasses it.

**Script:** `scripts/check-final-validation.js`

**Exports:**

| Export | Purpose |
|--------|---------|
| `checkFinalValidation(options)` | Runs the sweep; returns `{ hasBlockingItems, blockingItems, remediatedItems, coveredChildren, excludedChildren, passingCount, message }` |
| `getInReviewItems()` | Queries all `in_review` items (id, title, needsProducerReview, parentId, updatedAt) |
| `getItemById(itemId)` | Resolves a single item (any stage) for parent-chain coverage; `null` = deleted/missing |
| `resolveChildScope(item, boundaries)` | Resolves a child as `covered`/`excluded`/`uncovered` |
| `classifyAudit(workItem, auditData)` | Classifies an audit as `missing`/`transient`/`stale`/`failing`/`passing` |
| `isAuditStale(workItem, auditData)` | Time-gate staleness check (mirrors the audit runner's freshness floor) |
| `parseIsoUtc(value)` | ISO-8601 parse helper (naive timestamps treated as UTC) |

All command boundaries (`getItemsFn`, `getItemByIdFn`, `runAuditShow`,
`runAuditCommand`, `resolveAuditRunnerFn`) are injectable for hermetic tests.

### Step 8.5: Discord notification

After `verifyReleaseMerge()` succeeds, `sendReleaseNotification()` is called:

```javascript
await sendReleaseNotification({ version, prUrl, projectRoot });
```

**Behaviour:**

- Only runs on successful, non-dry-run releases (after merge verification).
- Resolves the webhook URL per AC2 precedence.
- Extracts the released version's changelog section from `CHANGELOG.md`.
- Builds a Discord embed payload (version, tag, date, PR URL, changelog).
- POSTs to the webhook via built-in `fetch` (10s timeout).
- On failure: logs a warning, returns `{ success: true, notified: false }`.
- The release exit code is **never** changed by notification failure.

**Script:** `scripts/discord-notify.js`

## API — `discord-notify.js`

### `sendReleaseNotification(release, options)`

Post-release Discord notification (non-blocking).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `release.version` | `string` | Yes | Semver version (e.g. `"1.2.3"`) |
| `release.prUrl` | `string` | No | Release PR URL |
| `release.projectRoot` | `string` | No | Project root (default: `process.cwd()`) |
| `options.fetchFn` | `Function` | No | Injected `fetch` for testing |
| `options.projectConfigPath` | `string` | No | Override project config path |
| `options.globalConfigPath` | `string` | No | Override global config path |
| `options.changelogPath` | `string` | No | Override `CHANGELOG.md` path |
| `options.changelogContent` | `string` | No | Pre-read changelog content |
| `options.now` | `Function` | No | Date provider for fallback date |
| `options.timeoutMs` | `number` | No | Webhook POST timeout (default: 10 000) |

**Returns:** `Promise<{ success: boolean, notified: boolean, skipped?: boolean, reason?: string, error?: string }>`

### `resolveDiscordWebhookUrl(projectRoot, options)`

Resolve the Discord webhook URL with precedence (AC2): private → project → global.

**Returns:** `string | null`

### `extractChangelogSection(changelog, version)`

Extract the changelog section for a given version from `CHANGELOG.md`.

**Returns:** `{ date: string, text: string } | null`

### `truncateForDiscord(text, maxLength)`

Truncate text to Discord embed description limit (4 096 chars).

**Returns:** `string`

### `buildDiscordPayload(details)`

Build the Discord webhook embed payload.

**Parameters:** `version`, `tag`, `date`, `prUrl`, `changelog`

**Returns:** `{ embeds: Array<object> }`

### `parseSimpleYaml(content)`

Minimal YAML parser for config files (top-level keys + one nesting level).

**Returns:** `Record<string, Record<string, string> | string>`

## Test Isolation

Close-work-items tests must **never mutate the live worklog** (SA-0MSJ2XMQL006CVQS):

- `closeWorkItemsAfterRelease` accepts injectable `getCandidateItemsFn` /
  `runCloseCommand` / `getDescendantsFn` boundaries.
- Tests inject fakes (or mock `wl`) and never call with the default boundary.

**Candidate-set scoping (SA-0MU2OY1N9000XL2H AC9/AC10):** because `wl close
--force` recursively closes ALL descendants, a candidate whose subtree
contains a descendant that is not itself a close candidate must not be
force-closed. Such candidates are **refused** and reported in `refusedItems`
(an explicit, reversible exclusion decision) rather than swept; only
collateral-free candidates are closed. `getDescendants(itemId)` resolves the
subtree recursively via `wl show <id> --children --json` (cycle-bounded).

## Remediation: test-spuriously-closed items

If items are spuriously closed during releases, run the idempotent sweep:

```bash
node $(skill_path ship)/scripts/remediate-spurious-closes.js
```

Deletes close comments authored by `worklog` with reasons matching
`"Shipped in vX.Y.Z"` and restores items to `status=completed, stage=in_review`.

## Release Test Cache

Verifying the full suite before promotion uses the test skill's cached runner
(test_cache.py, SA-0MSGN5OJ4002OZKY).  See
[ship-skill SKILL.md](../SKILL.md) for full command examples.

## Scripts Inventory

| Script | Purpose |
|--------|---------|
| `run-release.js` | Release orchestration (gates, merge, verify, notify) |
| `release/merge-dev-to-main.sh` | Canonical dev → main merge |
| `ship.js` | `pushToDev` helper |
| `git-helpers.js` | Branch naming & policy |
| `check-unmerged-branches.js` | Detect unmerged branches |
| `check-audit-gate.js` | Pre-release audit gate |
| `check-final-validation.js` | Final validation sweep (parent-coverage / out-of-scope aware, exit 12) |
| `check-critical-items.js` | Critical item gating |
| `check-worklog-refs.js` | Validate worklog references |
| `discord-notify.js` | Post-release Discord notification |
| `remediate-spurious-closes.js` | Idempotent close-comment remediation |
| `timing.js` | Timer / timing utilities |

All scripts are internal — the only user-facing action is `release`.
