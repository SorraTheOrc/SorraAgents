/**
 * Unit tests for skill/ship/scripts/check-critical-items.js
 *
 * Tests the critical-priority item gating function that checks whether
 * any critical-priority work items are not yet in a terminal state
 * (status=completed AND (stage=in_review OR stage=done)).
 *
 * Scenarios:
 *  1. No blocking critical items (all critical items are completed/in_review or done)
 *  2. One or more blocking critical items (blocks with report)
 *  3. No critical items at all (pass silently)
 *  4. --skip-checks bypass correctly skips the gate (tested via run-release.js)
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { spawnSync, execSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');

// ── Helper: Create a temporary test environment ───────────────────────────

/**
 * Create a temporary directory with a mocked `wl` CLI and the
 * check-critical-items.js module, then run the module with
 * the given wl output.
 *
 * @param {object} opts
 * @param {Array} [opts.wlCriticalItems] - Array of work items to return from
 *   `wl list --priority critical --json`. If null/undefined, wl will be
 *   unavailable (simulating CLI failure).
 * @param {string} [opts.importPath] - Path to override the module import
 *   (for testing before the real file exists).
 * @returns {object} spawnSync result
 */
function runCheckCriticalItems(opts = {}) {
  const {
    wlCriticalItems, // array of work items, or null to simulate wl not found
  } = opts;

  const tmpDir = mkdtempSync(join(tmpdir(), 'check-critical-items-test-'));

  // Copy the check-critical-items.js module into the tmp dir
  const testModulePath = join(tmpDir, 'check-critical-items.js');
  if (opts.importPath) {
    writeFileSync(testModulePath, readFileSync(opts.importPath, 'utf8'));
  } else {
    writeFileSync(testModulePath, readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  }

  // Create a mock `wl` script that returns controlled output
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });

  const wlScriptPath = join(binDir, 'wl');
  let wlScriptContent;
  
  if (wlCriticalItems === null) {
    // Simulate wl not found
    wlScriptContent = `#!/bin/bash\nexit 127\n`;
  } else if (wlCriticalItems === undefined) {
    // Simulate an empty response (no critical items)
    wlScriptContent = `#!/bin/bash\necho '{"success":true,"count":0,"workItems":[]}'\n`;
  } else {
    // Return the provided items
    const json = JSON.stringify({ success: true, count: wlCriticalItems.length, workItems: wlCriticalItems });
    wlScriptContent = `#!/bin/bash\necho '${json}'\n`;
  }

  writeFileSync(wlScriptPath, wlScriptContent, { mode: 0o755 });

  // Create a test harness that requires the module and calls checkCriticalItems()
  const harnessPath = join(tmpDir, 'run-test.mjs');
  const harnessCode = `
    import { checkCriticalItems } from './check-critical-items.js';
    const result = checkCriticalItems();
    process.stdout.write(JSON.stringify(result));
  `;
  writeFileSync(harnessPath, harnessCode);

  // Run with PATH pointing to our mock wl
  const env = { ...process.env, PATH: `${binDir}:${process.env.PATH}` };

  // Set WORKLOG_PROJECT_PATH to repo root so imports resolve correctly
  if (!env.WORKLOG_PROJECT_PATH) {
    env.WORKLOG_PROJECT_PATH = REPO_ROOT;
  }

  return spawnSync(process.execPath, [harnessPath], {
    encoding: 'utf-8',
    env,
    cwd: tmpDir,
    timeout: 10000,
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────

describe('checkCriticalItems()', () => {
  // ── Scenario (a): No blocking critical items ────────────────────────────
  test('passes when all critical items are completed/in_review', () => {
    const items = [
      {
        id: 'SA-00000000000000001',
        title: 'Completed feature A',
        status: 'completed',
        stage: 'in_review',
        priority: 'critical',
      },
      {
        id: 'SA-00000000000000002',
        title: 'Done feature B',
        status: 'completed',
        stage: 'done',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0, 'Exit code should be 0');
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, false);
    assert.strictEqual(output.blockingItems.length, 0);
    assert.ok(output.message.includes('critical-priority work item(s) are in a terminal state'));
  });

  // ── Scenario (b): One or more blocking critical items ──────────────────
  test('blocks when critical items are not in terminal state', () => {
    const items = [
      {
        id: 'SA-BLOCKING-0000001',
        title: 'Critical bug in production',
        status: 'in-progress',
        stage: 'in_progress',
        priority: 'critical',
      },
      {
        id: 'SA-00000000000000001',
        title: 'Completed feature A',
        status: 'completed',
        stage: 'in_review',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0, 'Exit code should be 0 (we return a report, not process exit)');
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, true);
    assert.strictEqual(output.blockingItems.length, 1);
    assert.strictEqual(output.blockingItems[0].workItemId, 'SA-BLOCKING-0000001');
    assert.strictEqual(output.blockingItems[0].title, 'Critical bug in production');
    assert.strictEqual(output.blockingItems[0].currentStatus, 'in-progress');
    assert.strictEqual(output.blockingItems[0].currentStage, 'in_progress');
    assert.ok(output.message.includes('SA-BLOCKING-0000001'));
    assert.ok(output.message.includes('Critical bug in production'));
  });

  // ── Scenario (b2): Multiple blocking critical items ─────────────────────
  test('reports multiple blocking critical items', () => {
    const items = [
      {
        id: 'SA-BLOCK-001',
        title: 'Blocking bug one',
        status: 'open',
        stage: 'idea',
        priority: 'critical',
      },
      {
        id: 'SA-BLOCK-002',
        title: 'Blocking bug two',
        status: 'in-progress',
        stage: 'intake_complete',
        priority: 'critical',
      },
      {
        id: 'SA-BLOCK-003',
        title: 'Blocking bug three',
        status: 'in-progress',
        stage: 'plan_complete',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, true);
    assert.strictEqual(output.blockingItems.length, 3);
  });

  // ── Scenario (c): No critical items at all ─────────────────────────────
  test('passes when no critical items exist', () => {
    const result = runCheckCriticalItems({ wlCriticalItems: [] });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, false);
    assert.strictEqual(output.blockingItems.length, 0);
    assert.ok(output.message.includes('No critical-priority work items found'));
  });

  // ── Edge case: wl CLI not available ────────────────────────────────────
  test('handles wl CLI failure gracefully', () => {
    const result = runCheckCriticalItems({ wlCriticalItems: null });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    // When wl fails, we should treat it as non-blocking (pass silently)
    // OR the module should handle the error gracefully
    assert.strictEqual(output.hasBlockingItems, false);
    assert.strictEqual(output.blockingItems.length, 0);
  });

  // ── Edge case: critical item with done stage ───────────────────────────
  test('passes when critical item is completed/done', () => {
    const items = [
      {
        id: 'SA-DONE-0000001',
        title: 'Released feature',
        status: 'completed',
        stage: 'done',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, false);
    assert.strictEqual(output.blockingItems.length, 0);
  });

  // ── Edge case: mixed terminal and non-terminal ─────────────────────────
  test('only flags non-terminal critical items', () => {
    const items = [
      {
        id: 'SA-TERM-001',
        title: 'Terminal item',
        status: 'completed',
        stage: 'in_review',
        priority: 'critical',
      },
      {
        id: 'SA-NONTERM-001',
        title: 'Non-terminal item',
        status: 'in-progress',
        stage: 'in_progress',
        priority: 'critical',
      },
      {
        id: 'SA-TERM-002',
        title: 'Another terminal item',
        status: 'completed',
        stage: 'done',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, true);
    assert.strictEqual(output.blockingItems.length, 1);
    assert.strictEqual(output.blockingItems[0].workItemId, 'SA-NONTERM-001');
  });

  // ── Edge case: critical item in completed status but wrong stage ───────
  test('flags critical item that is completed but not in terminal stage', () => {
    const items = [
      {
        id: 'SA-EDGE-001',
        title: 'Completed but not in_review/done',
        status: 'completed',
        stage: 'in_progress',
        priority: 'critical',
      },
    ];

    const result = runCheckCriticalItems({ wlCriticalItems: items });

    assert.strictEqual(result.status, 0);
    const output = JSON.parse(result.stdout);
    assert.strictEqual(output.hasBlockingItems, true);
    assert.strictEqual(output.blockingItems.length, 1);
    assert.strictEqual(output.blockingItems[0].workItemId, 'SA-EDGE-001');
  });
});
