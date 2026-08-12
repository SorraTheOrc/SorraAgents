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
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
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
    assert.equal(result.transient, false);
  });
});

// ---------------------------------------------------------------------------
// getAuditStatus / isTimeoutOrTransientAudit — timeout vs genuine failure
// (SA-0MS95HJ0J004IDIW AC1: a timeout/transient audit must NOT hard-block)
// ---------------------------------------------------------------------------
describe('getAuditStatus - timeout vs genuine not-ready distinction', () => {
  test('treats readyToClose:false with timeout marker as transient non-blocking', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-101',
      audit: {
        readyToClose: false,
        summary: 'Some summary',
        rawOutput: 'Ready to close: No\n\n## Summary\n\nDeep analysis timed out — manual review required.',
      },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-101', title: 'Timed Out Item' },
      auditData,
    );
    assert.equal(result.isBlocking, false, 'timeout audit should not hard-block');
    assert.equal(result.transient, true, 'timeout audit should be flagged transient');
    assert.match(result.reason, /timeout|transient/i);
  });

  test('treats readyToClose:false with provider-error marker as transient non-blocking', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-102',
      audit: {
        readyToClose: false,
        summary: 'Some summary',
        rawOutput: 'Ready to close: No\n\nPi provider error: upstream unavailable — criterion could not be evaluated.',
      },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-102', title: 'Provider Error Item' },
      auditData,
    );
    assert.equal(result.isBlocking, false, 'provider-error audit should not hard-block');
    assert.equal(result.transient, true);
  });

  test('treats readyToClose:false with FailureNotice wrapper as transient non-blocking', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-103',
      audit: {
        readyToClose: false,
        summary: 'Some summary',
        rawOutput: '======================================================================\n⚠ Script Execution Failure: pi (Phase 2 deep analysis) — Timeout after 1800s\nThe following output was produced manually.\n======================================================================',
      },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-103', title: 'Failure Notice Item' },
      auditData,
    );
    assert.equal(result.isBlocking, false);
    assert.equal(result.transient, true);
  });

  test('treats readyToClose:false with clean output as genuine blocking failure', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-104',
      audit: {
        readyToClose: false,
        summary: 'Some summary',
        rawOutput: 'Ready to close: No\n\n## Summary\n\n2 of 3 acceptance criteria for work item SA-104 are not met.',
      },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-104', title: 'Genuinely Not Ready' },
      auditData,
    );
    assert.equal(result.isBlocking, true, 'genuine not-ready audit should hard-block');
    assert.equal(result.transient, false);
    assert.equal(result.reason, 'Audit verdict: not ready to close');
  });

  test('treats readyToClose:false with summary-only timeout marker as transient', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      success: true,
      workItemId: 'SA-105',
      audit: {
        readyToClose: false,
        summary: 'Ready to close: No — Phase 2 deep analysis timed out. Manual audit required.',
        rawOutput: null,
      },
    };
    const result = mod.getAuditStatus(
      { id: 'SA-105', title: 'Summary Timeout' },
      auditData,
    );
    assert.equal(result.isBlocking, false);
    assert.equal(result.transient, true);
  });
});

describe('isTimeoutOrTransientAudit', () => {
  test('is exported as a function', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(typeof mod.isTimeoutOrTransientAudit, 'function');
  });

  test('detects timeout markers in rawOutput', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.isTimeoutOrTransientAudit({ rawOutput: 'Deep analysis timed out — manual review required.' }),
      true,
    );
    assert.equal(
      mod.isTimeoutOrTransientAudit({ rawOutput: 'Pi model call timed out after 1800s. Manual audit required.' }),
      true,
    );
  });

  test('detects provider-error markers', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.isTimeoutOrTransientAudit({ rawOutput: 'Pi provider error: upstream down' }),
      true,
    );
  });

  test('detects markers in summary when rawOutput is absent', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.isTimeoutOrTransientAudit({ rawOutput: null, summary: 'Phase 2 deep analysis timed out. Manual review required.' }),
      true,
    );
  });

  test('returns false for genuine not-ready audits without transient markers', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(
      mod.isTimeoutOrTransientAudit({ rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria not met.' }),
      false,
    );
  });

  test('returns false for null/undefined audit', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.isTimeoutOrTransientAudit(null), false);
    assert.equal(mod.isTimeoutOrTransientAudit(undefined), false);
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
  assert.ok('transientItems' in report);
  assert.ok('message' in report);
  assert.ok(Array.isArray(report.blockingItems));
  assert.ok(Array.isArray(report.transientItems));
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
// 17. getCandidateItems — single stage query + jq projection (SA-0MSLW5P7J0068UFZ,
//     SA-0MSPPDCTH004561Z)
// ---------------------------------------------------------------------------
// These tests mock `wl` on PATH to verify the query/projection contract:
// exactly ONE `wl list --stage in_review --json`
// invocation piped through jq, so large outputs cannot overflow execSync's
// default 1 MB buffer (ENOBUFS).

/**
 * Create a fake `wl` executable on a temp PATH that records its arguments
 * and emits a canned workItems payload (or fails).
 *
 * @param {object} opts
 * @param {Array<object>} opts.workItems - Items for the fake `wl list` output.
 * @param {string} opts.argsLogPath - File where the fake wl appends its args.
 * @param {boolean} [opts.fail] - When true the fake wl exits 1.
 * @returns {string} The temp bin dir to prepend to PATH.
 */
function createWlMock({ workItems, argsLogPath, fail = false }) {
  const binDir = mkdtempSync(join(tmpdir(), 'fakebin-'));
  const payloadPath = join(binDir, 'payload.json');
  writeFileSync(payloadPath, JSON.stringify({ success: true, workItems }), 'utf-8');
  const wlPath = join(binDir, 'wl');
  const script = `#!/usr/bin/env bash\n`
    + `echo "$@" >> "${argsLogPath}"\n`
    + (fail ? 'exit 1\n' : `cat "${payloadPath}"\n`);
  writeFileSync(wlPath, script, { mode: 0o755 });
  return binDir;
}

/**
 * Run *fn* with *binDir* prepended to PATH, restoring PATH afterwards.
 */
function withWlMock(binDir, fn) {
  const savedPath = process.env.PATH;
  process.env.PATH = `${binDir}:${savedPath}`;
  try {
    return fn();
  } finally {
    process.env.PATH = savedPath;
    rmSync(binDir, { recursive: true, force: true });
  }
}

describe('getCandidateItems - single stage query + jq projection', () => {
  test('issues exactly one stage-only query', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [{ id: 'SA-1', title: 'One', needsProducerReview: false }];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCandidateItems());
    const calls = readFileSync(argsLogPath, 'utf-8').trim().split('\n').filter(Boolean);

    assert.equal(calls.length, 1, 'should be exactly one wl list invocation');
    assert.ok(calls[0].includes('--stage in_review'), `should filter stage, got: ${calls[0]}`);
    assert.ok(!calls[0].includes('--status completed'), `should drop redundant status filter (completed-minus-done == in_review), got: ${calls[0]}`);
    assert.ok(calls[0].includes('--json'), `should request JSON, got: ${calls[0]}`);
    assert.deepEqual(items, [{ id: 'SA-1', title: 'One', needsProducerReview: false }]);
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('does not overflow execSync buffer with >1MB wl output', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    // ~200 items x ~6KB description ≈ 1.2MB — over execSync's default 1MB maxBuffer
    const workItems = Array.from({ length: 200 }, (_, i) => ({
      id: `SA-LARGE-${i}`,
      title: `Large item ${i}`,
      needsProducerReview: false,
      description: 'x'.repeat(6000),
    }));
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCandidateItems());

    assert.equal(items.length, 200, 'all items should be returned without ENOBUFS');
    for (const item of items) {
      assert.ok('id' in item && 'title' in item && 'needsProducerReview' in item);
    }
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns [] and logs a warning when wl query fails', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlMock({ workItems: [], argsLogPath, fail: true });

    const items = withWlMock(binDir, () => mod.getCandidateItems());

    assert.deepEqual(items, [], 'failed query should yield no candidates');
    rmSync(tmpDir, { recursive: true, force: true });
  });
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
