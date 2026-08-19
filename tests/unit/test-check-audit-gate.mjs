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

test('check-audit-gate: ship.js re-exports getTopLevelCandidateItems', async () => {
  const shipMod = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'ship.js'));

  assert.ok(
    typeof shipMod.getTopLevelCandidateItems === 'function',
    'ship.js should re-export getTopLevelCandidateItems from check-audit-gate.js',
  );

  const checkModule = await import(join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js'));
  assert.equal(
    shipMod.getTopLevelCandidateItems,
    checkModule.getTopLevelCandidateItems,
    'ship.js should export the same getTopLevelCandidateItems function',
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
    assert.deepEqual(items, [{ id: 'SA-1', title: 'One', needsProducerReview: false, parentId: null }]);
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

  // Regression for dash: getCandidateItems must invoke the query via
  // 'bash -c' so that 'set -o pipefail' does not fail on dash systems
  // (where /bin/sh → dash).  The mock 'wl' only emits its payload when
  // its parent process is bash — if the outer shell is dash the mock
  // exits 1, and getCandidateItems() should return [].
  // (LP-0MSQ0NTMO00577UJ)
  test('wraps execSync command in bash -c for dash compatibility', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const payloadPath = join(tmpDir, 'payload.json');
    writeFileSync(payloadPath, JSON.stringify({
      success: true,
      workItems: [{ id: 'SA-1', title: 'Dash-safe', needsProducerReview: false }],
    }));
    const wlPath = join(tmpDir, 'wl');
    // This script checks its parent process name; it only emits payload
    // when the parent is bash (i.e. the query runs under bash -c).
    const script = `#!/usr/bin/env bash\n` +
      `parent=$(ps -o comm= -p $PPID 2>/dev/null)\n` +
      `case "$parent" in *bash*) cat "${payloadPath}" ;; *) exit 1 ;; esac\n`;
    writeFileSync(wlPath, script, { mode: 0o755 });

    const items = withWlMock(tmpDir, () => mod.getCandidateItems());

    assert.deepEqual(items, [{
      id: 'SA-1',
      title: 'Dash-safe',
      needsProducerReview: false,
      parentId: null,
    }], 'query must run under bash so set -o pipefail works on dash');
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

// ---------------------------------------------------------------------------
// 18. getCandidateItems parentId projection (SA-0MSUT8GQP004WSYN AC1)
// ---------------------------------------------------------------------------
describe('getCandidateItems - parentId projection', () => {
  test('projects parentId for every returned item', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [
      { id: 'SA-TOP-1', title: 'Top', needsProducerReview: false, parentId: null },
      { id: 'SA-CHILD-1', title: 'Child', needsProducerReview: false, parentId: 'SA-TOP-1' },
    ];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCandidateItems());

    assert.equal(items.length, 2, 'both items should be returned by getCandidateItems');
    for (const item of items) {
      assert.ok('id' in item, 'item should have id');
      assert.ok('title' in item, 'item should have title');
      assert.ok('needsProducerReview' in item, 'item should have needsProducerReview');
      assert.ok('parentId' in item, 'item should have parentId');
    }
    rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// 19. getTopLevelCandidateItems filtering (SA-0MSUT8GQP004WSYN AC1)
// ---------------------------------------------------------------------------
describe('getTopLevelCandidateItems - top-level filtering', () => {
  test('is exported as a function', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(typeof mod.getTopLevelCandidateItems, 'function');
  });

  test('returns only items with parentId == null', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [
      { id: 'SA-TOP-1', title: 'Top 1', needsProducerReview: false, parentId: null },
      { id: 'SA-CHILD-1', title: 'Child 1', needsProducerReview: false, parentId: 'SA-TOP-1' },
      { id: 'SA-TOP-2', title: 'Top 2', needsProducerReview: false, parentId: null },
      { id: 'SA-CHILD-2', title: 'Child 2', needsProducerReview: false, parentId: 'SA-TOP-2' },
    ];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getTopLevelCandidateItems());

    assert.deepEqual(
      items.map((i) => i.id),
      ['SA-TOP-1', 'SA-TOP-2'],
      'children (parentId set) should be excluded; top-level items retained',
    );
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('includes orphans (in_review with no parent) as top-level', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [
      { id: 'SA-ORPHAN-1', title: 'Orphan', needsProducerReview: false, parentId: null },
    ];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getTopLevelCandidateItems());

    assert.deepEqual(
      items.map((i) => i.id),
      ['SA-ORPHAN-1'],
      'an orphan (parentId null) is top-level and must still be gated',
    );
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('preserves parentId field on returned items', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [
      { id: 'SA-TOP-1', title: 'Top', needsProducerReview: false, parentId: null },
    ];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getTopLevelCandidateItems());

    assert.equal(items.length, 1);
    assert.equal(items[0].parentId, null);
    rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// 20. Audit gate excludes children (SA-0MSUT8GQP004WSYN AC2)
// ---------------------------------------------------------------------------
// The gate queries top-level candidates only, so an in_review child with a
// failing audit must not block. Hermetic: `wl` is mocked on PATH and returns
// only child items (parentId set); because the top-level list is empty the
// gate never even calls `wl audit-show` for them.
describe('checkAuditReadyToClose - top-level-only blocking', () => {
  // Run the async gate while binDir is on PATH, then restore PATH and clean up.
  async function runGateWithWlMock(workItems) {
    const mod = await import(MODULE_PATH);
    const savedPath = process.env.PATH;
    const binDir = mkdtempSync(join(tmpdir(), 'fakebin-'));
    const payloadPath = join(binDir, 'payload.json');
    writeFileSync(payloadPath, JSON.stringify({ success: true, workItems }), 'utf-8');
    const wlPath = join(binDir, 'wl');
    const script = `#!/usr/bin/env bash\n` + `cat "${payloadPath}"\n`;
    writeFileSync(wlPath, script, { mode: 0o755 });
    process.env.PATH = `${binDir}:${savedPath}`;
    try {
      const report = await mod.checkAuditReadyToClose();
      return { report, binDir };
    } finally {
      process.env.PATH = savedPath;
    }
  }

  test('does not block, and does not query audits, for in_review children', async () => {
    const mod = await import(MODULE_PATH);
    // Only children are in_review; each would fail an audit if queried, but
    // because the gate scopes to top-level they must be excluded entirely.
    const workItems = [
      { id: 'SA-CHILD-1', title: 'Child Blocking', needsProducerReview: false, parentId: 'SA-TOP-1' },
    ];
    const { report, binDir } = await runGateWithWlMock(workItems);

    assert.equal(report.hasBlockingItems, false, 'child audit gap should not block');
    assert.equal(report.blockingItems.length, 0);
    assert.equal(report.transientItems.length, 0, 'children should not appear as transient warnings');
    rmSync(binDir, { recursive: true, force: true });
  });

  test('does not list children in the transient-warning report', async () => {
    const workItems = [
      { id: 'SA-CHILD-T', title: 'Child Transient', needsProducerReview: false, parentId: 'SA-TOP-1' },
    ];
    const { report, binDir } = await runGateWithWlMock(workItems);

    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.transientItems.length, 0, 'children should not appear as transient warnings');
    assert.ok(!report.message.includes('SA-CHILD-T'), 'message should not mention the child');
    rmSync(binDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// 21. Producer-review top-level scoping (SA-0MSUT8GQP004WSYN AC3)
// ---------------------------------------------------------------------------
describe('checkProducerReviewStatus - top-level scoping', () => {
  test('does not block on children flagged needsProducerReview = true', async () => {
    const mod = await import(MODULE_PATH);
    // checkProducerReviewStatus operates on the list it is given; here we
    // pass a top-level-only list (children excluded upstream) and verify a
    // child flag does not appear because it was filtered out of the input.
    const result = mod.checkProducerReviewStatus([
      { id: 'SA-TOP-1', title: 'Top Ready', needsProducerReview: false, parentId: null },
    ]);
    assert.equal(result.hasBlockingItems, false, 'top-level list with no flagged items passes');
    assert.equal(result.blockingItems.length, 0);

    // And a top-level item still blocks as before.
    const blocking = mod.checkProducerReviewStatus([
      { id: 'SA-TOP-2', title: 'Top Flagged', needsProducerReview: true, parentId: null },
    ]);
    assert.equal(blocking.hasBlockingItems, true, 'top-level flagged item still blocks');
    assert.equal(blocking.blockingItems[0].workItemId, 'SA-TOP-2');
  });

  test('children with producer-review flags are excluded from blocking report', async () => {
    const mod = await import(MODULE_PATH);
    // Simulate the gate being invoked with the top-level list only: child
    // items (parentId set) have already been filtered out, so their flags
    // cannot block.
    const items = [
      { id: 'SA-TOP-1', title: 'Top', needsProducerReview: false, parentId: null },
    ];
    const result = mod.checkProducerReviewStatus(items);
    assert.equal(result.hasBlockingItems, false);
    assert.equal(result.blockingItems.length, 0);
  });
});

// ---------------------------------------------------------------------------
// 22. Conservative audit auto-remediation (SA-0MSUT8GQP004WSYN AC2/AC4, FT2)
// ---------------------------------------------------------------------------
// Hermetic: all command boundaries (candidate query, audit-show, remediation
// runner, runner resolver) are injected — no live `wl`/`audit_runner` runs,
// no worklog mutation, `wl update` is never invoked by the gate.

const TOP_LEVEL_ITEM = {
  id: 'SA-TOP-1',
  title: 'Top Level Item',
  needsProducerReview: false,
  parentId: null,
};

const AUDIT_SHOW = {
  success: true,
  workItemId: 'SA-TOP-1',
  audit: { readyToClose: true, summary: 'All good' },
};
const AUDIT_MISSING = { success: true, workItemId: 'SA-TOP-1', audit: null };
const AUDIT_TRANSIENT = {
  success: true,
  workItemId: 'SA-TOP-1',
  audit: {
    readyToClose: false,
    summary: 'Deep analysis timed out — manual review required.',
    rawOutput: 'Ready to close: No\n\nPhase 2 deep analysis timed out.',
  },
};
const AUDIT_GENUINE = {
  success: true,
  workItemId: 'SA-TOP-1',
  audit: {
    readyToClose: false,
    summary: '2 of 3 acceptance criteria not met.',
    rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria for SA-TOP-1 are not met.',
  },
};

/**
 * Build a stateful audit-show runner that returns each canned result in
 * sequence, repeating the last one for any further calls.
 */
function makeAuditShowSequence(results) {
  let i = 0;
  return () => JSON.stringify(results[Math.min(i++, results.length - 1)]);
}

/**
 * Run the gate with fully injected boundaries.
 *
 * @param {object} opts
 * @param {Array<object>} opts.auditShowResults - Canned `wl audit-show`
 *   payloads, returned in sequence.
 * @param {function} [opts.runAuditCommand] - Remediation runner; when absent
 *   a recording no-op that records invocations in `invocations`.
 * @param {string} [opts.runnerPath] - Path returned by the resolver.
 * @returns {Promise<{ report: object, invocations: Array<Array<string>> }>}
 */
async function runGateWithBoundaries({ auditShowResults, runAuditCommand, runnerPath = '/tmp/fake-audit_runner.py' }) {
  const mod = await import(MODULE_PATH);
  const invocations = [];
  const recordRunAuditCommand = runAuditCommand
    || ((path, id) => { invocations.push([path, id]); return 'ok'; });
  const report = await mod.checkAuditReadyToClose({
    getCandidateItemsFn: () => [TOP_LEVEL_ITEM],
    runAuditShow: makeAuditShowSequence(auditShowResults),
    runAuditCommand: recordRunAuditCommand,
    resolveAuditRunnerFn: () => runnerPath,
  });
  return { report, invocations };
}

describe('checkAuditReadyToClose - auto-remediation missing audit', () => {
  test('missing audit triggers re-run; passing re-run unblocks the item', async () => {
    const { report, invocations } = await runGateWithBoundaries({
      // First audit-show: missing. After remediation: passing.
      auditShowResults: [AUDIT_MISSING, AUDIT_SHOW],
    });

    assert.equal(invocations.length, 1, 'exactly one remediation re-run should be invoked');
    assert.deepEqual(invocations[0], ['/tmp/fake-audit_runner.py', 'SA-TOP-1']);
    assert.equal(report.hasBlockingItems, false, 'passing re-run should unblock');
    assert.equal(report.blockingItems.length, 0);
    assert.equal(report.remediatedItems.length, 1, 'remediated item should be reported');
    assert.equal(report.remediatedItems[0].workItemId, 'SA-TOP-1');
  });

  test('missing audit re-run that still fails blocks the item', async () => {
    const { report, invocations } = await runGateWithBoundaries({
      auditShowResults: [AUDIT_MISSING, AUDIT_MISSING],
    });

    assert.equal(invocations.length, 1, 'remediation should be attempted once');
    assert.equal(report.hasBlockingItems, true, 'still-missing after re-run should block');
    assert.equal(report.blockingItems.length, 1);
    assert.equal(report.blockingItems[0].workItemId, 'SA-TOP-1');
    assert.ok(report.blockingItems[0].remediation.includes('audit_runner.py'), 'manual remediation command surfaced');
  });
});

describe('checkAuditReadyToClose - auto-remediation transient audit', () => {
  test('transient audit triggers re-run; passing re-run unblocks the item', async () => {
    const { report, invocations } = await runGateWithBoundaries({
      auditShowResults: [AUDIT_TRANSIENT, AUDIT_SHOW],
    });

    assert.equal(invocations.length, 1, 'exactly one remediation re-run should be invoked');
    assert.equal(report.hasBlockingItems, false, 'passing re-run should unblock');
    assert.equal(report.blockingItems.length, 0);
    assert.equal(report.remediatedItems.length, 1);
  });

  test('transient audit re-run that still fails blocks the item', async () => {
    const { report, invocations } = await runGateWithBoundaries({
      auditShowResults: [AUDIT_TRANSIENT, AUDIT_TRANSIENT],
    });

    assert.equal(invocations.length, 1);
    assert.equal(report.hasBlockingItems, true, 'still-failing after re-run should block');
    assert.equal(report.blockingItems.length, 1);
  });
});

describe('checkAuditReadyToClose - genuine verdict immediate block', () => {
  test('genuine not-ready verdict blocks immediately with no re-run', async () => {
    const { report, invocations } = await runGateWithBoundaries({
      auditShowResults: [AUDIT_GENUINE],
    });

    assert.equal(invocations.length, 0, 'no remediation re-run should be invoked for a genuine verdict');
    assert.equal(report.hasBlockingItems, true, 'genuine not-ready verdict must block');
    assert.equal(report.blockingItems.length, 1);
    assert.equal(report.blockingItems[0].reason, 'Audit verdict: not ready to close');
    assert.equal(report.remediatedItems.length, 0);
  });
});

describe('checkAuditReadyToClose - remediation runner failure', () => {
  test('runner failure blocks the item and surfaces the manual remediation command', async () => {
    const failingRunner = () => {
      const err = new Error('audit_runner.py exited with code 2');
      err.stderr = Buffer.from('boom');
      throw err;
    };
    const { report, invocations } = await runGateWithBoundaries({
      auditShowResults: [AUDIT_MISSING],
      runAuditCommand: failingRunner,
    });

    assert.equal(invocations.length, 0);
    assert.equal(report.hasBlockingItems, true, 'runner failure must block');
    assert.equal(report.blockingItems.length, 1);
    assert.match(report.blockingItems[0].reason, /Audit remediation failed/);
    assert.ok(
      report.blockingItems[0].remediation.includes('audit_runner.py issue SA-TOP-1'),
      'manual remediation command must be surfaced',
    );
  });

  test('re-check failure after a successful re-run blocks the item', async () => {
    const mod = await import(MODULE_PATH);
    // First audit-show: missing. Second call (re-check): throws.
    const invocations = [];
    const report = await mod.checkAuditReadyToClose({
      getCandidateItemsFn: () => [TOP_LEVEL_ITEM],
      runAuditShow: (() => {
        let i = 0;
        return () => {
          i += 1;
          if (i === 1) { return JSON.stringify(AUDIT_MISSING); }
          const err = new Error('wl audit-show failed on re-check');
          err.stderr = Buffer.from('re-check boom');
          throw err;
        };
      })(),
      runAuditCommand: (path, id) => { invocations.push([path, id]); return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });

    assert.equal(invocations.length, 1);
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
    assert.match(report.blockingItems[0].reason, /Failed to re-check audit after remediation/);
  });
});

describe('resolveAuditRunner - path resolution', () => {
  test('prefers the in-repo audit runner when it exists', async () => {
    const mod = await import(MODULE_PATH);
    const fakeFs = {
      existsSync: (p) => p.includes('skill/audit/scripts/audit_runner.py'),
    };
    const resolved = mod.resolveAuditRunner(fakeFs);
    assert.ok(
      resolved.endsWith('skill/audit/scripts/audit_runner.py'),
      `should prefer in-repo runner, got: ${resolved}`,
    );
  });

  test('falls back to the global skill runner when in-repo is absent', async () => {
    const mod = await import(MODULE_PATH);
    const fakeFs = {
      existsSync: (p) => p.includes('.pi/agent/skills/audit/scripts/audit_runner.py'),
    };
    const resolved = mod.resolveAuditRunner(fakeFs);
    assert.ok(
      resolved.includes('.pi/agent/skills/audit/scripts/audit_runner.py'),
      `should fall back to global runner, got: ${resolved}`,
    );
  });

  test('returns the in-repo path when neither exists (fails loudly, never silently)', async () => {
    const mod = await import(MODULE_PATH);
    const fakeFs = { existsSync: () => false };
    const resolved = mod.resolveAuditRunner(fakeFs);
    assert.ok(resolved.endsWith('skill/audit/scripts/audit_runner.py'));
  });
});

describe('checkAuditReadyToClose - gate never invokes wl update', () => {
  test('remediation goes through the audit runner only, never wl update', async () => {
    const mod = await import(MODULE_PATH);
    const invocations = [];
    const report = await mod.checkAuditReadyToClose({
      getCandidateItemsFn: () => [TOP_LEVEL_ITEM],
      runAuditShow: makeAuditShowSequence([AUDIT_MISSING, AUDIT_SHOW]),
      runAuditCommand: (path, id) => { invocations.push([path, id]); return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });

    assert.equal(invocations.length, 1);
    assert.deepEqual(invocations[0], ['/tmp/fake-audit_runner.py', 'SA-TOP-1']);
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.remediatedItems.length, 1);
  });
});
