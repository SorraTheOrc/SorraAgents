/**
 * Unit tests for skill/ship/scripts/check-unmerged-branches.js
 *
 * Tests the unmerged branch detection, work item ID extraction,
 * and report generation logic.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');

// ---------------------------------------------------------------------------
// 1. Module file exists
// ---------------------------------------------------------------------------
test('check-unmerged-branches: module file exists', () => {
  assert.ok(
    existsSync(MODULE_PATH),
    'skill/ship/scripts/check-unmerged-branches.js should exist',
  );
});

// ---------------------------------------------------------------------------
// 2. Module exports expected functions
// ---------------------------------------------------------------------------
test('check-unmerged-branches: exports expected functions', async () => {
  const mod = await import(MODULE_PATH);
  assert.equal(typeof mod.getUnmergedBranchNames, 'function');
  assert.equal(typeof mod.extractWorkItemId, 'function');
  assert.equal(typeof mod.getWorkItemStatus, 'function');
  assert.equal(typeof mod.checkUnmergedBranches, 'function');
  assert.equal(typeof mod.getCurrentBranch, 'function');
});

// ---------------------------------------------------------------------------
// 3. extractWorkItemId - Pure function tests
// ---------------------------------------------------------------------------
describe('extractWorkItemId', () => {
  test('extracts ID from standard wl-<id>-<slug> pattern', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-SA-0MPDZDPZB00121IE-branch-naming-policy'),
      'SA-0MPDZDPZB00121IE',
    );
  });

  test('extracts simple ID like SA-001', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-SA-001-fix-login-bug'),
      'SA-001',
    );
  });

  test('extracts ID with multiple hyphen groups', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-SA-0MPYMFZXO0004ZU4-test-something'),
      'SA-0MPYMFZXO0004ZU4',
    );
  });

  test('returns null for branches without wl- prefix', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('feature/SA-001-something'),
      null,
    );
  });

  test('returns null for branches not matching pattern (e.g., dev)', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('dev'),
      null,
    );
  });

  test('returns null for branches with lowercase work item ID', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-sa-001-fix-bug'),
      null,
    );
  });

  test('returns null for empty string', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId(''),
      null,
    );
  });

  test('returns null for branches with underscores', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-SA-001-bad_name'),
      null,
    );
  });

  test('handles branch with only wl- prefix and no slug', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.extractWorkItemId('wl-SA-001-'),
      null,
    );
  });
});

// ---------------------------------------------------------------------------
// 4. Module structure checks
// ---------------------------------------------------------------------------
describe('check-unmerged-branches module structure', () => {
  test('uses ESM exports', async () => {
    const content = await import(MODULE_PATH);
    // All expected exports are present
    assert.ok(content.getUnmergedBranchNames);
    assert.ok(content.extractWorkItemId);
    assert.ok(content.getWorkItemStatus);
    assert.ok(content.checkUnmergedBranches);
    assert.ok(content.getCurrentBranch);
  });
});

// ---------------------------------------------------------------------------
// 5. SKILL.md references the check-unmerged-branches gating step
// ---------------------------------------------------------------------------
test('check-unmerged-branches: SKILL.md documents the gating step', () => {
  const skillPath = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
  const content = readFileSync(skillPath, 'utf-8');
  
  // The SKILL.md should reference checking for unmerged branches
  assert.ok(
    content.includes('unmerged') ||
    content.includes('check-unmerged') ||
    content.includes('Unmerged branches') ||
    content.includes('gating'),
    'SKILL.md should document the unmerged branches gating step',
  );
});

// ---------------------------------------------------------------------------
// 6. ship.js re-exports checkUnmergedBranches
// ---------------------------------------------------------------------------
test('check-unmerged-branches: ship.js re-exports checkUnmergedBranches', async () => {
  const shipMod = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'ship.js'));
  
  assert.ok(
    typeof shipMod.checkUnmergedBranches === 'function',
    'ship.js should re-export checkUnmergedBranches from check-unmerged-branches.js',
  );
  
  // Verify the function is the same by checking identity
  const checkModule = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js'));
  assert.equal(
    shipMod.checkUnmergedBranches,
    checkModule.checkUnmergedBranches,
    'ship.js should export the same checkUnmergedBranches function',
  );
});

// ---------------------------------------------------------------------------
// 7. checkUnmergedBranches returns correct structure
// ---------------------------------------------------------------------------
test('check-unmerged-branches: checkUnmergedBranches returns expected structure when no unmerged branches', async () => {
  const mod = await import(MODULE_PATH);
  
  // When there are no unmerged branches (or on a fresh dev branch), the function
  // should return a clean "no issues" result.
  const report = mod.checkUnmergedBranches();
  
  // Should always return the expected shape
  assert.ok(typeof report === 'object');
  assert.ok('hasUnmergedBranches' in report);
  assert.ok('unmergedBranches' in report);
  assert.ok('message' in report);
  assert.ok(Array.isArray(report.unmergedBranches));
  assert.equal(typeof report.message, 'string');
});

// ---------------------------------------------------------------------------
// 8. getUnmergedBranchNames is a function that doesn't throw
// ---------------------------------------------------------------------------
test('check-unmerged-branches: getUnmergedBranchNames runs without throwing', async () => {
  const mod = await import(MODULE_PATH);
  
  // Should not throw even if dev doesn't exist (returns empty array gracefully)
  let result;
  try {
    result = mod.getUnmergedBranchNames();
    assert.ok(Array.isArray(result));
  } catch (err) {
    // If we're on a branch where dev exists, this should work
    // If we hit some other error, it should be handled gracefully
    assert.fail(`getUnmergedBranchNames threw: ${err.message}`);
  }
});
