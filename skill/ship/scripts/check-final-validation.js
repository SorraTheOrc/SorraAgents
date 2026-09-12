/**
 * check-final-validation.js — Final validation sweep for the ship skill.
 *
 * The release process runs several scoped gates before promoting `dev` to
 * `main`:
 *
 *   1. Unmerged branches (exit 3)
 *   2. Audit readiness (exit 6) — **top-level** `in_review` items only
 *   3. Critical items (exit 7)
 *   4. Worklog refs (exit 8)
 *   5. Producer review (exit 9) — **top-level** `in_review` items only
 *
 * None of those gates perform a comprehensive sweep of *all* `in_review`
 * items (both top-level and child items) for audit health. This module adds
 * that final sweep (SA-0MTMSPKEX003JGIX, Step 3.7): it queries **every**
 * `in_review` item — with no `parentId` filter — and blocks the release
 * (exit code 12) when any item is found with:
 *
 *   a. a missing audit (no audit record exists),
 *   b. a stale audit (audit exists but predates the item's last update),
 *   c. a failing audit (a fresh "not ready to close" verdict), or
 *   d. a producer-review flag (`needsProducerReview === true`).
 *
 * Missing, stale, and transient audits are auto-remediated conservatively by
 * re-running the audit runner (`audit_runner.py issue <id>`, which persists
 * per its own contract — this gate never calls `wl update` directly) and
 * re-checking `wl audit-show`. Genuine "not ready to close" verdicts block
 * immediately with no re-audit attempt. Producer-review flags are reported
 * with the command needed to clear them.
 *
 * Command boundaries (`getItemsFn`, `runAuditShow`, `runAuditCommand`,
 * `resolveAuditRunnerFn`) are injectable so unit tests are hermetic — no
 * live `wl`/`audit_runner` invocations.
 */

import { execSync } from 'node:child_process';
import {
  getAuditStatus,
  buildRemediationCommand,
  buildProducerReviewRemediationCommand,
  resolveAuditRunner,
  attemptAuditRemediation,
} from './check-audit-gate.js';

// Freshness constants mirrored from the audit runner
// (skill/audit/scripts/audit_runner.py) so the staleness heuristic matches
// the runner's own freshness gate. Kept in sync deliberately: the runner is
// the source of truth for freshness; this gate only needs a conservative
// signal that a re-audit is worthwhile (a false "stale" merely triggers the
// runner's fast, content-fingerprint freshness path).
export const AUDIT_FRESHNESS_BUFFER_SECONDS = 60;
export const AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS = 30;

// ── parseIsoUtc ──────────────────────────────────────────────────────────────

/**
 * Parse an ISO-8601 timestamp into a `Date`, treating a missing timezone as
 * UTC. Returns `null` for missing/unparseable values (fail open).
 *
 * @param {string|undefined|null} value - ISO-8601 timestamp.
 * @returns {Date|null}
 */
export function parseIsoUtc(value) {
  if (!value || typeof value !== 'string') {
    return null;
  }
  // Normalise the trailing 'Z' for older runtimes while keeping full
  // ISO-8601 support.
  const normalised = value.replace(/Z$/, '+00:00');
  const withZone = /[+-]\d{2}:?\d{2}$/.test(normalised) ? normalised : `${normalised}+00:00`;
  const parsed = new Date(withZone);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

// ── isAuditStale ─────────────────────────────────────────────────────────────

/**
 * Determine whether an existing audit is stale relative to its work item.
 *
 * Mirrors the audit runner's time-gate floor
 * (`_check_audit_freshness`, skill/audit/scripts/audit_runner.py): an audit
 * is fresh when its `auditedAt` is strictly more than
 * `AUDIT_FRESHNESS_BUFFER_SECONDS` after the item's `updatedAt`, or when the
 * item was updated within `AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS` **after**
 * the audit (the audit's own persistence writes bump `updatedAt`).
 * Everything else is stale.
 *
 * The runner's *primary* freshness signal is a content fingerprint (git
 * HEAD + description hash + Key Files + working tree). This gate cannot
 * recompute that fingerprint cheaply, so it uses the time gate as a
 * conservative signal: a false "stale" only causes a re-run, and the runner
 * short-circuits re-runs of unchanged content via its fingerprint fast path.
 *
 * @param {{ id: string, title: string, updatedAt?: string|null }} workItem -
 *   The work item (must carry `updatedAt` when available).
 * @param {object|null} auditData - Parsed `wl audit-show <id> --json` payload.
 * @returns {boolean} True when the audit exists but is stale; false when no
 *   audit exists, when freshness cannot be determined, or when the audit is
 *   fresh.
 */
export function isAuditStale(workItem, auditData) {
  if (!auditData || !auditData.audit) {
    return false;
  }
  const auditTime = parseIsoUtc(auditData.audit.auditedAt);
  const updateTime = parseIsoUtc(workItem && workItem.updatedAt);
  if (!auditTime || !updateTime) {
    return false; // Cannot determine — fail open, never falsely block.
  }

  const bufferMs = AUDIT_FRESHNESS_BUFFER_SECONDS * 1000;
  if (auditTime.getTime() > updateTime.getTime() + bufferMs) {
    return false; // Audit is newer than the item — fresh.
  }

  const writeDeltaMs = updateTime.getTime() - auditTime.getTime();
  if (writeDeltaMs >= 0 && writeDeltaMs <= AUDIT_PERSIST_WRITE_TOLERANCE_SECONDS * 1000) {
    return false; // The item's own audit-persistence write — fresh.
  }

  return true;
}

// ── classifyAudit ────────────────────────────────────────────────────────────

/**
 * Classify a work item's audit into one of five kinds.
 *
 * Precedence (first match wins):
 *   1. `missing`   — no audit record exists.
 *   2. `transient` — audit timed out / provider error / FailureNotice.
 *   3. `stale`     — audit exists but is outdated (see {@link isAuditStale}).
 *   4. `failing`   — a fresh audit with a genuine "not ready to close" verdict.
 *   5. `passing`   — a fresh audit that is ready to close.
 *
 * Staleness takes precedence over the verdict: a stale verdict is not
 * trustworthy, so a stale "not ready to close" is remediated (and re-blocked
 * only if it still fails) rather than blocked immediately.
 *
 * @param {{ id: string, title: string, updatedAt?: string|null }} workItem -
 *   The work item being checked.
 * @param {object|null} auditData - Parsed `wl audit-show <id> --json` payload.
 * @returns {{ kind: 'missing'|'transient'|'stale'|'failing'|'passing', reason: string, summary: string|null }}
 */
export function classifyAudit(workItem, auditData) {
  const status = getAuditStatus(workItem, auditData);

  if (status.isBlocking && status.reason === 'No audit found') {
    return { kind: 'missing', reason: status.reason, summary: status.summary || null };
  }
  if (status.transient) {
    return { kind: 'transient', reason: status.reason, summary: status.summary || null };
  }
  if (isAuditStale(workItem, auditData)) {
    return {
      kind: 'stale',
      reason: 'Audit is stale (performed before the work item was last updated)',
      summary: status.summary || null,
    };
  }
  if (status.isBlocking) {
    return { kind: 'failing', reason: status.reason, summary: status.summary || null };
  }
  return { kind: 'passing', reason: status.reason, summary: status.summary || null };
}

// ── getInReviewItems ─────────────────────────────────────────────────────────

/**
 * Query Worklog for **all** `in_review` work items (top-level and children).
 *
 * Unlike `getTopLevelCandidateItems()` (which scopes the existing release
 * gates to `parentId == null`), this deliberately applies no `parentId`
 * filter: the final validation sweep must catch audit gaps on child items
 * too (SA-0MTMSPKEX003JGIX AC1).
 *
 * The query is piped through `jq` so only the projected fields cross into
 * Node's buffer (mirroring `getCandidateItems()`), and runs under
 * `bash -c` for `set -o pipefail` support. `updatedAt` is projected so
 * {@link isAuditStale} can evaluate freshness without an extra `wl show`.
 *
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null, updatedAt: string|null }>}
 */
export function getInReviewItems() {
  try {
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c "[.workItems[] | {id, title, needsProducerReview, parentId, updatedAt}]"'`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    );
    const projected = JSON.parse(output);
    if (!Array.isArray(projected)) {
      return [];
    }
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
    console.error(
      `Warning: Failed to query in_review items for final validation: ${err.message}`,
    );
    return [];
  }
}

// ── checkFinalValidation ─────────────────────────────────────────────────────

/**
 * Run the final validation sweep over **all** `in_review` work items.
 *
 * For each item:
 *   - `needsProducerReview === true` → blocking (AC4) with remediation.
 *   - audit `missing` / `stale` / `transient` → auto-remediate via the audit
 *     runner, then re-check; pass → unblocked, still failing → blocking
 *     (AC2).
 *   - audit `failing` (fresh, not transient) → blocking immediately, no
 *     re-audit attempt (AC3).
 *
 * Returns a report whose `message` lists every blocking item with its reason
 * and an actionable remediation command (AC5). The caller
 * (`run-release.js` Step 3.7) maps `hasBlockingItems` to exit code 12.
 *
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {() => Array<object>} [options.getItemsFn] - All-`in_review`-item
 *   query; defaults to {@link getInReviewItems}.
 * @param {(workItemId: string) => string} [options.runAuditShow] - Runs
 *   `wl audit-show <id> --json` and returns the raw JSON string.
 * @param {(runnerPath: string, workItemId: string) => string} [options.runAuditCommand] -
 *   Runs the audit remediation (`python3 <runner> issue <id>`).
 * @param {() => string} [options.resolveAuditRunnerFn] - Resolves the audit
 *   runner script path; defaults to `resolveAuditRunner`.
 * @returns {Promise<{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reasons: string[],
 *     reason: string,
 *     summary: string|null,
 *     remediation: string
 *   }>,
 *   remediatedItems: Array<{ workItemId: string, title: string, reason: string }>,
 *   passingCount: number,
 *   message: string
 * }>}
 */
export async function checkFinalValidation(options = {}) {
  const {
    getItemsFn = getInReviewItems,
    runAuditShow = (workItemId) => execSync(
      `wl audit-show ${workItemId} --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    runAuditCommand = (runnerPath, workItemId) => execSync(
      `python3 "${runnerPath}" issue ${workItemId}`,
      // Per-item timeout guard, consistent with the audit gate's 600s cap.
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 600000 },
    ),
    resolveAuditRunnerFn = resolveAuditRunner,
  } = options;

  const items = getItemsFn();

  if (items.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      remediatedItems: [],
      passingCount: 0,
      message: 'No in_review work items found. Final-validation gate passed.',
    };
  }

  const blockingItems = [];
  const remediatedItems = [];
  let passingCount = 0;

  for (const item of items) {
    const reasons = [];
    let summary = null;
    let remediated = false;
    let producerReview = false;
    let auditIssue = false;

    // ── Producer-review flag (AC1d, AC4) ───────────────────────────────
    if (item.needsProducerReview === true) {
      producerReview = true;
      reasons.push('Flagged for producer review');
    }

    // ── Audit evaluation (AC1a–c, AC2, AC3) ────────────────────────────
    let auditData = null;
    let queryFailed = false;
    try {
      auditData = JSON.parse(runAuditShow(item.id));
    } catch (err) {
      queryFailed = true;
      auditIssue = true;
      reasons.push(
        `Failed to query audit: ${err.stderr?.toString()?.trim() || err.message}`,
      );
    }

    if (!queryFailed) {
      const classification = classifyAudit(item, auditData);
      summary = classification.summary;

      const needsRemediation = classification.kind === 'missing'
        || classification.kind === 'stale'
        || classification.kind === 'transient';

      if (needsRemediation) {
        console.log(
          `Final-validation gate: auto-remediating ${item.id} ` +
          `(${classification.kind} audit)...`,
        );
        const remediation = await attemptAuditRemediation(item, {
          runAuditShow,
          runAuditCommand,
          resolveAuditRunnerFn,
        });
        if (remediation.status === 'passing') {
          console.log(`Final-validation gate: ${item.id} auto-remediated successfully.`);
          remediated = true;
          summary = remediation.summary || summary;
        } else {
          console.log(`Final-validation gate: ${item.id} still failing after remediation — blocking.`);
          auditIssue = true;
          reasons.push(remediation.reason);
          summary = remediation.summary || summary;
        }
      } else if (classification.kind === 'failing') {
        auditIssue = true;
        reasons.push(classification.reason);
      }
    }

    if (reasons.length > 0) {
      const remediationParts = [];
      if (producerReview) {
        remediationParts.push(buildProducerReviewRemediationCommand(item.id));
      }
      if (auditIssue) {
        remediationParts.push(buildRemediationCommand(item.id));
      }
      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        reasons,
        reason: reasons.join('; '),
        summary,
        remediation: remediationParts.join('\n'),
      });
    } else if (remediated) {
      remediatedItems.push({
        workItemId: item.id,
        title: item.title,
        reason: 'Audit auto-remediated (re-run passed)',
      });
    } else {
      passingCount++;
    }
  }

  // ── Build report ───────────────────────────────────────────────────────
  const buildRemediatedNote = (lines) => {
    if (remediatedItems.length === 0) {
      return;
    }
    lines.push(
      '',
      `Note: ${remediatedItems.length} work item(s) had missing/stale/transient audits ` +
        'and were auto-remediated (re-audit succeeded, item unblocked):',
      '',
    );
    remediatedItems.forEach((entry, i) => {
      lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    });
  };

  if (blockingItems.length === 0) {
    const lines = [
      `All ${items.length} in_review work item(s) passed final validation ` +
        `(${passingCount} audit-clean, ${remediatedItems.length} auto-remediated). ` +
        'Final-validation gate passed.',
    ];
    buildRemediatedNote(lines);
    return {
      hasBlockingItems: false,
      blockingItems: [],
      remediatedItems,
      passingCount,
      message: lines.join('\n'),
    };
  }

  const lines = [
    `⚠️  Final-validation gate check failed — ${blockingItems.length} of ${items.length} ` +
      'in_review work item(s) have unresolved audit or producer-review issues:',
    '',
  ];

  blockingItems.forEach((entry, i) => {
    lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    entry.reasons.forEach((reason) => lines.push(`   Reason: ${reason}`));
    if (entry.summary) {
      const summary = entry.summary.length > 200
        ? entry.summary.substring(0, 200) + '...'
        : entry.summary;
      lines.push(`   Summary: ${summary}`);
    }
    lines.push('   Remediation:');
    lines.push(entry.remediation);
    lines.push('');
  });

  buildRemediatedNote(lines);

  lines.push(
    'This sweep covers ALL in_review items (top-level and children), unlike the',
    'scoped audit (Step 2) and producer-review (Step 3.6) gates. After remediation,',
    're-run the release without --skip-checks to re-validate. Use --skip-checks to',
    'bypass this gate.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    remediatedItems,
    passingCount,
    message: lines.join('\n'),
  };
}
