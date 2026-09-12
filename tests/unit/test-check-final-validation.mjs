/**
 * Unit tests for skill/ship/scripts/check-final-validation.js
 *
 * Covers the final validation sweep (SA-0MTMSPKEX003JGIX Step 3.7):
 *   - all in_review items queried (top-level AND children),
 *   - missing / stale / failing audits classified correctly,
 *   - missing/stale/transient audits auto-remediated; passing re-run unblocks,
 *     still-failing re-run blocks,
 *   - genuine (fresh) "not ready to close" verdicts block immediately with no
 *     re-audit attempt,
 *   - producer-review flags block with remediation guidance,
 *   - the gate never calls `wl update` (remediation goes through the audit
 *     runner only),
 *   - run-release.js wires Step 3.7 with exit code 12, and
 *   - SKILL.md / reference docs document the gate.
 *
 * All command boundaries are injected so the suite is hermetic (no live
 * `wl` / `audit_runner` invocations).
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-final-validation.js');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SKILL_MD = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const REFERENCE_MD = join(REPO_ROOT, 'skill', 'ship', 'docs', 'dev', 'ship-skill-reference.md');

// ── Module shape ─────────────────────────────────────────────────────────────

test('check-final-validation: module file exists', () => {
  assert.ok(existsSync(MODULE_PATH), 'skill/ship/scripts/check-final-validation.js should exist');
});

test('check-final-validation: exports expected functions', async () => {
  const mod = await import(MODULE_PATH);
  assert.equal(typeof mod.checkFinalValidation, 'function');
  assert.equal(typeof mod.getInReviewItems, 'function');
  assert.equal(typeof mod.classifyAudit, 'function');
  assert.equal(typeof mod.isAuditStale, 'function');
  assert.equal(typeof mod.parseIsoUtc, 'function');
});

// ── parseIsoUtc ──────────────────────────────────────────────────────────────

describe('parseIsoUtc', () => {
  test('parses a Z-suffixed timestamp', async () => {
    const mod = await import(MODULE_PATH);
    const d = mod.parseIsoUtc('2026-09-04T08:28:41.553Z');
    assert.ok(d instanceof Date);
    assert.equal(d.toISOString(), '2026-09-04T08:28:41.553Z');
  });

  test('treats a naive timestamp as UTC', async () => {
    const mod = await import(MODULE_PATH);
    const d = mod.parseIsoUtc('2026-09-04T08:28:41.553');
    assert.equal(d.toISOString(), '2026-09-04T08:28:41.553Z');
  });

  test('returns null for missing/unparseable input', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.parseIsoUtc(null), null);
    assert.equal(mod.parseIsoUtc(undefined), null);
    assert.equal(mod.parseIsoUtc(''), null);
    assert.equal(mod.parseIsoUtc('not-a-date'), null);
  });
});

// ── isAuditStale ─────────────────────────────────────────────────────────────

describe('isAuditStale', () => {
  const baseItem = (updatedAt) => ({ id: 'SA-1', title: 'Item', updatedAt });
  const auditAt = (auditedAt) => ({ audit: { auditedAt, readyToClose: true } });

  test('returns false when no audit exists', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.isAuditStale(baseItem('2026-09-04T10:00:00Z'), { audit: null }), false);
    assert.equal(mod.isAuditStale(baseItem('2026-09-04T10:00:00Z'), null), false);
  });

  test('returns false when the audit is newer than the item (fresh)', async () => {
    const mod = await import(MODULE_PATH);
    // audit 10:00, item updated 08:00 (2h earlier) → fresh
    assert.equal(
      mod.isAuditStale(baseItem('2026-09-04T08:00:00Z'), auditAt('2026-09-04T10:00:00Z')),
      false,
    );
  });

  test('returns true when the item was updated well after the audit (stale)', async () => {
    const mod = await import(MODULE_PATH);
    // audit 08:00, item updated 12:00 (4h later) → stale
    assert.equal(
      mod.isAuditStale(baseItem('2026-09-04T12:00:00Z'), auditAt('2026-09-04T08:00:00Z')),
      true,
    );
  });

  test('treats a persistence write shortly after the audit as fresh', async () => {
    const mod = await import(MODULE_PATH);
    // audit 10:00:00, item updated 10:00:05 (5s later, within 30s tolerance)
    assert.equal(
      mod.isAuditStale(baseItem('2026-09-04T10:00:05Z'), auditAt('2026-09-04T10:00:00Z')),
      false,
    );
  });

  test('item update beyond the tolerance window is stale', async () => {
    const mod = await import(MODULE_PATH);
    // audit 10:00:00, item updated 10:01:00 (60s later, > 30s tolerance and
    // within the 60s freshness buffer boundary → stale)
    assert.equal(
      mod.isAuditStale(baseItem('2026-09-04T10:01:00Z'), auditAt('2026-09-04T10:00:00Z')),
      true,
    );
  });

  test('returns false when timestamps are missing/unparseable (fail open)', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.isAuditStale(baseItem(null), auditAt('2026-09-04T10:00:00Z')), false);
    assert.equal(mod.isAuditStale(baseItem('2026-09-04T10:00:00Z'), { audit: {} }), false);
  });
});

// ── classifyAudit ────────────────────────────────────────────────────────────

describe('classifyAudit', () => {
  const NEW_ITEM = { id: 'SA-1', title: 'Item', updatedAt: '2026-09-04T10:00:00Z' };
  const OLD_ITEM = { id: 'SA-1', title: 'Item', updatedAt: '2026-09-05T10:00:00Z' };

  test('classifies a null audit as missing', async () => {
    const mod = await import(MODULE_PATH);
    assert.equal(mod.classifyAudit(NEW_ITEM, { audit: null }).kind, 'missing');
  });

  test('classifies a timeout audit as transient', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      audit: {
        readyToClose: false,
        auditedAt: '2026-09-04T10:00:00Z',
        rawOutput: 'Ready to close: No\n\nPhase 2 deep analysis timed out — manual review required.',
      },
    };
    assert.equal(mod.classifyAudit(NEW_ITEM, auditData).kind, 'transient');
  });

  test('classifies an outdated passing audit as stale', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      audit: { readyToClose: true, auditedAt: '2026-09-04T08:00:00Z', summary: 'ok' },
    };
    assert.equal(mod.classifyAudit(OLD_ITEM, auditData).kind, 'stale');
  });

  test('classifies a fresh not-ready audit as failing', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      audit: {
        readyToClose: false,
        auditedAt: '2026-09-04T09:59:55Z', // within the persistence-write window
        rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria not met.',
      },
    };
    assert.equal(mod.classifyAudit(NEW_ITEM, auditData).kind, 'failing');
  });

  test('classifies a fresh ready audit as passing', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      audit: { readyToClose: true, auditedAt: '2026-09-04T09:59:55Z', summary: 'all good' },
    };
    assert.equal(mod.classifyAudit(NEW_ITEM, auditData).kind, 'passing');
  });

  test('stale takes precedence over a not-ready verdict', async () => {
    const mod = await import(MODULE_PATH);
    const auditData = {
      audit: {
        readyToClose: false,
        auditedAt: '2026-09-04T08:00:00Z',
        rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria not met.',
      },
    };
    // OLD_ITEM (updated 2026-09-05) makes the audit stale; the stale verdict is
    // not trustworthy and must be remediated, not blocked immediately.
    assert.equal(mod.classifyAudit(OLD_ITEM, auditData).kind, 'stale');
  });
});

// ── getInReviewItems ─────────────────────────────────────────────────────────

/**
 * Create a fake `wl` executable and run `fn` with it on PATH.
 *
 * @param {object} opts
 * @param {Array<object>|null} opts.workItems - Payload items; null → failure.
 * @param {(result?: object) => void} fn - Callback invoked with the mocked PATH.
 */
function withWlMock({ workItems, fail = false }, fn) {
  const binDir = mkdtempSync(join(tmpdir(), 'fakebin-final-'));
  const payloadPath = join(binDir, 'payload.json');
  if (!fail) {
    writeFileSync(payloadPath, JSON.stringify({ success: true, workItems }), 'utf-8');
  }
  const wlPath = join(binDir, 'wl');
  const script = `#!/usr/bin/env bash\n`
    + (fail ? 'exit 1\n' : `cat "${payloadPath}"\n`);
  writeFileSync(wlPath, script, { mode: 0o755 });

  const savedPath = process.env.PATH;
  process.env.PATH = `${binDir}:${savedPath}`;
  try {
    return fn();
  } finally {
    process.env.PATH = savedPath;
    rmSync(binDir, { recursive: true, force: true });
  }
}

describe('getInReviewItems', () => {
  test('projects id/title/needsProducerReview/parentId/updatedAt for all items', async () => {
    const mod = await import(MODULE_PATH);
    const workItems = [
      {
        id: 'SA-TOP-1',
        title: 'Top',
        needsProducerReview: false,
        parentId: null,
        updatedAt: '2026-09-04T10:00:00Z',
      },
      {
        id: 'SA-CHILD-1',
        title: 'Child',
        needsProducerReview: true,
        parentId: 'SA-TOP-1',
        updatedAt: '2026-09-04T11:00:00Z',
      },
    ];
    const items = withWlMock({ workItems }, () => mod.getInReviewItems());

    assert.equal(items.length, 2, 'children must NOT be filtered out');
    assert.deepEqual(items[0], {
      id: 'SA-TOP-1',
      title: 'Top',
      needsProducerReview: false,
      parentId: null,
      updatedAt: '2026-09-04T10:00:00Z',
    });
    assert.equal(items[1].id, 'SA-CHILD-1');
    assert.equal(items[1].parentId, 'SA-TOP-1');
  });

  test('returns [] and warns when the wl query fails', async () => {
    const mod = await import(MODULE_PATH);
    const items = withWlMock({ workItems: [], fail: true }, () => mod.getInReviewItems());
    assert.deepEqual(items, []);
  });
});

// ── checkFinalValidation ─────────────────────────────────────────────────────

const ITEM = {
  id: 'SA-1',
  title: 'Item One',
  needsProducerReview: false,
  parentId: null,
  updatedAt: '2026-09-04T10:00:00Z',
};
const CHILD_ITEM = {
  id: 'SA-CHILD-1',
  title: 'Child One',
  needsProducerReview: false,
  parentId: 'SA-1',
  updatedAt: '2026-09-04T10:00:00Z',
};

const AUDIT_PASSING = {
  success: true,
  workItemId: 'SA-1',
  audit: { readyToClose: true, auditedAt: '2026-09-04T09:59:55Z', summary: 'all good' },
};
const AUDIT_PASSING_LATE = {
  success: true,
  workItemId: 'SA-1',
  audit: { readyToClose: true, auditedAt: '2026-09-04T08:00:00Z', summary: 'all good' },
};
const AUDIT_MISSING = { success: true, workItemId: 'SA-1', audit: null };
const AUDIT_STALE = {
  success: true,
  workItemId: 'SA-1',
  audit: { readyToClose: true, auditedAt: '2026-09-04T08:00:00Z', summary: 'ok' },
};
const AUDIT_FAILING = {
  success: true,
  workItemId: 'SA-1',
  audit: {
    readyToClose: false,
    auditedAt: '2026-09-04T09:59:55Z',
    rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria not met.',
  },
};
const AUDIT_TRANSIENT = {
  success: true,
  workItemId: 'SA-1',
  audit: {
    readyToClose: false,
    auditedAt: '2026-09-04T09:59:55Z',
    rawOutput: 'Ready to close: No\n\nPhase 2 deep analysis timed out.',
  },
};

/** Return each canned result in sequence, repeating the last. */
function sequence(results) {
  let i = 0;
  return () => JSON.stringify(results[Math.min(i++, results.length - 1)]);
}

/**
 * Run checkFinalValidation with fully injected boundaries.
 *
 * @param {object} opts
 * @param {Array<object>} [opts.items] - Items returned by getItemsFn.
 * @param {Array<object>} [opts.auditShowResults] - Canned audit-show payloads.
 * @param {Function} [opts.runAuditCommand] - Remediation runner (records by default).
 */
async function runGate({ items = [ITEM], auditShowResults, runAuditCommand } = {}) {
  const mod = await import(MODULE_PATH);
  const invocations = [];
  const runner = runAuditCommand || ((path, id) => { invocations.push([path, id]); return 'ok'; });
  const report = await mod.checkFinalValidation({
    getItemsFn: () => items,
    runAuditShow: sequence(auditShowResults),
    runAuditCommand: runner,
    resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
  });
  return { report, invocations };
}

describe('checkFinalValidation - passing and empty sets', () => {
  test('passes with no in_review items', async () => {
    const { report, invocations } = await runGate({ items: [] });
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.blockingItems.length, 0);
    assert.match(report.message, /No in_review work items found/);
    assert.equal(invocations.length, 0);
  });

  test('passes when every audit is fresh and ready', async () => {
    const { report, invocations } = await runGate({ auditShowResults: [AUDIT_PASSING] });
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.passingCount, 1);
    assert.equal(invocations.length, 0, 'a passing audit is never re-run');
  });
});

describe('checkFinalValidation - missing audit remediation', () => {
  test('missing audit re-run passes → unblocked', async () => {
    const { report, invocations } = await runGate({
      auditShowResults: [AUDIT_MISSING, AUDIT_PASSING],
    });
    assert.equal(invocations.length, 1, 'exactly one remediation re-run');
    assert.deepEqual(invocations[0], ['/tmp/fake-audit_runner.py', 'SA-1']);
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.remediatedItems.length, 1);
    assert.equal(report.remediatedItems[0].workItemId, 'SA-1');
  });

  test('missing audit re-run still missing → blocking', async () => {
    const { report, invocations } = await runGate({
      auditShowResults: [AUDIT_MISSING, AUDIT_MISSING],
    });
    assert.equal(invocations.length, 1);
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
    assert.equal(report.blockingItems[0].workItemId, 'SA-1');
    assert.ok(report.blockingItems[0].remediation.includes('audit_runner.py'));
  });
});

describe('checkFinalValidation - stale audit remediation', () => {
  test('stale audit re-run passes → unblocked', async () => {
    const { report, invocations } = await runGate({
      auditShowResults: [AUDIT_STALE, AUDIT_PASSING],
    });
    assert.equal(invocations.length, 1, 'stale audits are re-run');
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.remediatedItems.length, 1);
  });

  test('stale audit re-run still failing → blocking', async () => {
    const { report } = await runGate({
      auditShowResults: [AUDIT_STALE, AUDIT_FAILING],
    });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
  });
});

describe('checkFinalValidation - transient audit remediation', () => {
  test('transient audit re-run passes → unblocked', async () => {
    const { report, invocations } = await runGate({
      auditShowResults: [AUDIT_TRANSIENT, AUDIT_PASSING],
    });
    assert.equal(invocations.length, 1);
    assert.equal(report.hasBlockingItems, false);
    assert.equal(report.remediatedItems.length, 1);
  });
});

describe('checkFinalValidation - genuine failing audit', () => {
  test('fresh not-ready verdict blocks immediately with no re-run', async () => {
    const { report, invocations } = await runGate({ auditShowResults: [AUDIT_FAILING] });
    assert.equal(invocations.length, 0, 'genuine verdicts are never re-run');
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
    assert.match(report.blockingItems[0].reason, /not ready to close/i);
  });
});

describe('checkFinalValidation - producer-review flag', () => {
  test('needsProducerReview === true blocks with remediation guidance', async () => {
    const flagged = { ...ITEM, needsProducerReview: true };
    const { report } = await runGate({ items: [flagged], auditShowResults: [AUDIT_PASSING] });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
    assert.match(report.blockingItems[0].reason, /producer review/i);
    assert.ok(report.blockingItems[0].remediation.includes('wl update SA-1'));
  });

  test('needsProducerReview === false does not block', async () => {
    const { report } = await runGate({ auditShowResults: [AUDIT_PASSING] });
    assert.equal(report.hasBlockingItems, false);
  });
});

describe('checkFinalValidation - children are in scope', () => {
  test('a child with a failing audit blocks the release', async () => {
    const { report } = await runGate({
      items: [CHILD_ITEM],
      auditShowResults: [AUDIT_FAILING],
    });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems[0].workItemId, 'SA-CHILD-1');
  });

  test('a child flagged for producer review blocks', async () => {
    const flaggedChild = { ...CHILD_ITEM, needsProducerReview: true };
    const { report } = await runGate({
      items: [flaggedChild],
      auditShowResults: [AUDIT_PASSING],
    });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems[0].workItemId, 'SA-CHILD-1');
  });
});

describe('checkFinalValidation - audit query failure', () => {
  test('a failed audit-show query blocks', async () => {
    const failingShow = () => {
      const err = new Error('wl audit-show failed');
      err.stderr = Buffer.from('boom');
      throw err;
    };
    const mod = await import(MODULE_PATH);
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [ITEM],
      runAuditShow: failingShow,
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });
    assert.equal(report.hasBlockingItems, true);
    assert.match(report.blockingItems[0].reason, /Failed to query audit/);
  });
});

describe('checkFinalValidation - remediation runner failure', () => {
  test('a throwing remediation runner blocks with the manual command surfaced', async () => {
    const { report } = await runGate({
      auditShowResults: [AUDIT_MISSING],
      runAuditCommand: () => {
        const err = new Error('runner exited 2');
        err.stderr = Buffer.from('runner boom');
        throw err;
      },
    });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems.length, 1);
    assert.match(report.blockingItems[0].reason, /Audit remediation failed/);
    assert.ok(report.blockingItems[0].remediation.includes('audit_runner.py issue SA-1'));
  });
});

describe('checkFinalValidation - never mutates the worklog directly', () => {
  test('remediation goes through the audit runner only, never wl update', async () => {
    const mod = await import(MODULE_PATH);
    const runnerCalls = [];
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [ITEM],
      runAuditShow: sequence([AUDIT_MISSING, AUDIT_PASSING]),
      runAuditCommand: (path, id) => { runnerCalls.push([path, id]); return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });
    assert.deepEqual(runnerCalls, [['/tmp/fake-audit_runner.py', 'SA-1']]);
    assert.equal(report.remediatedItems.length, 1);
  });
});

// ── run-release.js wiring ────────────────────────────────────────────────────

describe('run-release: final-validation gating step', () => {
  test('imports checkFinalValidation from check-final-validation.js', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    assert.ok(
      content.includes("from './check-final-validation.js'") &&
        content.includes('checkFinalValidation'),
      'run-release.js should import checkFinalValidation',
    );
  });

  test('has a Step 3.7 final-validation gating step', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    assert.ok(
      content.includes('Step 3.7'),
      'run-release.js should contain a Step 3.7 gating step',
    );
  });

  test('uses exit code 12 for final-validation gate failure', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    assert.ok(
      content.includes('return finish(12)') || content.includes('exit code 12'),
      'run-release.js should use exit code 12 for the final-validation gate',
    );
  });

  test('the Step 3.7 gate is guarded by !skipChecks', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    const stepStart = content.indexOf('Step 3.7: Final validation sweep');
    assert.ok(stepStart >= 0, 'Should find the Step 3.7 comment');
    const beforeStep = content.slice(0, stepStart);
    const lastGuard = beforeStep.lastIndexOf('!skipChecks');
    assert.ok(lastGuard >= 0, 'Step 3.7 should be guarded by !skipChecks');
  });

  test('runRelease JSDoc lists the final-validation gating step', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    const docMatch = content.match(/\/\*\*[\s\S]*?runRelease[\s\S]*?\*\//);
    assert.ok(docMatch, 'Should find runRelease JSDoc');
    assert.ok(
      docMatch[0].includes('3.7') && /final validation/i.test(docMatch[0]),
      'runRelease JSDoc should document the Step 3.7 final validation gate',
    );
  });
});

// ── Documentation ────────────────────────────────────────────────────────────

describe('ship skill documentation', () => {
  test('SKILL.md documents the final-validation gate', () => {
    const content = readFileSync(SKILL_MD, 'utf-8');
    assert.ok(/final.?validation/i.test(content), 'SKILL.md should mention the final-validation gate');
  });

  test('SKILL.md documents exit code 12', () => {
    const content = readFileSync(SKILL_MD, 'utf-8');
    assert.ok(
      content.includes('| 12 |'),
      'SKILL.md exit-code table should document exit code 12',
    );
  });

  test('reference doc lists check-final-validation.js', () => {
    const content = readFileSync(REFERENCE_MD, 'utf-8');
    assert.ok(
      content.includes('check-final-validation.js'),
      'reference doc should list the new script',
    );
  });

  test('reference doc documents exit code 12', () => {
    const content = readFileSync(REFERENCE_MD, 'utf-8');
    assert.ok(
      content.includes('12'),
      'reference doc should reference exit code 12',
    );
  });
});
