/**
 * check-audit-gate.js — Audit readiness and producer-review gating for the ship skill.
 *
 * This module provides gating functions for the release process:
 *   1. Audit readiness — checks **top-level** `in_review` items have
 *      `audit.readyToClose`. Missing/stale/transient audits are auto-remediated
 *      (stale semantics shared with the Step-3.7 sweep via ./audit-freshness.js);
 *      a fresh genuine failure blocks.
 *   2. Producer review — checks that no candidate items have `needsProducerReview = true`.
 *
 * Both gates are complementary and must pass before a release proceeds.
 *
 * Usage:
 *
 *   import { checkAuditReadyToClose, checkProducerReviewStatus } from './check-audit-gate.js';
 *
 *   // Audit gate
 *   const auditReport = await checkAuditReadyToClose();
 *   if (auditReport.hasBlockingItems) {
 *     console.log(auditReport.message);
 *     // Release is blocked with exit code 6
 *   }
 *
 *   // Producer-review gate
 *   const items = getCandidateItems();
 *   const reviewReport = checkProducerReviewStatus(items);
 *   if (reviewReport.hasBlockingItems) {
 *     console.log(reviewReport.message);
 *     // Release is blocked with exit code 9
 *   }
 */

import { execSync } from 'node:child_process';
import { existsSync, readFileSync, unlinkSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isAuditStale } from './audit-freshness.js';

// Prefix of the versioned content-fingerprint metadata line embedded in
// persisted audit reports by the audit runner (`audit_runner.py`). Mirrored
// here so the gate can detect a v2-bearing audit without spawning the runner
// (SA-0MUOO5W8J001DYTI, SA-0MVIDICGZ0030DXC). Legacy unversioned lines
// (`Audit content fingerprint: <hex>`) are deliberately NOT matched: their
// fingerprints were computed with superseded formulas and cannot be recomputed,
// so they fall back to the local time gate instead of spawning a probe that
// would only reproduce it.
export const AUDIT_CONTENT_FINGERPRINT_PREFIX = 'Audit content fingerprint (v2): ';
import {
  RemediationBudget,
  classifyRemediationError,
  resolveRemediationTimeoutMs,
  OFFLINE_AUDIT_REFRESH_HINT,
} from './audit-remediation.js';

// Module constants for audit-runner path resolution: the in-repo copy of the
// audit skill is preferred; the globally installed skill is the fallback.
const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..', '..');
const HOME_DIR = homedir();

// ------------------------------------------------------------------
// Abort-recovery: leftover audit state cleanup (SA-0MTRH8C5O005URGR).
// When the audit runner is killed (SIGKILL) it cannot remove its
// abort-recovery state file. The parent process checks for leftover
// files after each execSync and restores the work item if needed.
// ------------------------------------------------------------------

/**
 * Build the shell command that restores a work item to its pre-audit state.
 *
 * Exported for testability and reuse. The command mirrors the audit
 * runner's own restore branch (``_apply_terminal_lifecycle``) — never a
 * bare ``wl update`` without the captured state.
 *
 * @param {string} issueId - The work item id.
 * @param {string} status - The captured pre-audit status.
 * @param {string} [stage] - The captured pre-audit stage.
 * @returns {string} The ``wl update`` command string.
 */
export function buildAbortRestoreCommand(issueId, status, stage) {
  return (
    `wl update ${issueId} --status ${status} ` +
    `--stage ${stage || 'plan_complete'} ` +
    `--assignee "" --json`
  );
}

/**
 * Check for a leftover audit state file and restore the work item
 * if the audit runner was killed before cleaning up.
 *
 * When the audit runner is SIGKILLed it cannot run its ``finally`` block,
 * so the pre-audit state file it wrote before the ``in_progress`` claim is
 * left behind. This function finds that file and restores the item — the
 * parent-process half of the abort-recovery contract
 * (SA-0MTRH8C5O005URGR).
 *
 * Boundaries are injectable (mirroring the other gate helpers) so unit
 * tests are hermetic — no live ``wl`` invocations.
 *
 * @param {string} workItemId - The work item id to check.
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {string|null} [options.baseDir] - Base directory for state files;
 *   defaults to the repo root, then /tmp.
 * @param {object} [options.fsMod] - fs-like module (``existsSync``,
 *   ``readFileSync``, ``unlinkSync``); defaults to ``node:fs``.
 * @param {(cmd: string) => string} [options.runCommand] - Command runner;
 *   defaults to ``execSync``.
 * @returns {boolean} True if a leftover state file was found and restored.
 */
export function checkAndRestoreStuckAudit(workItemId, options = {}) {
  const {
    baseDir = null,
    fsMod = { existsSync, readFileSync, unlinkSync },
    runCommand = (cmd) => execSync(
      cmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
  } = options;

  // State file naming: .audit_state_<id>.json — matches audit_runner.py.
  const dirs = [
    baseDir,
    REPO_ROOT,
    '/tmp',
  ].filter(Boolean);

  for (const dir of dirs) {
    const stateFile = join(dir, `.audit_state_${workItemId}.json`);
    if (fsMod.existsSync(stateFile)) {
      try {
        const data = JSON.parse(fsMod.readFileSync(stateFile, 'utf-8'));
        if (data.issue_id === workItemId && data.original_status) {
          console.log(
            `Aborted audit for ${workItemId}: restoring "${data.original_status}" "${data.original_stage || 'plan_complete'}".`,
          );
          runCommand(
            buildAbortRestoreCommand(
              data.issue_id, data.original_status, data.original_stage,
            ),
          );
        }
      } catch (err) {
        // Best-effort: silently ignore corrupt state files.
      } finally {
        // Always remove the file, even if restore failed.
        try {
          fsMod.unlinkSync(stateFile);
        } catch (err) {
          // Best-effort cleanup.
        }
      }
      return true;
    }
  }
  return false;
}

// ── getCandidateItems ────────────────────────────────────────────────────────

/**
 * Query Worklog for candidate work items.
 *
 * Release candidates are exactly the items with `stage: in_review` (status
 * `completed` — per the stage/status model, `in_review` items have status
 * `completed`; items stuck in `in_progress` are NOT candidates). A single
 * `--stage in_review` query replaces the previous two-query union
 * (SA-0MSPPDCTH004561Z): the old `--status completed` arm contributed zero
 * candidates (completed-minus-done == in_review) while re-downloading
 * ~4.9 MB of already-released items.
 *
 * The full `wl list --json` output for a large worklog can exceed
 * execSync's default 1 MB buffer (ENOBUFS), so the query is piped through
 * `jq` and only the needed field projection crosses into Node's buffer
 * (the OS pipe between `wl` and `jq` is unbounded). `set -o pipefail`
 * ensures a `wl` failure still surfaces as an execSync error so the
 * warning path below fires.
 *
 * Extracts `needsProducerReview` from each item's `wl list` output,
 * defaulting to `null` when the field is missing. This field is used
 * by `checkProducerReviewStatus()` for the producer-review gating step.
 * Also projects `parentId` (SA-0MSUT8GQP004WSYN) so callers can
 * distinguish top-level items from children; `getTopLevelCandidateItems()`
 * uses it to scope the release gates. `updatedAt` is projected so
 * `getAuditStatus()` can evaluate audit staleness (SA-0MUOO5UEB008RXBP).
 *
 * Invoked via `bash -c` (not plain /bin/sh) because `set -o pipefail`
 * is a bash-ism not supported by dash (Ubuntu/Debian's default sh) —
 * see LP-0MSQ0NTMO00577UJ.
 *
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null, updatedAt: string|null }>}
 */
export function getCandidateItems() {
  try {
    // Single query: stage=in_review implies status=completed (the
    // completed-minus-done == in_review invariant), piped through jq so only
    // {id, title, needsProducerReview, parentId, updatedAt} enters the execSync
    // buffer (updatedAt lets the gate evaluate audit staleness —
    // SA-0MUOO5UEB008RXBP).
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c \"[.workItems[] | {id, title, needsProducerReview, parentId, updatedAt}]\"'`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    );
    const projected = JSON.parse(output);
    if (!Array.isArray(projected)) {
      return [];
    }
    // jq emits null for a missing needsProducerReview/parentId; normalize
    // them to null (the same default the previous extraction applied for
    // undefined).
    return projected.map((item) => ({
      id: item.id,
      title: item.title || item.id,
      needsProducerReview: item.needsProducerReview !== undefined
        ? item.needsProducerReview
        : null,
      parentId: item.parentId !== undefined ? item.parentId : null,
      updatedAt: item.updatedAt !== undefined ? item.updatedAt : null,
    }));
  } catch (err) {
    console.error(`Warning: Failed to query release candidates: ${err.message}`);
    return [];
  }
}

// ── getTopLevelCandidateItems ────────────────────────────────────────────────

/**
 * Query Worklog for top-level candidate work items only.
 *
 * Same query and projection as {@link getCandidateItems}, but filters to
 * items with `parentId == null`. Children are audited (and covered) as part
 * of their parent's audit/review, so the release gates must consider only
 * top-level items — a child-level audit gap must not spuriously block a
 * release (SA-0MSUT8GQP004WSYN). An orphaned `in_review` item with no
 * parent (`parentId == null`) IS top-level and remains gated.
 *
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null, updatedAt: string|null }>}
 */
export function getTopLevelCandidateItems() {
  return getCandidateItems().filter((item) => item.parentId === null);
}

// ── getAuditStatus ───────────────────────────────────────────────────────────

// Patterns that indicate the audit pipeline failed transiently (timeout,
// provider error, script execution failure) rather than reaching a genuine
// 'not ready to close' verdict. When the audit's stored report matches one of
// these markers, the gate treats the item as transient (warning, not blocking)
// so a release is not spuriously blocked by a merely timed-out audit.
const TIMEOUT_TRANSIENT_PATTERNS = [
  /\btime(?:d)?\s*outs?\b/i, // "timed out", "time out", "timeout(s)"
  /provider\s+(?:error|stop\s+reason)/i, // "Pi provider error: ..."
  /script\s+execution\s+failure/i, // FailureNotice banner
];

/**
 * Detect whether an audit's stored report indicates a timeout or transient
 * failure rather than a genuine 'not ready to close' verdict.
 *
 * Inspects the audit's ``rawOutput`` and ``summary`` (whichever is present)
 * for markers produced by the audit runner's timeout/skip/provider-error
 * paths (e.g. "Deep analysis timed out — manual review required.",
 * "Pi provider error: ...", "Script Execution Failure: ...").
 *
 * @param {object|null} audit - The audit object from ``wl audit-show``.
 * @returns {boolean} True if the audit appears to have timed out or hit a
 *   transient failure; false for genuine verdicts.
 */
export function isTimeoutOrTransientAudit(audit) {
  if (!audit || typeof audit !== 'object') {
    return false;
  }
  const haystack = [audit.rawOutput, audit.summary]
    .filter((v) => typeof v === 'string' && v.length > 0)
    .join('\n');
  return TIMEOUT_TRANSIENT_PATTERNS.some((pattern) => pattern.test(haystack));
}

/**
 * Check the audit status for a single work item.
 *
 * Determines whether the item's audit is blocking (not ready to close),
 * transient (timed out / hit a transient failure — reported as a warning,
 * NOT blocking), or passing (ready to close).
 *
 * @param {{ id: string, title: string }} workItem - The work item to check.
 * @param {object|null} auditData - The parsed audit data (or null if no audit).
 * @param {object|null} [auditData.audit] - The audit object from wl audit-show.
 * @returns {{
 *   isBlocking: boolean,
 *   transient: boolean,
 *   stale: boolean,
 *   passing: boolean,
 *   reason: string,
 *   summary: string|null
 * }}
 */
export function getAuditStatus(workItem, auditData) {
  // No audit data or audit is null → blocking (missing).
  if (!auditData || auditData.audit === null || auditData.audit === undefined) {
    return {
      isBlocking: true,
      transient: false,
      stale: false,
      passing: false,
      reason: 'No audit found',
      summary: null,
    };
  }

  const audit = auditData.audit;

  // Check readyToClose. A passing verdict is never blocking; the `stale` flag
  // is still reported so callers (e.g. classifyAudit) can distinguish a fresh
  // pass from an outdated one, while parent-coverage logic keys off `passing`
  // only (a stale-passing audit still covers its children — the Step-2
  // readiness gate is authoritative for top-level items).
  if (audit.readyToClose === true) {
    return {
      isBlocking: false,
      transient: false,
      stale: isAuditStale(workItem, auditData),
      passing: true,
      reason: 'Ready to close',
      summary: audit.summary || null,
    };
  }

  // readyToClose is false or missing. Distinguish a genuine 'not ready to
  // close' verdict from a timeout/transient failure: a timed-out or failed
  // audit pipeline is not a real verdict and must not hard-block the release.
  if (isTimeoutOrTransientAudit(audit)) {
    return {
      isBlocking: false,
      transient: true,
      stale: false,
      passing: false,
      reason:
        'Audit timed out or hit a transient failure — not blocking (re-audit recommended)',
      summary: audit.summary || null,
    };
  }

  // A failing verdict that predates the work item's last update is stale: the
  // verdict is not trustworthy (the item may have changed since), so it is
  // treated as remediable rather than an immediate block. This aligns the
  // Step-2 audit gate with the Step-3.7 final-validation sweep
  // (SA-0MUOO5UEB008RXBP).
  if (isAuditStale(workItem, auditData)) {
    return {
      isBlocking: false,
      transient: false,
      stale: true,
      passing: false,
      reason:
        'Audit is stale (performed before the work item was last updated) — not blocking (re-audit recommended)',
      summary: audit.summary || null,
    };
  }

  // readyToClose is false or missing → genuine blocking failure.
  return {
    isBlocking: true,
    transient: false,
    stale: false,
    passing: false,
    reason: 'Audit verdict: not ready to close',
    summary: audit.summary || null,
  };
}

// ── buildRemediationCommand ──────────────────────────────────────────────────

/**
 * Build an actionable remediation command string for a blocking item.
 *
 * @param {string} workItemId - The ID of the blocking work item.
 * @returns {string} A shell command to re-run the audit.
 */
export function buildRemediationCommand(workItemId) {
  return [
    `  # Re-run audit for ${workItemId}:`,
    `  wl audit-show ${workItemId} --json`,
    `  python3 skill/audit/scripts/audit_runner.py issue ${workItemId}`,
  ].join('\n');
}

// ── resolveAuditRunner ───────────────────────────────────────────────────────

/**
 * Resolve the path to the audit runner script.
 *
 * Prefers the in-repo copy (`<repo>/skill/audit/scripts/audit_runner.py`)
 * so the release process uses the version pinned with the repository;
 * falls back to the globally installed skill
 * (`~/.pi/agent/skills/audit/scripts/audit_runner.py`) when the in-repo
 * copy is absent (e.g. the ship skill itself is run from its global
 * install). If neither exists the in-repo path is returned and the
 * invocation fails loudly — never silently.
 *
 * @param {object} [fsMod] - Injectable fs-like module exposing
 *   `existsSync` (defaults to `node:fs`). Used by unit tests to simulate
 *   in-repo/global availability.
 * @returns {string} The resolved audit runner script path.
 */
export function resolveAuditRunner(fsMod = { existsSync }) {
  const inRepo = join(REPO_ROOT, 'skill', 'audit', 'scripts', 'audit_runner.py');
  const globalPath = join(HOME_DIR, '.pi', 'agent', 'skills', 'audit', 'scripts', 'audit_runner.py');
  if (fsMod.existsSync(inRepo)) {
    return inRepo;
  }
  if (fsMod.existsSync(globalPath)) {
    return globalPath;
  }
  return inRepo;
}

// ── queryContentFreshness ────────────────────────────────────────────────────

/**
 * Ask the audit runner (read-only) whether a work item's stored audit is still
 * fresh by content fingerprint (SA-0MUOO5W8J001DYTI).
 *
 * Delegates to `audit_runner.py check-freshness <id> --json`, which reuses the
 * runner's own `_check_audit_freshness` (content fingerprint first, then the
 * time gate) without running an audit or mutating the worklog. This lets the
 * ship gates treat a time-stale-but-content-unchanged audit as trustworthy
 * instead of re-auditing it, while a content change still triggers a re-audit.
 *
 * Fails open: any error (runner missing, command failure, bad JSON) returns
 * `{ fresh: false }` so the caller falls back to the conservative time gate —
 * never falsely passing a stale audit.
 *
 * @param {string} workItemId - Work item id.
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {(cmd: string) => string} [options.runCommand] - Command runner.
 * @param {() => string} [options.resolveAuditRunnerFn] - Runner path resolver.
 * @returns {{ fresh: boolean, reason: string, hasFingerprint: boolean, auditedAt: string|null }}
 */
export function queryContentFreshness(workItemId, options = {}) {
  const {
    runCommand = (cmd) => execSync(cmd, { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }),
    resolveAuditRunnerFn = resolveAuditRunner,
  } = options;
  try {
    const runner = resolveAuditRunnerFn();
    const output = runCommand(`python3 "${runner}" check-freshness ${workItemId} --json`);
    const parsed = JSON.parse(output);
    return {
      fresh: !!(parsed && parsed.fresh === true),
      reason: (parsed && parsed.reason) || '',
      hasFingerprint: !!(parsed && parsed.hasFingerprint),
      auditedAt: (parsed && parsed.auditedAt) || null,
    };
  } catch (err) {
    return {
      fresh: false,
      reason: `check-freshness failed: ${err.stderr?.toString()?.trim() || err.message}`,
      hasFingerprint: false,
      auditedAt: null,
    };
  }
}

// ── hasContentFingerprint ────────────────────────────────────────────────────

/**
 * Whether a stored audit's report carries a **v2 content fingerprint**.
 *
 * The audit runner embeds a versioned metadata line
 * ``Audit content fingerprint (v2): <sha256-hex>`` in the persisted report
 * when the item-data-only fingerprint feature captured one (SA-0MVIDI8E00063OOP).
 * Only v2-bearing audits can be resolved by content (the `check-freshness`
 * probe); legacy unversioned reports (``Audit content fingerprint: <hex>``)
 * and fingerprint-less reports fall back to the local time gate. A legacy
 * line is deliberately NOT matched here — its fingerprint cannot be recomputed,
 * so spawning the probe would only reproduce the time gate — and the v2
 * prefix is not a substring of the legacy form, so the detection is
 * version-aware by construction. Detecting it here keeps the gates hermetic —
 * no runner invocation for legacy/fingerprint-less audits — and implements
 * the AC3 fallback (SA-0MVIDICGZ0030DXC).
 *
 * @param {object|null} auditData - Parsed `wl audit-show <id> --json` payload.
 * @returns {boolean}
 */
export function hasContentFingerprint(auditData) {
  const audit = auditData && auditData.audit;
  if (!audit) {
    return false;
  }
  const haystack = [audit.rawOutput, audit.summary]
    .filter((v) => typeof v === 'string')
    .join('\n');
  return haystack.includes(AUDIT_CONTENT_FINGERPRINT_PREFIX);
}

// ── buildProducerReviewRemediationCommand ────────────────────────────────────

/**
 * Build an actionable remediation command string for a work item that is
 * blocked by the producer-review gate.
 *
 * Suggests running the audit to clear the flag or clearing it manually
 * with `wl update <id> --needs-producer-review false` (the `wl` CLI flag is
 * kebab-case; the JSON field read back from `wl --json` is
 * `needsProducerReview`).
 *
 * @param {string} workItemId - The ID of the blocking work item.
 * @returns {string} A shell command to resolve the producer-review flag.
 */
export function buildProducerReviewRemediationCommand(workItemId) {
  return [
    `  # Clear the producer-review flag for ${workItemId}:`,
    `  wl update ${workItemId} --needs-producer-review false --json`,
    `  # Or re-run audit to auto-resolve:`,
    `  python3 skill/audit/scripts/audit_runner.py issue ${workItemId}`,
  ].join('\n');
}

// ── checkProducerReviewStatus ────────────────────────────────────────────────

/**
 * Check a list of candidate work items for producer-review blocking.
 *
 * Items with `needsProducerReview = true`, `null`, or `undefined` are
 * considered blocking — they require producer approval before they can
 * be shipped. Items with `needsProducerReview = false` are passing.
 *
 * This is intended as a gating step in the release process, complementing
 * the audit readiness gate. It blocks the release with exit code 9 if any
 * candidate items need producer review.
 *
 * @param {Array<{ id: string, title: string, needsProducerReview: boolean|null }>} items -
 *   Candidate work items to check (typically from `getCandidateItems()`).
 * @returns {{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     needsProducerReview: boolean|null,
 *     reason: string,
 *     remediation: string
 *   }>,
 *   message: string
 * }}
 */
export function checkProducerReviewStatus(items) {
  if (!items || items.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: 'No candidate work items. Producer-review gate passed.',
    };
  }

  const blockingItems = [];

  for (const item of items) {
    // Items with needsProducerReview = true, null, or undefined are blocking
    const needsReview = item.needsProducerReview === true ||
      item.needsProducerReview === null ||
      item.needsProducerReview === undefined;

    if (needsReview) {
      const reason = item.needsProducerReview === true
        ? 'Flagged for producer review'
        : 'Producer-review status unknown (needsProducerReview not set)';

      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        needsProducerReview: item.needsProducerReview !== undefined
          ? item.needsProducerReview
          : null,
        reason,
        remediation: buildProducerReviewRemediationCommand(item.id),
      });
    }
  }

  // Build report
  if (blockingItems.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: `All ${items.length} work item(s) have passed producer review. Producer-review gate passed.`,
    };
  }

  const lines = [
    `⚠️  Producer-review gate check failed — ${blockingItems.length} of ${items.length} work item(s) need producer review:`,
    '',
  ];

  blockingItems.forEach((entry, i) => {
    lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    lines.push(`   Reason: ${entry.reason}`);
    lines.push(`   Remediation:`);
    lines.push(entry.remediation);
    lines.push('');
  });

  lines.push(
    'These items are blocking the release until a producer reviews and approves them.',
    'To bypass this check, re-run with --skip-checks.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    message: lines.join('\n'),
  };
}

// ── stalePassingBlockReason ──────────────────────────────────────────────────

/**
 * Build the non-contradictory block reason for a **passing but time-stale**
 * audit whose content freshness cannot be confirmed (SA-0MVTU22MB0057T2L).
 *
 * A passing-but-time-stale audit must never surface its own verdict
 * (`Ready to close`) as a blocking reason — that reads as "blocks because it
 * passed". Instead the reason names the real condition (the time gate is the
 * only basis left once the content fingerprint could not confirm the audit)
 * and carries the actionable `--force` remedy, which is the only way to
 * restamp an audit the runner considers content-fresh.
 *
 * @param {string} workItemId - Work item id (used in the remedy command).
 * @param {object|null} [freshness] - Result of `queryContentFreshness()` when
 *   a probe actually ran; `null` when no v2 fingerprint was available.
 * @returns {string}
 */
export function stalePassingBlockReason(workItemId, freshness = null) {
  const detail = freshness && freshness.reason ? ` — ${freshness.reason}` : '';
  return `Audit is time-stale but content changed (or could not be confirmed unchanged)${detail}; `
    + `re-audit with: python3 skill/audit/scripts/audit_runner.py issue ${workItemId} --force`;
}

// ── attemptAuditRemediation ──────────────────────────────────────────────────

/**
 * Attempt to self-heal a missing or transient audit by re-running the audit
 * for a single work item.
 *
 * Conservative remediation (SA-0MSUT8GQP004WSYN AC2): re-runs the audit via
 * `audit_runner.py issue <id>` (persist semantics — never `wl update`
 * directly), then re-checks `wl audit-show`. The item is unblocked only if
 * the re-run produces a passing audit; a still-failing item blocks. A runner
 * failure (non-zero exit / thrown exception / timeout) is treated as blocking
 * for that item and never silently passes.
 *
 * **Passing-but-time-stale resolution (SA-0MVTU22MB0057T2L).** The runner
 * short-circuits `issue <id>` for a content-fresh audit, so a remediation run
 * can legitimately leave a `{passing: true, stale: true}` audit whose
 * `auditedAt` was never restamped. Blocking on that state would report a
 * *passing* audit as blocking with the reason `Ready to close`. Instead the
 * post-remediation status is resolved by **content freshness**: with a v2
 * content fingerprint, the read-only probe is re-queried and an unchanged
 * fingerprint resolves the item as `passing` (naming the content-fresh basis);
 * a changed/unconfirmable fingerprint (or a probe failure, which fails open)
 * blocks with {@link stalePassingBlockReason}. Legacy/fingerprint-less audits
 * cannot be compared by content, so they fall back to the conservative time
 * gate and block with the same actionable reason.
 *
 * @param {{ id: string, title: string }} workItem - The work item to remediate.
 * @param {object} boundaries - Injectable command boundaries.
 * @param {(workItemId: string) => string} boundaries.runAuditShow - Runs
 *   `wl audit-show <id> --json` and returns the raw JSON string.
 * @param {(runnerPath: string, workItemId: string) => string} boundaries.runAuditCommand -
 *   Runs the audit runner for the item.
 * @param {() => string} boundaries.resolveAuditRunnerFn - Resolves the audit
 *   runner script path.
 * @param {(workItemId: string) => boolean} [boundaries.checkStuckAuditFn] -
 *   Abort-recovery cleanup; defaults to {@link checkAndRestoreStuckAudit}.
 *   Injectable for hermetic tests.
 * @param {(workItemId: string) => { fresh: boolean, reason: string, hasFingerprint: boolean, auditedAt: string|null }} [boundaries.queryContentFreshnessFn] -
 *   Read-only content-freshness probe; defaults to
 *   {@link queryContentFreshness}. Injectable for hermetic tests.
 * @returns {Promise<{ status: 'passing'|'blocking'|'runner-failed', reason: string, summary: string|null }>}
 */
export async function attemptAuditRemediation(workItem, {
  runAuditShow,
  runAuditCommand,
  resolveAuditRunnerFn,
  checkStuckAuditFn = checkAndRestoreStuckAudit,
  queryContentFreshnessFn = queryContentFreshness,
}) {
  try {
    const runnerPath = resolveAuditRunnerFn();
    runAuditCommand(runnerPath, workItem.id);
    // Abort-recovery (SA-0MTRH8C5O005URGR): if the audit runner was
    // killed (SIGKILL) it cannot run its finally block — restore the
    // work item from the leftover state file.
    checkStuckAuditFn(workItem.id);
  } catch (err) {
    // Abort-recovery (SA-0MTRH8C5O005URGR): a SIGKILLed audit runner
    // surfaces here (execSync throws with status null / signal SIGKILL)
    // and cannot run its own finally block — restore the work item from
    // the leftover state file before classifying the failure.
    checkStuckAuditFn(workItem.id);
    // Classify the failure so an infrastructure problem (timeout/concurrency/
    // provider) is distinguishable from a genuine verdict
    // (SA-0MUOO5V0P00461X8). The "Audit remediation failed" prefix is retained
    // for backward compatibility with existing reports/tests.
    const { category, detail } = classifyRemediationError(err);
    return {
      status: 'runner-failed',
      category,
      reason: `Audit remediation failed (${category}): ${detail}`,
      summary: null,
    };
  }

  // Re-check the audit after the remediation run.
  try {
    const output = runAuditShow(workItem.id);
    const auditData = JSON.parse(output);
    const status = getAuditStatus(workItem, auditData);

    // A passing-but-time-stale audit is resolved by content freshness, never
    // by its own verdict (SA-0MVTU22MB0057T2L). Protecting this branch here
    // means every consumer of the shared remediation path (Step-2 audit gate,
    // Step-3.7 sweep, `refresh-audits.js`) gets the fix without duplicating
    // the logic.
    if (status.passing && status.stale) {
      const freshness = hasContentFingerprint(auditData)
        ? queryContentFreshnessFn(workItem.id)
        : null;
      if (freshness && freshness.fresh) {
        return {
          status: 'passing',
          reason: `Content-fresh audit after remediation (${freshness.reason})`,
          summary: status.summary,
        };
      }
      return {
        status: 'blocking',
        reason: stalePassingBlockReason(workItem.id, freshness),
        // Never surface the passing verdict in a blocking entry.
        summary: null,
      };
    }

    if (status.isBlocking || status.transient || status.stale) {
      return {
        status: 'blocking',
        reason: status.reason,
        summary: status.summary,
      };
    }
    return {
      status: 'passing',
      reason: 'Remediated audit passes',
      summary: status.summary,
    };
  } catch (err) {
    return {
      status: 'blocking',
      reason: `Failed to re-check audit after remediation: ${err.stderr?.toString()?.trim() || err.message}`,
      summary: null,
    };
  }
}

// ── checkAuditReadyToClose ───────────────────────────────────────────────────

/**
 * Check all top-level `in_review` work items for audit readiness.
 *
 * For each top-level candidate item, queries `wl audit-show <id> --json`
 * and checks `audit.readyToClose`. Items whose audit is **missing**,
 * **stale** (performed before the item's last update), or
 * **transient** (timeout/provider-error/FailureNotice) are auto-remediated:
 * the gate re-runs the audit (`audit_runner.py issue <id>`, which persists
 * per its own contract — the gate never calls `wl update` directly), then
 * re-checks `wl audit-show`; the item blocks only if it still fails after
 * the re-run. Genuine (fresh) "not ready to close" verdicts block immediately
 * with no re-audit attempt (conservative remediation, SA-0MSUT8GQP004WSYN AC2;
 * stale handling shared with Step 3.7 per SA-0MUOO5UEB008RXBP). A re-run that
 * leaves a *passing but time-stale* audit is resolved by content freshness in
 * the shared remediation path — never blocked with the reason `Ready to
 * close` (SA-0MVTU22MB0057T2L).
 *
 * Command boundaries are injectable (mirroring the
 * `closeWorkItemsAfterRelease` `runCloseCommand` pattern) so unit tests are
 * hermetic — no live `wl`/`audit_runner` invocations.
 *
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {() => Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null }>} [options.getCandidateItemsFn] -
 *   Candidate-item query; defaults to `getTopLevelCandidateItems()` (top-level
 *   `in_review` items only).
 * @param {(workItemId: string) => string} [options.runAuditShow] - Runs
 *   `wl audit-show <id> --json` and returns the raw JSON string.
 * @param {(runnerPath: string, workItemId: string) => string} [options.runAuditCommand] -
 *   Runs the audit remediation (`python3 <runner> issue <id>`) and returns
 *   its stdout.
 * @param {() => string} [options.resolveAuditRunnerFn] - Resolves the audit
 *   runner script path; defaults to `resolveAuditRunner`.
 * @param {() => import('./audit-remediation.js').RemediationBudget} [options.createRemediationBudgetFn] -
 *   Factory for the bounded remediation budget; defaults to a fresh
 *   `RemediationBudget` (env-configured). Injectable for deterministic tests.
 * @param {(workItemId: string) => { fresh: boolean, reason: string, hasFingerprint: boolean, auditedAt: string|null }} [options.queryContentFreshnessFn] -
 *   Read-only content-freshness probe; defaults to
 *   {@link queryContentFreshness}. Used by the fast path and by the shared
 *   post-remediation resolution (SA-0MVTU22MB0057T2L). Injectable for
 *   hermetic tests.
 * @returns {Promise<{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reason: string,
 *     summary: string|null,
 *     remediation: string
 *   }>,
 *   transientItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reason: string,
 *     summary: string|null,
 *     remediation: string
 *   }>,
 *   remediatedItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reason: string,
 *     summary: string|null
 *   }>,
 *   message: string
 * }>}
 */
export async function checkAuditReadyToClose(options = {}) {
  const {
    getCandidateItemsFn = getTopLevelCandidateItems,
    runAuditShow = (workItemId) => execSync(
      `wl audit-show ${workItemId} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    runAuditCommand = (runnerPath, workItemId) => execSync(
      `python3 "${runnerPath}" issue ${workItemId}`,
      // Per-item timeout guard (SA-0MSUT8GQP004WSYN AC6 / R1): a hung audit
      // runner must not stall the whole release gate. The default audit
      // runner hard-times-out internally, so this is a belt-and-braces cap.
      // Configurable via SHIP_AUDIT_REMEDIATION_TIMEOUT_MS
      // (SA-0MUOO5V0P00461X8).
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'], timeout: resolveRemediationTimeoutMs() },
    ),
    resolveAuditRunnerFn = resolveAuditRunner,
    createRemediationBudgetFn = () => new RemediationBudget(),
    skipRemediation = false,
    queryContentFreshnessFn = queryContentFreshness,
  } = options;

  // Step 1: Collect candidate items — top-level only. Children are covered
  // by their parent's audit, so they must never block the release
  // (SA-0MSUT8GQP004WSYN).
  const items = getCandidateItemsFn();

  if (items.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      transientItems: [],
      remediatedItems: [],
      message: 'No in_review work items found. Audit gate passed.',
    };
  }

  // Step 3: Check audit status for each item
  const blockingItems = [];
  const transientItems = [];
  const remediatedItems = [];
  const budget = createRemediationBudgetFn();

  for (const item of items) {
    let auditData = null;
    try {
      const output = runAuditShow(item.id);
      auditData = JSON.parse(output);
    } catch (err) {
      // If audit-show fails entirely, treat as blocking
      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        reason: `Failed to query audit: ${err.stderr?.toString()?.trim() || err.message}`,
        summary: null,
        remediation: buildRemediationCommand(item.id),
      });
      continue;
    }

    let status = getAuditStatus(item, auditData);

    // Content-fingerprint fast path (SA-0MUOO5W8J001DYTI): a time-stale audit
    // whose stored content fingerprint still matches needs no re-audit. Ask the
    // runner read-only and upgrade the classification accordingly:
    //   - passing + content-fresh → treat as fresh (no remediation);
    //   - failing + content-fresh → the verdict is current, so block immediately.
    // The probe result is kept so a later block (skip/budget) can name the real
    // condition instead of repeating the passing verdict (SA-0MVTU22MB0057T2L).
    let contentFreshness = null;
    if (status.stale && hasContentFingerprint(auditData)) {
      const freshness = queryContentFreshnessFn(item.id);
      contentFreshness = freshness;
      if (freshness.fresh) {
        status = status.passing
          ? { ...status, stale: false, reason: `Content fingerprint unchanged (${freshness.reason})` }
          : {
            isBlocking: true,
            transient: false,
            stale: false,
            passing: false,
            reason: `Audit verdict: not ready to close (content-fresh: ${freshness.reason})`,
            summary: status.summary,
          };
      }
    }

    // Missing audit (no audit record at all), stale (the verdict predates the
    // item's last update), or transient (timed out / provider error /
    // FailureNotice): attempt conservative auto-remediation. A stale failing
    // verdict is not trustworthy, so — matching the Step-3.7 sweep — it is
    // remediated rather than blocking immediately (SA-0MUOO5UEB008RXBP).
    const isMissingAudit = status.isBlocking && status.reason === 'No audit found';
    if (isMissingAudit || status.transient || status.stale) {
      // Narrow bypass (SA-0MUOO5WV0006D7UD): when --skip-audit-remediation is
      // set, do not re-audit in-gate — report the item blocking with the
      // offline hint. The readyToClose requirement is NOT relaxed.
      if (skipRemediation) {
        console.log(
          `Audit gate: skipping in-gate remediation for ${item.id} `
          + '(--skip-audit-remediation) — blocking.',
        );
        // A passing-but-time-stale audit must not block with its own verdict
        // (`Ready to close`) as the reason (SA-0MVTU22MB0057T2L).
        const skipReason = status.passing && status.stale
          ? stalePassingBlockReason(item.id, contentFreshness)
          : status.reason;
        blockingItems.push({
          workItemId: item.id,
          title: item.title,
          reason: `${skipReason} (in-gate remediation skipped)`,
          summary: status.passing && status.stale ? null : (status.summary || null),
          remediation: `${buildRemediationCommand(item.id)}\n  # ${OFFLINE_AUDIT_REFRESH_HINT}`,
        });
        continue;
      }
      // Log the remediation attempt per item (SA-0MSUT8GQP004WSYN AC6) so a
      // slow release gate is attributable.
      const auditKind = isMissingAudit
        ? 'missing audit'
        : status.stale
          ? 'stale audit'
          : 'transient audit';
      console.log(
        `Audit gate: auto-remediating ${item.id} (${auditKind})...`,
      );
      // Bounded remediation (SA-0MUOO5V0P00461X8): once the budget is spent
      // (attempt cap or wall clock), stop re-auditing and report the remaining
      // items with an offline-refresh instruction instead of implying the work
      // is not ready to close.
      if (!budget.canAttempt()) {
        console.log(
          `Audit gate: remediation budget exhausted (${budget.describe()}) — skipping ${item.id}.`,
        );
        blockingItems.push({
          workItemId: item.id,
          title: item.title,
          reason:
            `Audit remediation budget exhausted (${budget.describe()}) — `
            + 'audit needs refreshing offline',
          summary: null,
          remediation: `${buildRemediationCommand(item.id)}\n  # ${OFFLINE_AUDIT_REFRESH_HINT}`,
        });
        continue;
      }
      const remediation = await attemptAuditRemediation(item, {
        runAuditShow,
        runAuditCommand,
        resolveAuditRunnerFn,
        queryContentFreshnessFn,
      });
      budget.record();
      if (remediation.status === 'passing') {
        console.log(`Audit gate: ${item.id} auto-remediated successfully.`);
      } else if (remediation.status === 'runner-failed') {
        console.log(`Audit gate: ${item.id} remediation runner failed — blocking.`);
      } else {
        console.log(`Audit gate: ${item.id} still failing after remediation — blocking.`);
      }

      if (remediation.status === 'passing') {
        // Re-run succeeded: item unblocked.
        remediatedItems.push({
          workItemId: item.id,
          title: item.title,
          reason: remediation.reason,
          summary: remediation.summary,
        });
      } else if (remediation.status === 'runner-failed') {
        // The remediation runner itself failed — never silently pass; block
        // and surface the manual remediation command. `category` records
        // whether the failure was infrastructure (timeout/concurrency/provider)
        // or another error (SA-0MUOO5V0P00461X8).
        blockingItems.push({
          workItemId: item.id,
          title: item.title,
          reason: remediation.reason,
          summary: null,
          category: remediation.category,
          remediation: buildRemediationCommand(item.id),
        });
      } else {
        // Still failing after the re-run → block.
        blockingItems.push({
          workItemId: item.id,
          title: item.title,
          reason: remediation.reason,
          summary: remediation.summary,
          remediation: buildRemediationCommand(item.id),
        });
      }
    } else if (status.isBlocking) {
      // Genuine 'not ready to close' verdict — block immediately, no re-run.
      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        reason: status.reason,
        summary: status.summary,
        remediation: buildRemediationCommand(item.id),
      });
    }
  }

  // Helper: format a transient-items warning block for the report message
  function buildTransientWarning(lines) {
    if (transientItems.length === 0) {
      return;
    }
    lines.push(
      '',
      `Note: ${transientItems.length} work item(s) had timed-out or transient audits ` +
        'and were NOT treated as blocking (re-audit recommended):',
      '',
    );
    transientItems.forEach((entry, i) => {
      lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
      lines.push(`   Reason: ${entry.reason}`);
      lines.push(`   Remediation:`);
      lines.push(entry.remediation);
      lines.push('');
    });
  }

  // Helper: format an auto-remediated-items note for the report message
  function buildRemediatedNote(lines) {
    if (remediatedItems.length === 0) {
      return;
    }
    lines.push(
      '',
      `Note: ${remediatedItems.length} work item(s) had missing or transient audits ` +
        'and were auto-remediated (re-audit succeeded, item unblocked):',
      '',
    );
    remediatedItems.forEach((entry, i) => {
      lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
      lines.push(`   Reason: ${entry.reason}`);
      lines.push('');
    });
  }

  // Step 4: Build report
  if (blockingItems.length === 0) {
    const lines = [
      `All ${items.length} work item(s) have passing audits. Audit gate passed.`,
    ];
    buildRemediatedNote(lines);
    buildTransientWarning(lines);
    return {
      hasBlockingItems: false,
      blockingItems: [],
      transientItems,
      remediatedItems,
      message: lines.join('\n'),
    };
  }

  const lines = [
    `⚠️  Audit gate check failed — ${blockingItems.length} of ${items.length} work item(s) are not ready to close:`,
    '',
  ];

  blockingItems.forEach((entry, i) => {
    lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    lines.push(`   Reason: ${entry.reason}`);
    if (entry.summary) {
      // Truncate long summaries for the report
      const summary = entry.summary.length > 200
        ? entry.summary.substring(0, 200) + '...'
        : entry.summary;
      lines.push(`   Summary: ${summary}`);
    }
    lines.push(`   Remediation:`);
    lines.push(entry.remediation);
    lines.push('');
  });

  buildRemediatedNote(lines);
  buildTransientWarning(lines);

  lines.push(
    'Note: This report is a point-in-time snapshot. After remediation, re-run the release',
    'process without --skip-checks to re-validate. Use --skip-checks to bypass this gate.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    transientItems,
    remediatedItems,
    message: lines.join('\n'),
  };
}
