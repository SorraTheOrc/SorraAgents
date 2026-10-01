/**
 * Unit tests for skill/ship/scripts/refresh-audits.js
 *
 * Covers the ship pre-flight-only audit refresh (SA-0MUOO5VMH005VF69):
 * planning/classification, bounded execution, dry-run no-op, and that the
 * pre-flight path never sets the Code Freeze marker.
 *
 * Command boundaries are injected (or `wl` is mocked) so the suite is
 * hermetic — no live worklog/audit-runner mutation.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'refresh-audits.js');
const REMEDIATION_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'audit-remediation.js');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');

const ITEM_BASE = (id, updatedAt) => ({
  id,
  title: `Item ${id}`,
  needsProducerReview: false,
  parentId: null,
  updatedAt,
});

// Audit payloads for classification.
const PASSING = { audit: { readyToClose: true, auditedAt: '2026-09-04T09:59:55Z', summary: 'ok' } };
const MISSING = { audit: null };
const STALE = { audit: { readyToClose: true, auditedAt: '2026-09-01T08:00:00Z', summary: 'old' } };
const TRANSIENT = {
  audit: {
    readyToClose: false,
    auditedAt: '2026-09-04T09:59:55Z',
    rawOutput: 'Ready to close: No\n\nDeep analysis timed out — manual review required.',
  },
};
const FAILING = {
  audit: {
    readyToClose: false,
    auditedAt: '2026-09-04T09:59:55Z',
    rawOutput: 'Ready to close: No\n\n2 of 3 acceptance criteria not met.',
  },
};

describe('refresh-audits: planAuditRefresh', () => {
  test('classifies fresh / toRefresh / failing', async () => {
    const mod = await import(MODULE_PATH);
    const items = [
      ITEM_BASE('SA-FRESH', '2026-09-04T10:00:00Z'),
      ITEM_BASE('SA-MISSING', '2026-09-04T10:00:00Z'),
      ITEM_BASE('SA-STALE', '2026-09-04T10:00:00Z'),
      ITEM_BASE('SA-TRANSIENT', '2026-09-04T10:00:00Z'),
      ITEM_BASE('SA-FAILING', '2026-09-04T10:00:00Z'),
    ];
    const audits = {
      'SA-FRESH': PASSING,
      'SA-MISSING': MISSING,
      'SA-STALE': STALE,
      'SA-TRANSIENT': TRANSIENT,
      'SA-FAILING': FAILING,
    };
    const plan = mod.planAuditRefresh({
      getItemsFn: () => items,
      runAuditShow: (id) => JSON.stringify(audits[id]),
    });

    assert.deepEqual(plan.fresh.map((i) => i.id), ['SA-FRESH']);
    assert.deepEqual(plan.failing.map((i) => i.id), ['SA-FAILING']);
    assert.deepEqual(
      plan.toRefresh.map((i) => [i.id, i.kind]).sort(),
      [['SA-MISSING', 'missing'], ['SA-STALE', 'stale'], ['SA-TRANSIENT', 'transient']],
    );
    assert.deepEqual(plan.errors, []);
  });

  test('records a query failure as an error, not a refresh', async () => {
    const mod = await import(MODULE_PATH);
    const plan = mod.planAuditRefresh({
      getItemsFn: () => [ITEM_BASE('SA-ERR', '2026-09-04T10:00:00Z')],
      runAuditShow: () => { const e = new Error('wl failed'); e.stderr = Buffer.from('boom'); throw e; },
    });
    assert.equal(plan.toRefresh.length, 0);
    assert.equal(plan.errors.length, 1);
    assert.match(plan.errors[0].reason, /boom/);
  });
});

describe('refresh-audits: refreshAudits', () => {
  test('re-audits a missing item and reports it refreshed', async () => {
    const mod = await import(MODULE_PATH);
    let runnerCalls = 0;
    let shows = 0;
    const report = await mod.refreshAudits({
      getItemsFn: () => [ITEM_BASE('SA-M', '2026-09-04T10:00:00Z')],
      runAuditShow: () => {
        shows += 1;
        return JSON.stringify(shows === 1 ? MISSING : PASSING);
      },
      runAuditCommand: () => { runnerCalls += 1; return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });
    assert.equal(runnerCalls, 1);
    assert.deepEqual(report.refreshed.map((i) => i.id), ['SA-M']);
    assert.equal(report.hasRemaining, false);
  });

  test('reports still-failing items and surfaces offline guidance', async () => {
    const mod = await import(MODULE_PATH);
    const report = await mod.refreshAudits({
      getItemsFn: () => [ITEM_BASE('SA-M', '2026-09-04T10:00:00Z')],
      runAuditShow: () => JSON.stringify(MISSING),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });
    assert.equal(report.hasRemaining, true);
    assert.deepEqual(report.stillFailing.map((i) => i.id), ['SA-M']);
    assert.match(report.message, /audit_runner\.py batch/);
  });

  test('dry-run performs no runner call and no mutation', async () => {
    const mod = await import(MODULE_PATH);
    let runnerCalls = 0;
    const report = await mod.refreshAudits({
      dryRun: true,
      getItemsFn: () => [ITEM_BASE('SA-M', '2026-09-04T10:00:00Z')],
      runAuditShow: () => JSON.stringify(MISSING),
      runAuditCommand: () => { runnerCalls += 1; return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
    });
    assert.equal(runnerCalls, 0, 'dry-run must not invoke the audit runner');
    assert.equal(report.dryRun, true);
    assert.deepEqual(report.skipped.map((i) => i.id), ['SA-M']);
    assert.match(report.message, /dry-run/i);
  });

  test('respects the remediation budget (skips beyond the cap)', async () => {
    const mod = await import(MODULE_PATH);
    const budgetMod = await import(REMEDIATION_PATH);
    let runnerCalls = 0;
    const callCounts = {};
    const items = [
      ITEM_BASE('SA-1', '2026-09-04T10:00:00Z'),
      ITEM_BASE('SA-2', '2026-09-04T10:00:00Z'),
    ];
    const report = await mod.refreshAudits({
      getItemsFn: () => items,
      runAuditShow: (id) => {
        callCounts[id] = (callCounts[id] || 0) + 1;
        // SA-1: missing on the plan pass, passing on the re-check. SA-2: missing.
        if (id === 'SA-1') {
          return JSON.stringify(callCounts[id] === 1 ? MISSING : PASSING);
        }
        return JSON.stringify(MISSING);
      },
      runAuditCommand: () => { runnerCalls += 1; return 'ok'; },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      createRemediationBudgetFn: () => new budgetMod.RemediationBudget({ maxItems: 1, budgetMs: 1_000_000 }),
    });
    assert.equal(runnerCalls, 1, 'only the funded attempt runs');
    assert.equal(report.skipped.length, 1);
    assert.equal(report.hasRemaining, true);
  });
});

describe('refresh-audits: run-release wiring', () => {
  test('run-release.js imports and invokes runRefreshAuditsAction before the freeze', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    assert.ok(
      content.includes("from './refresh-audits.js'") && content.includes('runRefreshAuditsAction'),
      'run-release.js should import runRefreshAuditsAction',
    );
    const refreshIdx = content.indexOf("cliArgs.includes('--refresh-audits')");
    const freezeIdx = content.indexOf('setCodeFreezeMarker(projectRoot)', refreshIdx);
    assert.ok(refreshIdx >= 0, 'runRelease should branch on --refresh-audits');
    assert.ok(
      freezeIdx === -1 || freezeIdx > refreshIdx,
      'the refresh branch must precede setCodeFreezeMarker',
    );
    assert.ok(
      content.includes("'--refresh-audits'"),
      '--refresh-audits must be a recognised wrapper-only flag',
    );
  });

  test('pre-flight refresh does not set the Code Freeze marker', async () => {
    const { runRelease } = await import(RUN_RELEASE_PATH);
    const cwd = mkdtempSync(join(tmpdir(), 'refresh-preflight-'));
    const savedPath = process.env.PATH;
    // Mock `wl` so getInReviewItems() returns no items (nothing to refresh).
    const binDir = mkdtempSync(join(tmpdir(), 'refresh-fakebin-'));
    const payload = join(binDir, 'payload.json');
    writeFileSync(payload, JSON.stringify({ success: true, workItems: [] }));
    writeFileSync(join(binDir, 'wl'), `#!/usr/bin/env bash\ncat "${payload}"\n`, { mode: 0o755 });
    process.env.PATH = `${binDir}:${savedPath}`;
    const savedCwd = process.cwd();
    try {
      process.chdir(cwd);
      const code = await runRelease(['--refresh-audits']);
      assert.equal(code, 0, 'empty backlog → exit 0');
      assert.equal(
        existsSync(join(cwd, '.worklog', 'code-freeze.json')),
        false,
        'pre-flight must not create the Code Freeze marker',
      );
    } finally {
      process.chdir(savedCwd);
      process.env.PATH = savedPath;
      rmSync(cwd, { recursive: true, force: true });
      rmSync(binDir, { recursive: true, force: true });
    }
  });
});
