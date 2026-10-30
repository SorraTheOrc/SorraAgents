# Ship Skill Reference

## Overview

The ship skill automates the `dev` → `main` release workflow and provides
related tooling (`pushToDev`, audit gate, branch checks).  All scripts are
internal — the only user-facing action is `release`.

## Work-item exemption

**No work item is required for a release.** Running a release
(`/skill:ship release`, `run-release.js`) is a work-item-exempt maintenance
action: do **not** create a release work item (nor ask the operator to create
one). This is distinct from the automatic **closure** of `in_review` work items
after a verified release (Release Process step 12, `closeWorkItemsAfterRelease`),
which needs no release work item to exist. See
[SKILL.md](../../SKILL.md#when-to-use).

## Configuration Schema

### Per-project configuration (`<project>/.worklog/config.yaml`)

```yaml
# Required keys (set once per project):
projectName: MyProject       # Human-readable project name
prefix: LP                   # Worklog item prefix

# Optional:
projectDescription: A short 2–3 sentence summary of what the project is.
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

### Project description / pitch (`projectDescription`)

The optional top-level `projectDescription` scalar is resolved with the **same
precedence as `projectName`** (project private → project → global) and is used
verbatim as the leading pitch paragraph of the embed description.

When unset, the pitch is generated from the leading prose of `README.md` (the
block before the first `##` heading, capped at ~1000 characters) via the shared
DeepSeek LLM caller (`scripts/llm.js`), summarised into 2–3 sentences. When
generation is unavailable (no API key, LLM/network error, missing/unreadable
README) the pitch paragraph is **omitted** and the notification still sends. The
pitch is never persisted to `config.yaml`.

### Call-to-action (`cta`, SA-0MUWCIFU2006K39K)

The optional top-level `cta` scalar is resolved with the **same precedence as
`projectName`/`projectDescription`** (project private → project → global). When
set, it is rendered as its own embed paragraph placed **after** the intro block
(pitch → focus → `**<projectName> v<version>**` line) and **immediately before**
the changelog, so the audience can act on the release (play the alpha, join
Discord, etc.). Markdown link syntax in the value is preserved verbatim.

When `cta` is unset, empty, or the config is malformed/unreadable, the
paragraph is simply omitted and the notification still sends — a bad CTA never
fails the release. The value is read verbatim; it is never rewritten or
re-generated.

### Non-blocking semantics (AC3)

A notification failure (network error, HTTP error, timeout) logs a warning and
**never** changes the release exit code.  An already-landed release is never
failed by a notification failure.

### Discord embed limits (AC4)

The fully-composed embed description — including the pitch, focus, project
line, `cta` and changelog — is truncated to 4 096 characters with an ellipsis
marker (`…`) when it exceeds the limit.

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

### Pre-flight audit refresh (`--refresh-audits`)

`run-release.js --refresh-audits [--dry-run]` is a **pre-flight-only** action
(SA-0MUOO5VMH005VF69): it refreshes missing/stale/transient audits for all
`in_review` items via `refresh-audits.js`, then exits **without merging and
without setting the Code Freeze marker**. Exit code 0 when nothing remains, 12
when items still need attention. `--dry-run` reports the plan (`fresh` /
`toRefresh` / `failing`) without invoking the audit runner. The refresh reuses
`planAuditRefresh()` / `refreshAudits()` and the bounded `RemediationBudget`, so
it can never run unbounded. `--refresh-audits` is a wrapper-only flag (never
forwarded to the merge script).

### Step 2: Audit readiness gate (exit 6)

`checkAuditReadyToClose()` (`check-audit-gate.js`) verifies **top-level**
`in_review` items (`parentId == null`) have a passing audit. Children are
covered by their parent's audit and never block.

Missing, **stale**, and transient audits are auto-remediated conservatively:
the gate re-runs `audit_runner.py issue <id>` and re-checks `wl audit-show`,
blocking only if the item still fails after the re-run. A **stale** verdict is
one whose `auditedAt` predates the item's last update (per the shared freshness
buffer/tolerance in `audit-freshness.js`) — the verdict is not trustworthy, so
it is remediated rather than blocking immediately. This is the same staleness
precedence as Step 3.7 (SA-0MUOO5UEB008RXBP): before this change, Step 2
treated a stale failing verdict as genuine and killed the release (exit 6)
before the staleness-aware sweep could run.

A **fresh** genuine "not ready to close" verdict blocks immediately with **no**
re-audit attempt. A remediation-runner failure is treated as blocking with the
manual remediation command surfaced — never silently passed. `--skip-checks`
bypasses the gate.

**Shared freshness module:** `audit-freshness.js` owns `isAuditStale`,
`parseIsoUtc`, and the freshness constants; `check-audit-gate.js` and
`check-final-validation.js` both import them. The module exists to avoid an
import cycle between the two gate modules (`check-final-validation.js` already
imports from `check-audit-gate.js`). `check-final-validation.js` re-exports
these symbols for backward compatibility.

**Bounded remediation (SA-0MUOO5V0P00461X8):** in-gate remediation is bounded
by `audit-remediation.js` so a backlog of slow audits cannot hold the release
(and Code Freeze) for hours. Configuration (env, all optional):

| Env var | Default | Purpose |
|---------|---------|---------|
| `SHIP_AUDIT_REMEDIATION_TIMEOUT_MS` | `1800000` (30 min) | Per-item `audit_runner.py issue` timeout |
| `SHIP_AUDIT_REMEDIATION_BUDGET_MS` | `1800000` (30 min) | Total wall-clock remediation budget per gate run |
| `SHIP_AUDIT_REMEDIATION_MAX_ITEMS` | `5` | Maximum in-gate remediation attempts per gate run |

When the budget is exhausted, the remaining items are reported **blocking**
with an offline-refresh instruction (`python3 skill/audit/scripts/audit_runner.py
batch`), never as "not ready to close". `classifyRemediationError()` labels a
runner failure `timeout` / `concurrency` / `provider` / `error`, so an
infrastructure failure is distinguishable from a work verdict (surfaced as the
`category` field on the blocking entry and in the reason text).

**Narrow audit bypass (`--skip-audit-remediation`, SA-0MUOO5WV0006D7UD):** skips
only the in-gate re-audit attempts. The `readyToClose === true` requirement is
**not** relaxed — a missing/stale/failing audit still blocks with the
offline-refresh guidance. Unlike `--skip-checks` it does not bypass the other
gates, and it is a wrapper-only flag (never forwarded to the merge script).

**Content-fingerprint fast path (SA-0MUOO5W8J001DYTI):** before re-auditing a
time-stale item, the gates call the audit runner read-only
(`queryContentFreshness()` → `audit_runner.py check-freshness <id> --json`,
which reuses the runner's own `_check_audit_freshness`). A fingerprint-bearing
audit whose content is unchanged is treated as trustworthy (no re-audit): a
passing verdict is fresh; a failing verdict is a current genuine failure and
blocks immediately. Legacy (fingerprint-less) audits, content changes, and
probe errors fall back to the time gate / re-audit path. The probe is only
attempted when the stored report actually carries the **versioned**
`Audit content fingerprint (v2):` line — mirroring the runner's
`AUDIT_CONTENT_FINGERPRINT_PREFIX` constant — (`hasContentFingerprint()`), so
fingerprint-less items never spawn the runner. Legacy unversioned reports
(`Audit content fingerprint: <hex>`) are deliberately **not** matched: their
fingerprints were computed with superseded formulas and cannot be recomputed,
so they fall back to the time gate exactly like fingerprint-less audits.

**Content-freshness contract (SA-0MVTU2643004QTQN):** content-fresh audits are
trusted regardless of `updatedAt` — when the stored v2 content fingerprint
still matches the work item, the time gate never overrides that verdict, so a
`wl sync`-moved `updatedAt` can never force a re-audit of unchanged content.
The probe invocation is `audit_runner.py check-freshness <id> --json`; the
runner accepts `--json` (its output is already a single JSON object), and the
command string built by `queryContentFreshness()` is pinned to that accepted
argument shape by tests on both sides. In-gate remediation inherits the same
contract: when `audit_runner.py issue <id>` short-circuits on a content-fresh
audit and leaves a passing-but-time-stale verdict, the shared
`attemptAuditRemediation()` re-queries content freshness and reports the item
as passing (naming the content-fresh basis) — a **passing-but-time-stale audit
must not block** the release. Only a content-changed or unconfirmable audit
blocks, and its reason names the real condition (`time-stale but content
changed`) plus the `audit_runner.py issue <id> --force` remedy. No blocking
entry is ever emitted with the reason `Ready to close`.

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
reported separately. Remediation is bounded by the same
`SHIP_AUDIT_REMEDIATION_*` budget as Step 2 (SA-0MUOO5V0P00461X8): once
exhausted, remaining items block with an offline-refresh instruction. Genuine
"not ready to close" verdicts block immediately with **no** re-audit attempt.
The gate never calls `wl update` directly. `--skip-checks` bypasses it.

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
await sendReleaseNotification({ version, prUrl, projectRoot }, { preRunTags });
```

`preRunTags` is the pre-run `git tag --list` snapshot captured by the wrapper before the merge script ran. The notifier uses it as an independent provenance guard: when the resolved version's tag already existed before the run (or the snapshot is `null`), it refuses to send before resolving the webhook or building a payload (SA-0MV0QEI3Q0063KH3 AC5, defence in depth beneath the Step 8 newly-created-tag gate).

**Behaviour:**

- Only runs on successful, non-dry-run releases (after merge verification).
- **Provenance guard (AC5):** refuses to send (no webhook resolution, no payload, no POST) unless the version tag was newly created by this run.
- Resolves the webhook URL per AC2 precedence.
- Resolves the project pitch: `projectDescription` wins; otherwise README/LLM; otherwise omitted.
- Resolves the project call-to-action (`cta`): project config value wins; otherwise omitted when unset/malformed.
- Extracts the released version's changelog section from `CHANGELOG.md`.
- Extracts and removes the `> **Release focus:** …` marker from the section (no duplication).
- Composes the embed description as pitch → focus → project/version line → `cta` → changelog, truncated to 4 096 chars.
- Builds a Discord embed payload (version, tag, date, PR URL, composed description).
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
| `options.llmFetchFn` | `Function` | No | Injected LLM `fetch` for pitch generation (testing) |
| `options.privateConfigPath` | `string` | No | Override private config path |
| `options.projectConfigPath` | `string` | No | Override project config path |
| `options.globalConfigPath` | `string` | No | Override global config path |
| `options.changelogPath` | `string` | No | Override `CHANGELOG.md` path |
| `options.changelogContent` | `string` | No | Pre-read changelog content |
| `options.preRunTags` | `Set<string>\|string[]\|null` | No | Pre-run `git tag --list` snapshot; presence of `v<version>` (or `null`) refuses the send (AC5). `undefined` = legacy caller, guard not applicable |
| `options.tagCreatedByThisRun` | `boolean` | No | Explicit provenance flag; `false` refuses the send (AC5) |
| `options.readmePath` | `string` | No | Override `README.md` path (pitch fallback) |
| `options.readmeContent` | `string` | No | Pre-read README content |
| `options.now` | `Function` | No | Date provider for fallback date |
| `options.timeoutMs` | `number` | No | Webhook POST timeout (default: 10 000) |

**Returns:** `Promise<{ success: boolean, notified: boolean, skipped?: boolean, reason?: string, error?: string }>`

When the provenance guard refuses, the result is `{ success: true, notified: false, skipped: true, reason: 'version tag not created by this run' }`.

### `resolveDiscordWebhookUrl(projectRoot, options)`

Resolve the Discord webhook URL with precedence (AC2): private → project → global.

**Returns:** `string | null`

### `resolveProjectDescription(projectRoot, options)`

Resolve the project elevator pitch text with the same precedence as `projectName`: private → project → global (top-level `projectDescription` scalar).

**Returns:** `string | null`

### `readCtaFromConfig(configPath)`

Read the top-level `cta` scalar from a YAML config file. Returns `null` when the file is missing/unreadable or the key is absent/empty; markdown link syntax is preserved verbatim.

**Returns:** `string | null`

### `resolveCta(projectRoot, options)`

Resolve the project call-to-action with the same precedence as `projectName`: private → project → global (top-level `cta` scalar). Non-blocking — a malformed/unreadable config yields `null`.

**Returns:** `string | null`

### `resolveProjectPitch(projectRoot, options)`

Resolve the pitch paragraph (async): the configured `projectDescription` wins; otherwise generate from the leading README prose via the shared LLM caller; otherwise `null` (paragraph omitted). Options include `readmePath`, `readmeContent`, `llmFetchFn`.

**Returns:** `Promise<string | null>`

### `extractChangelogSection(changelog, version)`

Extract the changelog section for a given version from `CHANGELOG.md`.

**Returns:** `{ date: string, text: string } | null`

### `extractReleaseFocus(sectionText)`

Extract the `> **Release focus:** …` marker from a changelog section body and return the remaining body with the marker line removed.

**Returns:** `{ focus: string | null, body: string }`

### `truncateForDiscord(text, maxLength)`

Truncate text to Discord embed description limit (4 096 chars).

**Returns:** `string`

### `extractReadmePitch(readmeContent, maxLength)`

Return the leading README prose before the first `##` heading, capped at ~1000 characters.

**Returns:** `string`

### `generatePitch(readmeProse, options)`

Generate a 2–3 sentence elevator pitch from README prose via the shared LLM caller (`llm.js`). Returns `null` when the prose is empty or the LLM is unavailable.

**Returns:** `Promise<string | null>`

### `buildDiscordPayload(details)`

Build the Discord webhook embed payload.

**Parameters:** `version`, `tag`, `date`, `prUrl`, `changelog`, `projectName`, `pitch`, `focus`

**Returns:** `{ embeds: Array<object> }`

### `parseSimpleYaml(content)`

Minimal YAML parser for config files (top-level keys + one nesting level).

**Returns:** `Record<string, Record<string, string> | string>`

## Test Isolation

### Tests never emit real release notifications (WL-0MUYA98Z8003HU8X / SA-0MU3VIW8C0035KKL)

Release integration harnesses run the **real** `run-release.js`, which reaches
Step 8.5 (`sendReleaseNotification`). A harness builds its temp project root
with no `.worklog/` config, so the **real** `discord-notify.js` would fall back
to the operator's global `~/.pi/agent/config.yaml` and POST a real release
notification (e.g. `Release v9.9.9`, or `Release v0.2.0` from the fail-fast
harness) on every test run. Tests must never emit real outbound notifications.

Never copy the real `discord-notify.js` into a harness's temp skill layout.
Install the shared recording stub instead — `writeStubDiscordNotify()` from
`tests/helpers/release-notify-stub.mjs`. The stub performs no network I/O
and records each Step 8.5 call (as a JSON line) to `$DISCORD_NOTIFY_LOG`, so a
test can pin that the stub — not the real module — ran.

Harnesses covered:

- `tests/unit/test-run-release-merge-guard.mjs` — writes the recording stub and
  pins this with assertions that Step 8.5 was served by the stub
  (`stubbed: true`) and that **no** notification is emitted before the merge is
  verified;
- `tests/unit/test-run-release-fail-fast.mjs` — the genuine-release path
  (`gitDescribe: 'v0.2.0'`) reaches Step 8.5 and previously POSTed a real
  `Release v0.2.0`; it now uses the shared stub (with a regression assertion),
  while the fail-fast no-POST tests deliberately keep the **real** module and
  point it at an in-process loopback webhook;
- `tests/unit/test-run-release.mjs`, `tests/unit/test-run-release-arg-filtering.mjs`,
  `tests/unit/test-code-freeze-marker.mjs`, `tests/unit/test-run-release-cleanup.mjs`
  — use the shared stub for the `discord-notify.js` dependency;
- `tests/unit/test-run-release-discord-notify.mjs` — the reference pattern:
  records `sendReleaseNotification` arguments without sending.

Forwarding-only integration tests (e.g. the `--skip-checks` forwarding suite in
`test-run-release-arg-filtering.mjs`) must run `run-release.js` with
`--skip-checks` so the gating-check suite (git/`wl`/audit subprocesses) does not
run inside the test's spawn window. Without it the gates contend with the rest
of a parallel full-suite run and can exceed the spawnSync timeout, flaking the
test (green standalone, red under full-suite parallel load;
SA-0MVE01JCJ00243W1). Gate behaviour is covered by dedicated gate tests.

Production behaviour is unchanged: real releases still post the Discord
notification.

### Worklog isolation

Close-work-items tests must **never mutate the live worklog** (SA-0MSJ2XMQL006CVQS):

- `closeWorkItemsAfterRelease` accepts injectable `getCandidateItemsFn` /
  `runCloseCommand` / `getDescendantsFn` / `getCommentsFn` boundaries.
- Tests inject fakes (or mock `wl`) and never call with the default boundary.

**Idempotency (SA-0MUECDK14002J37J):** the close sweep is safe to re-run. An
item whose comments already carry this version's `Shipped in v<version>` marker
is skipped — never re-closed, never re-annotated — and reported in
`alreadyShippedCount` / `alreadyShippedItems`. The marker match is
version-exact (`v0.1.1` never matches a `v0.1.18` marker). Marker discovery goes
through the injectable `getCommentsFn` boundary (default: a read-only
`wl comment list <id> --json`); a throwing/unreadable boundary is fail-safe — it
warns and leaves the existing close behaviour intact so a release is never
blocked by the check. Additionally, `wl close --force` already annotates every
descendant, so only the topmost closable item of a subtree issues a close
command; its swept descendants are verified via `getCloseStateFn` but never
re-closed. A single sweep therefore produces exactly one close comment per
item (the 2026-10-09 incident produced 99 close comments for 74 items through
both double annotation paths).

**Candidate-set scoping (SA-0MU2OY1N9000XL2H AC9/AC10):** because `wl close
--force` recursively closes ALL descendants, a candidate whose subtree
contains a descendant that is not itself a close candidate must not be
force-closed. Such candidates are **refused** and reported in `refusedItems`
(an explicit, reversible exclusion decision) rather than swept; only
collateral-free candidates are closed. `getDescendants(itemId)` resolves the
subtree recursively via `wl show <id> --children --json` (cycle-bounded).
Terminal descendants (`stage: done` / `status: deleted`) are excluded from the
collateral set, and a non-terminal descendant held back solely by
`needsProducerReview === true` is reported with a distinct producer-review
refusal reason (and `refusedItems[].needsProducerReview`) rather than the
generic collateral list (SA-0MUJKPDAA002VVDP AC2).

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
| `audit-freshness.js` | Shared audit-staleness heuristics (`isAuditStale`, `parseIsoUtc`, freshness constants) used by both audit gates |
| `audit-remediation.js` | Bounded in-gate remediation (`RemediationBudget`, `classifyRemediationError`, env-configurable timeout/budget/attempt cap) |
| `refresh-audits.js` | Pre-flight-only audit refresh (`planAuditRefresh`, `refreshAudits`, `runRefreshAuditsAction`) for `--refresh-audits` |
| `check-final-validation.js` | Final validation sweep (parent-coverage / out-of-scope aware, exit 12) |
| `check-critical-items.js` | Critical item gating |
| `check-worklog-refs.js` | Validate worklog references (JS gate used by `run-release.js`) |
| `release/worklog-ref-gate.sh` | Shell worklog-ref gate used by `release/merge-dev-to-main.sh`; applies the identical content rule (see below) |
| `discord-notify.js` | Post-release Discord notification |
| `remediate-spurious-closes.js` | Idempotent close-comment remediation |
| `timing.js` | Timer / timing utilities |

All scripts are internal — the only user-facing action is `release`.

### Worklog-ref gate (two entry points, one rule)

The worklog-ref gate exists twice because it is applied at two different
layers, but the **rule is identical** and must never diverge
(SA-0MVO7XZAB0095R6F):

| Layer | Implementation | Caller |
|---|---|---|
| Wrapper gate (exit 8) | `check-worklog-refs.js` | `run-release.js` Step 3.5 |
| Merge-script gate (exit 1) | `release/worklog-ref-gate.sh` | `release/merge-dev-to-main.sh` |

Both exclude (a) remote-tracking mirrors `refs/worklog/remotes/**` and (b) any
`refs/worklog/*` ref whose commit contains the worklog data file
`.worklog/worklog-data.jsonl` — `wl sync` bookkeeping such as
`refs/worklog/data`, `refs/worklog/fetched-check` and
`refs/worklog/tmp-reauthor`. Every remaining ref is a dangerous orphan and
blocks the release; a ref whose data-file presence cannot be determined is
treated as dangerous (fail closed).

`worklog-ref-gate.sh [<repo-path>]` can be run standalone for diagnosis:
exit 0 = clean, exit 1 = dangerous ref(s) printed to stderr, exit 2 = usage
error / not a git repository. `tests/unit/test-worklog-ref-gate.mjs` asserts
the behaviour of both entry points **and** their parity on the same throwaway
repository.

> **Historical bug (fixed):** the merge script previously inlined a
> *name-based* check that only special-cased `refs/worklog/data`, so once
> `wl sync` created `refs/worklog/fetched-check` / `tmp-reauthor` every release
> aborted in the merge script immediately after the wrapper's worklog-ref gate
> had passed. Do not re-inline a name-based check in the merge script.
