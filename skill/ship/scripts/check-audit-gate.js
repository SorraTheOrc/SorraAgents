/**
 * check-audit-gate.js — Audit readiness gating step for the ship skill.
 *
 * Checks all `in_review` work items for their audit readiness. For each
 * item, the gate calls `wl audit-show <id> --json` and checks
 * `audit.readyToClose`. Items with no audit or with `readyToClose: false`
 * are flagged as blocking.
 *
 * This is intended as a gating step in the release process: before a
 * dev → main merge, agents should call `checkAuditReadyToClose()` to
 * determine whether all candidate work items have passed their audits.
 *
 * Usage:
 *
 *   import { checkAuditReadyToClose } from './check-audit-gate.js';
 *
 *   const report = await checkAuditReadyToClose();
 *   if (report.hasBlockingItems) {
 *     console.log(report.message);
 *     // Release is blocked with exit code 6
 *   }
 */

import { execSync } from 'node:child_process';

// ── getCandidateItems ────────────────────────────────────────────────────────

/**
 * Query Worklog for candidate work items.
 *
 * Fetches items with `stage: in_review` OR `status: completed`, deduplicating
 * by ID. Items in `status: completed` with `stage: done` are already released
 * and are excluded from the candidate set.
 *
 * @returns {Array<{ id: string, title: string }>}
 */
export function getCandidateItems() {
  const seen = new Set();
  const items = [];

  // Query 1: items in in_review stage (main case — about to be released)
  try {
    const output = execSync('wl list --stage in_review --json', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const data = JSON.parse(output);
    if (data.workItems && Array.isArray(data.workItems)) {
      for (const item of data.workItems) {
        if (!seen.has(item.id)) {
          seen.add(item.id);
          items.push({ id: item.id, title: item.title || item.id });
        }
      }
    }
  } catch (err) {
    console.error(`Warning: Failed to query in_review items: ${err.message}`);
  }

  // Query 2: items with status completed (catches edge cases where an item
  // is completed but its stage is not yet in_review). Items already in
  // stage: done are excluded since they have already been released.
  // Use a large maxBuffer to handle potentially large output sets.
  try {
    const output = execSync('wl list --status completed --json', {
      encoding: 'utf-8',
      maxBuffer: 5 * 1024 * 1024, // 5 MB
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const data = JSON.parse(output);
    if (data.workItems && Array.isArray(data.workItems)) {
      for (const item of data.workItems) {
        if (!seen.has(item.id) && item.stage !== 'done') {
          seen.add(item.id);
          items.push({ id: item.id, title: item.title || item.id });
        }
      }
    }
  } catch (err) {
    console.error(`Warning: Failed to query completed items: ${err.message}`);
  }

  return items;
}

// ── getAuditStatus ───────────────────────────────────────────────────────────

/**
 * Check the audit status for a single work item.
 *
 * Determines whether the item's audit is blocking (not ready to close)
 * or passing (ready to close).
 *
 * @param {{ id: string, title: string }} workItem - The work item to check.
 * @param {object|null} auditData - The parsed audit data (or null if no audit).
 * @param {object|null} [auditData.audit] - The audit object from wl audit-show.
 * @returns {{
 *   isBlocking: boolean,
 *   reason: string,
 *   summary: string|null
 * }}
 */
export function getAuditStatus(workItem, auditData) {
  // No audit data or audit is null → blocking
  if (!auditData || auditData.audit === null || auditData.audit === undefined) {
    return {
      isBlocking: true,
      reason: 'No audit found',
      summary: null,
    };
  }

  const audit = auditData.audit;

  // Check readyToClose
  if (audit.readyToClose === true) {
    return {
      isBlocking: false,
      reason: 'Ready to close',
      summary: audit.summary || null,
    };
  }

  // readyToClose is false or missing → blocking
  return {
    isBlocking: true,
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

// ── checkAuditReadyToClose ───────────────────────────────────────────────────

/**
 * Check all `in_review` and `completed` work items for audit readiness.
 *
 * For each candidate item, queries `wl audit-show <id> --json` and checks
 * `audit.readyToClose`. Collects any items that are blocking (no audit,
 * or audit verdict is not ready to close) and returns a structured report.
 *
 * @returns {Promise<{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     reason: string,
 *     summary: string|null,
 *     remediation: string
 *   }>,
 *   message: string
 * }>}
 */
export async function checkAuditReadyToClose() {
  // Step 1: Collect candidate items
  const items = getCandidateItems();

  if (items.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: 'No in_review work items found. Audit gate passed.',
    };
  }

  // Step 3: Check audit status for each item
  const blockingItems = [];

  for (const item of items) {
    let auditData = null;
    try {
      const output = execSync(`wl audit-show ${item.id} --json`, {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      });
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

    if (status.isBlocking) {
      blockingItems.push({
        workItemId: item.id,
        title: item.title,
        reason: status.reason,
        summary: status.summary,
        remediation: buildRemediationCommand(item.id),
      });
    }
  }

  // Step 4: Build report
  if (blockingItems.length === 0) {
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: `All ${items.length} work item(s) have passing audits. Audit gate passed.`,
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

  lines.push(
    'Note: This report is a point-in-time snapshot. After remediation, re-run the release',
    'process without --skip-checks to re-validate. Use --skip-checks to bypass this gate.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    message: lines.join('\n'),
  };
}
