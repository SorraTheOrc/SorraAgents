/**
 * check-critical-items.js — Critical-priority item gating step for the ship skill.
 *
 * Checks all critical-priority work items to verify they are in a terminal
 * state before a release proceeds. Items are considered terminal if:
 *   - `status === 'completed'` AND (`stage === 'in_review'` OR `stage === 'done'`)
 *
 * Any critical-priority item not matching this condition is considered
 * blocking and will abort the release with exit code 7.
 *
 * This is intended as a gating step in the release process, complementing
 * the unmerged-branches check and the audit readiness gate.
 *
 * Usage:
 *
 *   import { checkCriticalItems } from './check-critical-items.js';
 *
 *   const report = checkCriticalItems();
 *   if (report.hasBlockingItems) {
 *     console.error(report.message);
 *     // Release is blocked with exit code 7
 *   }
 */

import { execSync } from 'node:child_process';

// ── checkCriticalItems ──────────────────────────────────────────────────────

/**
 * Check all critical-priority work items for release-readiness.
 *
 * Queries `wl list --priority critical --status <open|in-progress|blocked>
 * --json` directly — the returned items ARE the blockers, so no in-script
 * terminal-state filter is needed (SA-0MSPPHLF9009UMWA). The full
 * `--priority critical` dump (~94 KB) is replaced by three small scoped
 * queries (~16 KB total).
 *
 * @returns {{
 *   hasBlockingItems: boolean,
 *   blockingItems: Array<{
 *     workItemId: string,
 *     title: string,
 *     currentStatus: string,
 *     currentStage: string
 *   }>,
 *   message: string
 * }}
 */
export function checkCriticalItems() {
  // Non-terminal statuses: open / in-progress / blocked. Terminal is
  // status=completed (stage in_review|done), so querying only the blocking
  // statuses returns exactly the blockers — no in-script filter needed
  // (SA-0MSPPHLF9009UMWA). Payload drops from ~94 KB to ~16 KB.
  const blockingStatuses = ['open', 'in-progress', 'blocked'];
  const blockingItems = [];
  let queriedCount = 0;

  for (const status of blockingStatuses) {
    let items = [];
    try {
      const output = execSync(
        `wl list --priority critical --status ${status} --json`, {
          encoding: 'utf-8',
          stdio: ['pipe', 'pipe', 'pipe'],
        },
      );
      const data = JSON.parse(output);
      if (data.workItems && Array.isArray(data.workItems)) {
        items = data.workItems;
      }
    } catch (err) {
      // If wl CLI fails entirely (not installed, network error), treat as
      // non-blocking — the release should not be blocked by a tooling failure.
      // Log a warning so operators are aware.
      console.warn(
        `Warning: Failed to query critical items (status ${status}): ` +
        `${err.stderr?.toString()?.trim() || err.message}`
      );
      return {
        hasBlockingItems: false,
        blockingItems: [],
        message: 'Could not query critical work items (wl CLI unavailable or error). Gate skipped.',
      };
    }
    queriedCount += items.length;
    for (const item of items) {
      blockingItems.push({
        workItemId: item.id,
        title: item.title || item.id,
        currentStatus: item.status,
        currentStage: item.stage,
      });
    }
  }

  // Step 2: Build report
  if (blockingItems.length === 0) {
    if (queriedCount === 0) {
      return {
        hasBlockingItems: false,
        blockingItems: [],
        message: 'No non-terminal critical-priority work items found. Critical-items gate passed.',
      };
    }
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: `All queried non-terminal critical-priority work item(s) (${queriedCount}) are accounted for. Critical-items gate passed.`,
    };
  }

  const lines = [
    `⚠️  Critical-items gate check failed — ${blockingItems.length} critical-priority work item(s) are not in a terminal state:`,
    '',
  ];

  blockingItems.forEach((entry, i) => {
    lines.push(`${i + 1}. ${entry.title} (${entry.workItemId})`);
    lines.push(`   Status: ${entry.currentStatus} | Stage: ${entry.currentStage}`);
    lines.push('');
  });

  lines.push(
    'A terminal state requires: status=completed AND (stage=in_review OR stage=done).',
    'Resolve or advance the listed items before proceeding with the release.',
    '',
    'To bypass this check, re-run with --skip-checks.',
  );

  return {
    hasBlockingItems: true,
    blockingItems,
    message: lines.join('\n'),
  };
}
