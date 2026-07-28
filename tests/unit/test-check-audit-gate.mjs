/**
 * Unit tests for skill/ship/scripts/check-audit-gate.js
 *
 * Tests the audit readiness gating logic used in the ship skill's
 * release process. The gate checks all `in_review`/`completed` work items
 * for their `audit.readyToClose` status and blocks the release if any
 * items are not ready.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');

// ---------------------------------------------------------------------------
// 1. Module file exists
// ---------------------------------------------------------------------------
test('check-audit-gate: module file exists', () => {
  assert.ok(
    existsSync(MODULE_PATH),
    'skill/ship/scripts/check-audit-gate.js should exist',
  );
});

// ---------------------------------------------------------------------------
// 2. Module exports expected functions
// ---------------------------------------------------------------------------
test('check-audit-gate: exports expected functions', async () => {
  const mod = await import(MODULE_PATH);
  assert.equal(typeof mod.checkAuditReadyToClose, 'function');
  assert.equal(typeof mod.getAuditStatus, 'function');
  assert.equal(typeof mod.getCandidateItems, 'function');
});

// ---------------------------------------------------------------------------
// 3. Pure function: isBlockingAudit (tested indirectly via checkAuditReadyToClose)
//    Tests the buildBlockingMessage helper to verify report structure
// ---------------------------------------------------------------------------
describe('getAuditStatus - blocking condition detection', () => {
  test('recognises null audit as blocking', async () => {
    const mod = await import(MODULE_PATH);
    const result = mod.getAuditStatus(
      { id: 'SA-001', title: 'Test Item' },
      null,
    );
    assert.equal(result.isBlocking, true);
    assert.equal(
      result.reason,
      'No audit found',
    );
  });

  test('recognises audit with readyToClose: false as blocking', async () => {
    const mod = await import(MODULE_PATH);
    // Simulate wl audit-show output structure: { success, workItemId, audit }
    const auditData = {
      success: true,
      workItemId: 'SA-002',
      audit: { readyToClose: false, summary: 'Some issues remain' },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-002', title: 'Test Item' },
      auditData,
    );
    assert.equal(result.isBlocking, true);
    assert.equal(
      result.reason,
      'Audit verdict: not ready to close',
    );
  });

  test('recognises audit with readyToClose: true as passing', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-003',
      audit: { readyToClose: true, summary: 'All good' },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-003', title: 'Test Item' },
      auditData,
    );
    assert.equal(result.isBlocking, false);
  });
});

// ---------------------------------------------------------------------------
// 5. checkAuditReadyToClose returns expected structure
// ---------------------------------------------------------------------------
test('check-audit-gate: checkAuditReadyToClose returns expected structure', async () => {
  const mod = await import(MODULE_PATH);

  const report = await mod.checkAuditReadyToClose();

  // Should always return the expected shape
  assert.ok(typeof report === 'object');
  assert.ok('hasBlockingItems' in report);
  assert.ok('blockingItems' in report);
  assert.ok('message' in report);
  assert.ok(Array.isArray(report.blockingItems));
  assert.equal(typeof report.message, 'string');
});

// ---------------------------------------------------------------------------
// 6. SKILL.md documents the audit gating step
// ---------------------------------------------------------------------------
test('check-audit-gate: SKILL.md documents the audit gating step', () => {
  const skillPath = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
  const content = readFileSync(skillPath, 'utf-8');

  // The SKILL.md should reference audit readiness gating
  assert.ok(
    content.includes('audit') &&
    (content.includes('readiness') || content.includes('gate') || content.includes('ready to close')),
    'SKILL.md should document the audit readiness gating step',
  );
});

// ---------------------------------------------------------------------------
// 7. ship.js re-exports checkAuditReadyToClose
// ---------------------------------------------------------------------------
test('check-audit-gate: ship.js re-exports checkAuditReadyToClose', async () => {
  const shipMod = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'ship.js'));

  assert.ok(
    typeof shipMod.checkAuditReadyToClose === 'function',
    'ship.js should re-export checkAuditReadyToClose from check-audit-gate.js',
  );

  // Verify the function is the same by checking identity
  const checkModule = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js'));
  assert.equal(
    shipMod.checkAuditReadyToClose,
    checkModule.checkAuditReadyToClose,
    'ship.js should export the same checkAuditReadyToClose function',
  );
});

// ---------------------------------------------------------------------------
// 8. run-release.js imports and uses the audit gate
// ---------------------------------------------------------------------------
test('check-audit-gate: run-release.js imports checkAuditReadyToClose', async () => {
  const runReleaseContent = readFileSync(
    join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js'),
    'utf-8',
  );

  assert.ok(
    runReleaseContent.includes('check-audit-gate') &&
    runReleaseContent.includes('checkAuditReadyToClose'),
    'run-release.js should import checkAuditReadyToClose from check-audit-gate.js',
  );
});

// ---------------------------------------------------------------------------
// 9. run-release.js uses exit code 6 for audit gate failure
// ---------------------------------------------------------------------------
test('check-audit-gate: run-release.js uses exit code 6 for audit gate failure', () => {
  const runReleaseContent = readFileSync(
    join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js'),
    'utf-8',
  );

  // Should reference exit code 6 for audit gate failures
  assert.ok(
    runReleaseContent.includes('return 6') ||
    runReleaseContent.includes('exit code 6') ||
    runReleaseContent.includes('exitCode = 6'),
    'run-release.js should use exit code 6 for audit gate failures',
  );
});

// ---------------------------------------------------------------------------
// 10. SKILL.md documents exit code 6
// ---------------------------------------------------------------------------
test('check-audit-gate: SKILL.md documents exit code 6 for audit gate', () => {
  const skillPath = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
  const content = readFileSync(skillPath, 'utf-8');

  // Should reference exit code 6 in the audit gate context
  assert.ok(
    content.includes('exit code 6') ||
    content.includes('exit 6') ||
    (content.includes('6') && content.includes('audit')),
    'SKILL.md should reference exit code 6 related to audit gating',
  );
});

// ---------------------------------------------------------------------------
// 11. Module structure checks
// ---------------------------------------------------------------------------
describe('check-audit-gate module structure', () => {
  test('uses ESM exports', async () => {
    const mod = await import(MODULE_PATH);
    assert.ok(mod.checkAuditReadyToClose);
    assert.ok(mod.getAuditStatus);
    assert.ok(mod.getCandidateItems);
    assert.ok(mod.checkProducerReviewStatus);
  });
});

// ---------------------------------------------------------------------------
// 12. getCandidateItems returns needsProducerReview field per AC6
// ---------------------------------------------------------------------------
describe('getCandidateItems - needsProducerReview field', () => {
  test('returns needsProducerReview field for each item', async () => {
    const mod = await import(MODULE_PATH);
    const items = mod.getCandidateItems();
    for (const item of items) {
      assert.ok('id' in item, 'item should have id');
      assert.ok('title' in item, 'item should have title');
      assert.ok('needsProducerReview' in item, 'item should have needsProducerReview');
    }
  });

  test('needsProducerReview is boolean or null', async () => {
    const mod = await import(MODULE_PATH);
    const items = mod.getCandidateItems();
    for (const item of items) {
      if (item.needsProducerReview !== null) {
        assert.equal(typeof item.needsProducerReview, 'boolean',
          `needsProducerReview should be boolean or null, got ${typeof item.needsProducerReview} for ${item.id}`);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 13. checkProducerReviewStatus function
// ---------------------------------------------------------------------------
describe('checkProducerReviewStatus', () => {
  test('is exported as a function', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(typeof mod.checkProducerReviewStatus, 'function');
  });

  test('returns expected structure for empty items', async () => {
    const mod = await import(MODULE_PATH);
    const result = mod.checkProducerReviewStatus([]);
    assert.ok(typeof result === 'object');
    assert.ok('hasBlockingItems' in result);
    assert.ok('blockingItems' in result);
    assert.ok('message' in result);
    assert.ok(Array.isArray(result.blockingItems));
    assert.equal(typeof result.message, 'string');
    assert.equal(result.hasBlockingItems, false);
  });

  test('flags items with needsProducerReview = true as blocking', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      { id: 'SA-001', title: 'Needs Review', needsProducerReview: true },
      { id: 'SA-002', title: 'Ready', needsProducerReview: false },
    ];
    const result = mod.checkProducerReviewStatus(items);
    assert.equal(result.hasBlockingItems, true);
    assert.equal(result.blockingItems.length, 1);
    assert.equal(result.blockingItems[0].workItemId, 'SA-001');
    assert.ok(result.blockingItems[0].remediation.includes('wl update'));
  });

  test('flags items with needsProducerReview = null as blocking', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      { id: 'SA-003', title: 'Unknown', needsProducerReview: null },
    ];
    const result = mod.checkProducerReviewStatus(items);
    assert.equal(result.hasBlockingItems, true);
    assert.equal(result.blockingItems.length, 1);
    assert.equal(result.blockingItems[0].workItemId, 'SA-003');
  });

  test('flags items with needsProducerReview = undefined as blocking', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      { id: 'SA-004', title: 'Missing Field' },
    ];
    const result = mod.checkProducerReviewStatus(items);
    assert.equal(result.hasBlockingItems, true);
    assert.equal(result.blockingItems.length, 1);
    assert.equal(result.blockingItems[0].workItemId, 'SA-004');
  });

  test('passes when all items have needsProducerReview = false', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      { id: 'SA-005', title: 'Ready 1', needsProducerReview: false },
      { id: 'SA-006', title: 'Ready 2', needsProducerReview: false },
    ];
    const result = mod.checkProducerReviewStatus(items);
    assert.equal(result.hasBlockingItems, false);
    assert.equal(result.blockingItems.length, 0);
  });

  test('blockingItems entries have expected fields', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      { id: 'SA-007', title: 'Blocked', needsProducerReview: true },
    ];
    const result = mod.checkProducerReviewStatus(items);
    const entry = result.blockingItems[0];
    assert.ok('workItemId' in entry);
    assert.ok('title' in entry);
    assert.ok('needsProducerReview' in entry);
    assert.ok('reason' in entry);
    assert.ok('remediation' in entry);
    assert.equal(entry.workItemId, 'SA-007');
    assert.equal(entry.needsProducerReview, true);
  });
});

// ---------------------------------------------------------------------------
// 14. buildProducerReviewRemediationCommand helper
// ---------------------------------------------------------------------------
describe('buildProducerReviewRemediationCommand', () => {
  test('is exported as a function', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(typeof mod.buildProducerReviewRemediationCommand, 'function');
  });

  test('returns a string containing wl update command', async () => {
    const mod = await import(MODULE_PATH);
    const cmd = mod.buildProducerReviewRemediationCommand('SA-001');
    assert.equal(typeof cmd, 'string');
    assert.ok(cmd.includes('wl update SA-001'), 'Should contain wl update command');
    assert.ok(cmd.includes('needsProducerReview'), 'Should mention needsProducerReview');
  });
});

// ---------------------------------------------------------------------------
// 15. ship.js re-exports checkProducerReviewStatus
// ---------------------------------------------------------------------------
test('check-audit-gate: ship.js re-exports checkProducerReviewStatus', async () => {
  const shipMod = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'ship.js'));

  assert.ok(
    typeof shipMod.checkProducerReviewStatus === 'function',
    'ship.js should re-export checkProducerReviewStatus from check-audit-gate.js',
  );

  // Verify the function is the same by checking identity
  const checkModule = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js'));
  assert.equal(
    shipMod.checkProducerReviewStatus,
    checkModule.checkProducerReviewStatus,
    'ship.js should export the same checkProducerReviewStatus function',
  );
});

// ---------------------------------------------------------------------------
// 16. JSDoc consistency: checkProducerReviewStatus has JSDoc
// ---------------------------------------------------------------------------
test('check-audit-gate: checkProducerReviewStatus has JSDoc comment', async () => {
  const content = readFileSync(MODULE_PATH, 'utf-8');
  // Find the index of 'export function checkProducerReviewStatus' and search
  // backwards for the preceding JSDoc comment (greedy match for the most
  // recent /** ... */ block before that line).
  const fnIndex = content.indexOf('export function checkProducerReviewStatus');
  assert.ok(fnIndex >= 0, 'should find checkProducerReviewStatus export');
  const beforeFn = content.slice(0, fnIndex);
  const jsdocMatch = beforeFn.match(/\/\*\*[\s\S]*?\*\//g);
  assert.ok(jsdocMatch && jsdocMatch.length > 0, 'checkProducerReviewStatus should have a JSDoc comment');
  const jsdoc = jsdocMatch[jsdocMatch.length - 1];
  assert.ok(jsdoc.includes('@returns'), 'JSDoc should document return type');
  assert.ok(jsdoc.includes('@param'), 'JSDoc should document parameters');
});
