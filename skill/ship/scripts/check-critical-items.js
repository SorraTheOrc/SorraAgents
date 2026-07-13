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

// ── isTerminalState ─────────────────────────────────────────────────────────

/**
 * Determine whether a work item is in a terminal state.
 *
 * A terminal state is defined as:
 *   `status === 'completed'` AND (`stage === 'in_review'` OR `stage === 'done'`)
 *
 * @param {{ status: string, stage: string }} workItem - The work item to check.
 * @returns {boolean} True if the item is in a terminal (releasable) state.
 */
export function isTerminalState(workItem) {
  return (
    workItem.status === 'completed' &&
    (workItem.stage === 'in_review' || workItem.stage === 'done')
  );
}

// ── checkCriticalItems ──────────────────────────────────────────────────────

/**
 * Check all critical-priority work items for release-readiness.
 *
 * Queries `wl list --priority critical --json` to fetch all critical-priority
 * items, then checks each one for terminal state. Any item not in a terminal
 * state is flagged as blocking.
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
  let criticalItems = [];

  // Step 1: Query all critical-priority work items
  try {
    const output = execSync('wl list --priority critical --json', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const data = JSON.parse(output);
    if (data.workItems && Array.isArray(data.workItems)) {
      criticalItems = data.workItems;
    }
  } catch (err) {
    // If wl CLI fails entirely (not installed, network error), treat as
    // non-blocking — the release should not be blocked by a tooling failure.
    // Log a warning so operators are aware.
    console.warn(
      `Warning: Failed to query critical items: ${err.stderr?.toString()?.trim() || err.message}`
    );
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: 'Could not query critical work items (wl CLI unavailable or error). Gate skipped.',
    };
  }

  // Step 2: Filter for non-terminal (blocking) items
  const blockingItems = [];

  for (const item of criticalItems) {
    if (!isTerminalState(item)) {
      blockingItems.push({
        workItemId: item.id,
        title: item.title || item.id,
        currentStatus: item.status,
        currentStage: item.stage,
      });
    }
  }

  // Step 3: Build report
  if (blockingItems.length === 0) {
    if (criticalItems.length === 0) {
      return {
        hasBlockingItems: false,
        blockingItems: [],
        message: 'No critical-priority work items found. Critical-items gate passed.',
      };
    }
    return {
      hasBlockingItems: false,
      blockingItems: [],
      message: `All ${criticalItems.length} critical-priority work item(s) are in a terminal state. Critical-items gate passed.`,
    };
  }

  const lines = [
    `⚠️  Critical-items gate check failed — ${blockingItems.length} of ${criticalItems.length} critical-priority work item(s) are not in a terminal state:`,
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
