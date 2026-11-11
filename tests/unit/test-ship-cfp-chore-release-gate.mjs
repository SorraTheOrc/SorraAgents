/**
 * Release-gate regression for audit-generated candidate-false-positive chores.
 *
 * SA-0MVZNECSD005XB42 AC4 — a repository holding N audit-generated
 * "candidate false positive — producer decision required" tracking chores
 * must not fail the ship producer-review / final-validation gate. The audit
 * runner no longer arms `needsProducerReview` for such chores (they are
 * bookkeeping for a finding already reported on the audited item) and no
 * longer re-flags an item whose only findings are tracked/accepted medium/low
 * false positives, so the gate sees `needsProducerReview === false` for them.
 *
 * The gate is state-based: it reads the stored flag from `wl`. These tests
 * therefore assert it passes over unflagged tracking chores and still blocks
 * over a genuinely flagged item (the fix must never relax the gate for real
 * producer decisions).
 *
 * All command boundaries are injected, so the suite is hermetic.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-final-validation.js');

const FP_ANNOTATION = 'candidate false positive — producer decision required';

/** An audit-generated tracking chore as `wl list --stage in_review` reports it. */
function fpChore(n, overrides = {}) {
  return {
    id: `SA-CHORE-${n}`,
    title: `ruff F841 in src/mod${n}.py:${n} — ${FP_ANNOTATION}`,
    description: `False-positive candidate (medium/low severity).\nCFP-Chore-Key: ruff:F841:src/mod${n}.py:${n}`,
    issueType: 'chore',
    needsProducerReview: false,
    parentId: null,
    updatedAt: '2026-09-04T10:00:00Z',
    ...overrides,
  };
}

function runningAudit(item) {
  return {
    success: true,
    workItemId: item.id,
    audit: {
      readyToClose: true,
      auditedAt: '2026-09-04T09:59:55Z',
      summary: 'all good',
    },
  };
}

async function runGate(items) {
  const mod = await import(MODULE_PATH);
  const byId = new Map(items.map((item) => [item.id, item]));
  return mod.checkFinalValidation({
    getItemsFn: () => items,
    getItemByIdFn: (id) => byId.get(id) ?? null,
    runAuditShow: (id) => JSON.stringify(runningAudit(byId.get(id) ?? { id })),
    runAuditCommand: () => 'ok',
    resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
  });
}

describe('checkFinalValidation - candidate false-positive chores', () => {
  test('N unflagged tracking chores do not fail the gate', async () => {
    const chores = [1, 2, 3, 4, 5].map((n) => fpChore(n));
    const report = await runGate(chores);
    assert.equal(report.hasBlockingItems, false, report.message);
    assert.equal(report.blockingItems.length, 0);
    assert.equal(report.passingCount, chores.length);
  });

  test('tracking chores mixed with normal in_review items still pass', async () => {
    const items = [
      fpChore(1),
      {
        id: 'SA-2',
        title: 'Ordinary reviewed item',
        needsProducerReview: false,
        parentId: null,
        updatedAt: '2026-09-04T10:00:00Z',
      },
    ];
    const report = await runGate(items);
    assert.equal(report.hasBlockingItems, false, report.message);
  });

  test('a genuinely flagged item still blocks (gate not relaxed)', async () => {
    const items = [fpChore(1), {
      id: 'SA-3',
      title: 'Needs a real decision',
      needsProducerReview: true,
      parentId: null,
      updatedAt: '2026-09-04T10:00:00Z',
    }];
    const report = await runGate(items);
    assert.equal(report.hasBlockingItems, true);
    const blocking = report.blockingItems.find((b) => b.workItemId === 'SA-3');
    assert.ok(blocking, 'the flagged item must block');
    assert.ok(
      blocking.reasons.some((r) => /producer review/i.test(r)),
      `expected a producer-review reason, got ${JSON.stringify(blocking.reasons)}`,
    );
  });

  test('a legacy flagged tracking chore still blocks until cleared', async () => {
    // Pre-existing chores created before this fix carry the flag; the runner
    // never clears a flag it did not set, so the producer clears them once.
    const items = [fpChore(1, { needsProducerReview: true })];
    const report = await runGate(items);
    assert.equal(report.hasBlockingItems, true);
  });
});
