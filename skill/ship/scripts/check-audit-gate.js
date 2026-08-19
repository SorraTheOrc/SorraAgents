/**
 * check-audit-gate.js — Audit readiness and producer-review gating for the ship skill.
 *
 * This module provides gating functions for the release process:
 *   1. Audit readiness — checks all `in_review` work items have `audit.readyToClose`.
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
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Module constants for audit-runner path resolution: the in-repo copy of the
// audit skill is preferred; the globally installed skill is the fallback.
const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..', '..');
const HOME_DIR = homedir();

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
 * uses it to scope the release gates.
 *
 * Invoked via `bash -c` (not plain /bin/sh) because `set -o pipefail`
 * is a bash-ism not supported by dash (Ubuntu/Debian's default sh) —
 * see LP-0MSQ0NTMO00577UJ.
 *
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null }>}
 */
export function getCandidateItems() {
  try {
    // Single query: stage=in_review implies status=completed (the
    // completed-minus-done == in_review invariant), piped through jq so only
    // {id, title, needsProducerReview, parentId} enters the execSync buffer.
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c \"[.workItems[] | {id, title, needsProducerReview, parentId}]\"'`,
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
 * @returns {Array<{ id: string, title: string, needsProducerReview: boolean|null, parentId: string|null }>}
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
 *   reason: string,
 *   summary: string|null
 * }}
 */
export function getAuditStatus(workItem, auditData) {
  // No audit data or audit is null → blocking
  if (!auditData || auditData.audit === null || auditData.audit === undefined) {
    return {
      isBlocking: true,
      transient: false,
      reason: 'No audit found',
      summary: null,
    };
  }

  const audit = auditData.audit;

  // Check readyToClose
  if (audit.readyToClose === true) {
    return {
      isBlocking: false,
      transient: false,
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
      reason:
        'Audit timed out or hit a transient failure — not blocking (re-audit recommended)',
      summary: audit.summary || null,
    };
  }

  // readyToClose is false or missing → blocking
  return {
    isBlocking: true,
    transient: false,
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

// ── buildProducerReviewRemediationCommand ────────────────────────────────────

/**
 * Build an actionable remediation command string for a work item that is
 * blocked by the producer-review gate.
 *
 * Suggests running the audit to clear the flag or manually setting
 * `needsProducerReview = false` via `wl update`.
 *
 * @param {string} workItemId - The ID of the blocking work item.
 * @returns {string} A shell command to resolve the producer-review flag.
 */
export function buildProducerReviewRemediationCommand(workItemId) {
  return [
    `  # Clear the producer-review flag for ${workItemId}:`,
    `  wl update ${workItemId} --needsProducerReview false --json`,
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

// ── attemptAuditRemediation ──────────────────────────────────────────────────

/**
 * Attempt to self-heal a missing or transient audit by re-running the audit
 * for a single work item.
 *
 * Conservative remediation (SA-0MSUT8GQP004WSYN AC2): re-runs the audit via
 * `audit_runner.py issue <id>` (persist semantics — never `wl update`
 * directly), then re-checks `wl audit-show`. The item is unblocked only if
 * the re-run produces a passing audit; a still-failing item blocks. A runner
 * failure (non-zero exit / thrown exception) is treated as blocking for that
 * item and never silently passes.
 *
 * @param {{ id: string, title: string }} workItem - The work item to remediate.
 * @param {object} boundaries - Injectable command boundaries.
 * @param {(workItemId: string) => string} boundaries.runAuditShow - Runs
 *   `wl audit-show <id> --json` and returns the raw JSON string.
 * @param {(runnerPath: string, workItemId: string) => string} boundaries.runAuditCommand -
 *   Runs the audit runner for the item.
 * @param {() => string} boundaries.resolveAuditRunnerFn - Resolves the audit
 *   runner script path.
 * @returns {Promise<{ status: 'passing'|'blocking'|'runner-failed', reason: string, summary: string|null }>}
 */
async function attemptAuditRemediation(workItem, { runAuditShow, runAuditCommand, resolveAuditRunnerFn }) {
  try {
    const runnerPath = resolveAuditRunnerFn();
    runAuditCommand(runnerPath, workItem.id);
  } catch (err) {
    return {
      status: 'runner-failed',
      reason: `Audit remediation failed: ${err.stderr?.toString()?.trim() || err.message}`,
      summary: null,
    };
  }

  // Re-check the audit after the remediation run.
  try {
    const output = runAuditShow(workItem.id);
    const auditData = JSON.parse(output);
    const status = getAuditStatus(workItem, auditData);
    if (status.isBlocking || status.transient) {
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
 * and checks `audit.readyToClose`. Items whose audit is **missing** or
 * **transient** (timeout/provider-error/FailureNotice) are auto-remediated:
 * the gate re-runs the audit (`audit_runner.py issue <id>`, which persists
 * per its own contract — the gate never calls `wl update` directly), then
 * re-checks `wl audit-show`; the item blocks only if it still fails after
 * the re-run. Genuine "not ready to close" verdicts block immediately with
 * no re-audit attempt (conservative remediation, SA-0MSUT8GQP004WSYN AC2).
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
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
    resolveAuditRunnerFn = resolveAuditRunner,
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

    const status = getAuditStatus(item, auditData);

    // Missing audit (no audit record at all) or transient (timed out /
    // provider error / FailureNotice): attempt conservative auto-remediation.
    const isMissingAudit = status.isBlocking && status.reason === 'No audit found';
    if (isMissingAudit || status.transient) {
      const remediation = await attemptAuditRemediation(item, {
        runAuditShow,
        runAuditCommand,
        resolveAuditRunnerFn,
      });

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
        // and surface the manual remediation command.
        blockingItems.push({
          workItemId: item.id,
          title: item.title,
          reason: remediation.reason,
          summary: null,
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
