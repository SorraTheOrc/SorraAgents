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
    assert.ok(mod.getCandidateItems);
  });
});
