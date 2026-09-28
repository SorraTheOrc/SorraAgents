/**
 * Unit tests for the ship-skill child producer-review override/escalation
 * contract (SA-0MUJLWPB10038Z8Q).
 *
 * The release close step (`closeWorkItemsAfterRelease`) and the pre-ship
 * final-validation sweep (`checkFinalValidation`) must resolve a child's
 * `needsProducerReview` flag against its parent's readiness:
 *
 *   - AC1: a child flagged `needsProducerReview=true` under an audit-ready
 *     `in_review` parent is closed after its flag is cleared (with an
 *     explanatory comment naming the authorising parent).
 *   - AC2: an uncovered child that genuinely needs attention escalates the
 *     flag to its nearest `in_review` ancestor (with a comment enumerating
 *     the child id); the parent then blocks the gate.
 *   - AC3: clearing a child flag is refused unless the parent is `in_review`
 *     and its audit is ready to close.
 *   - AC4: `--dry-run` performs no mutations and reports the planned
 *     overrides/escalations.
 *
 * Every command boundary is injected (no live `wl`/`audit_runner` calls, no
 * live worklog mutation).
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const FINAL_VALIDATION_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-final-validation.js');
const SKILL_MD = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const REFERENCE_MD = join(REPO_ROOT, 'docs', 'dev', 'ship-skill-reference.md');

const PARENT_IN_REVIEW = {
  id: 'SA-P1',
  title: 'Parent One',
  stage: 'in_review',
  parentId: null,
  updatedAt: '2026-09-04T09:00:00Z',
};

const AUDIT_READY = { success: true, audit: { readyToClose: true, auditedAt: '2026-09-04T09:30:00Z', summary: 'ok' } };
const AUDIT_NOT_READY = { success: true, audit: { readyToClose: false, auditedAt: '2026-09-04T09:30:00Z', rawOutput: 'not ready' } };
const AUDIT_MISSING = { success: true, audit: null };

// ── findInReviewAncestor ─────────────────────────────────────────────────────

describe('findInReviewAncestor', () => {
  test('returns the nearest in_review ancestor', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const item = { id: 'SA-C1', parentId: 'SA-P1' };
    const ancestors = {
      'SA-P1': { id: 'SA-P1', stage: 'in_review', parentId: null },
    };
    assert.equal(
      mod.findInReviewAncestor(item, (id) => ancestors[id] ?? null),
      'SA-P1',
    );
  });

  test('skips non-in_review ancestors and walks upward', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const item = { id: 'SA-C1', parentId: 'SA-MID' };
    const ancestors = {
      'SA-MID': { id: 'SA-MID', stage: 'in_progress', parentId: 'SA-P1' },
      'SA-P1': { id: 'SA-P1', stage: 'in_review', parentId: null },
    };
    assert.equal(
      mod.findInReviewAncestor(item, (id) => ancestors[id] ?? null),
      'SA-P1',
    );
  });

  test('returns null when no in_review ancestor exists', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const item = { id: 'SA-C1', parentId: 'SA-P1' };
    assert.equal(
      mod.findInReviewAncestor(item, () => ({ id: 'SA-P1', stage: 'open', parentId: null })),
      null,
    );
  });

  test('breaks cycles conservatively (returns null, no infinite loop)', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const item = { id: 'SA-C1', parentId: 'SA-P2' };
    const ancestors = {
      'SA-P2': { id: 'SA-P2', stage: 'open', parentId: 'SA-C1' },
    };
    assert.equal(
      mod.findInReviewAncestor(item, (id) => ancestors[id] ?? null),
      null,
    );
  });
});

// ── resolveCandidateCoverage ─────────────────────────────────────────────────

describe('resolveCandidateCoverage', () => {
  test('reports covered when the nearest in_review ancestor audit is ready', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const item = { id: 'SA-C1', title: 'Child', parentId: 'SA-P1' };
    const coverage = mod.resolveCandidateCoverage(item, {
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: () => JSON.stringify(AUDIT_READY),
    });
    assert.equal(coverage.outcome, 'covered');
    assert.equal(coverage.ancestorId, 'SA-P1');
  });

  test('reports uncovered when the in_review ancestor audit is not ready', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const item = { id: 'SA-C1', title: 'Child', parentId: 'SA-P1' };
    const coverage = mod.resolveCandidateCoverage(item, {
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: () => JSON.stringify(AUDIT_NOT_READY),
    });
    assert.notEqual(coverage.outcome, 'covered');
  });
});

// ── Final-validation escalation (AC2, AC4) ───────────────────────────────────

/** Item payloads for the final-validation gate. */
function escalationItems() {
  return [
    {
      id: 'SA-P1',
      title: 'Parent One',
      needsProducerReview: false,
      parentId: null,
      updatedAt: '2026-09-04T09:00:00Z',
    },
    {
      id: 'SA-C1',
      title: 'Child One',
      needsProducerReview: true,
      parentId: 'SA-P1',
      updatedAt: '2026-09-04T10:00:00Z',
    },
  ];
}

describe('checkFinalValidation - uncovered child escalation', () => {
  test('AC2: escalates needsProducerReview to the nearest in_review ancestor with a comment naming the child', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const mutations = [];
    const auditByItem = {
      'SA-P1': AUDIT_NOT_READY, // parent in_review but not passing → child uncovered
      'SA-C1': AUDIT_READY,
    };
    const report = await mod.checkFinalValidation({
      getItemsFn: () => escalationItems(),
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: (id) => JSON.stringify(auditByItem[id] ?? AUDIT_MISSING),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: (itemId, args) => {
        mutations.push({ itemId, args });
      },
    });

    // The parent must have been flagged for producer review via `wl update`.
    const updateCall = mutations.find((m) => m.args.includes('update'));
    assert.ok(updateCall, 'expected a wl update call to escalate to the parent');
    assert.equal(updateCall.itemId, 'SA-P1');
    assert.ok(
      updateCall.args.includes('--needs-producer-review') &&
        updateCall.args.includes('true'),
      'escalation must set --needs-producer-review true on the parent',
    );
    const commentArg = updateCall.args[updateCall.args.indexOf('--comment') + 1] || '';
    assert.ok(
      commentArg.includes('SA-C1'),
      'the escalation comment must enumerate the child id (SA-C1)',
    );

    // The escalation is reported and the gate blocks.
    assert.equal(report.escalatedItems.length, 1);
    assert.equal(report.escalatedItems[0].workItemId, 'SA-C1');
    assert.equal(report.escalatedItems[0].ancestorId, 'SA-P1');
    assert.equal(report.escalatedItems[0].planned, false);
    assert.equal(report.hasBlockingItems, true);
  });

  test('AC2: multiple uncovered children escalate to one ancestor and the block names them all', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [
        { id: 'SA-C1', title: 'Child One', needsProducerReview: true, parentId: 'SA-P1', updatedAt: '2026-09-04T10:00:00Z' },
        { id: 'SA-C2', title: 'Child Two', needsProducerReview: true, parentId: 'SA-P1', updatedAt: '2026-09-04T10:00:00Z' },
      ],
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: (id) => JSON.stringify(id === 'SA-P1' ? AUDIT_NOT_READY : AUDIT_READY),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: () => {},
    });

    assert.equal(report.escalatedItems.length, 2);
    const parentBlock = report.blockingItems.find((b) => b.workItemId === 'SA-P1');
    assert.ok(parentBlock, 'the shared ancestor must be blocking once');
    assert.match(parentBlock.reason, /SA-C1/);
    assert.match(parentBlock.reason, /SA-C2/);
  });

  test('escalation adds the ancestor to the blocking set when it is not otherwise blocking', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const mutations = [];
    // Only the child is returned by the item query — the ancestor is not
    // evaluated independently, so the escalation must surface it as blocking.
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [
        {
          id: 'SA-C1',
          title: 'Child One',
          needsProducerReview: true,
          parentId: 'SA-P1',
          updatedAt: '2026-09-04T10:00:00Z',
        },
      ],
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: (id) => JSON.stringify(id === 'SA-P1' ? AUDIT_NOT_READY : AUDIT_READY),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: (itemId, args) => { mutations.push({ itemId, args }); },
    });

    assert.equal(report.hasBlockingItems, true);
    const blockingIds = report.blockingItems.map((b) => b.workItemId);
    assert.ok(blockingIds.includes('SA-P1'), 'the escalated ancestor must be blocking');
    assert.equal(mutations.length, 1, 'exactly one escalation mutation');
  });

  test('AC4: dry-run reports the planned escalation but performs no mutation', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const mutations = [];
    const report = await mod.checkFinalValidation({
      dryRun: true,
      getItemsFn: () => escalationItems(),
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: (id) => JSON.stringify(id === 'SA-P1' ? AUDIT_NOT_READY : AUDIT_READY),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: (itemId, args) => { mutations.push({ itemId, args }); },
    });

    assert.equal(mutations.length, 0, 'dry-run must not mutate the worklog');
    assert.equal(report.escalatedItems.length, 1);
    assert.equal(report.escalatedItems[0].planned, true);
    assert.match(report.message, /dry-run/i);
    assert.match(report.message, /SA-C1/);
  });

  test('a top-level item flagged for producer review still blocks directly', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [
        {
          id: 'SA-TOP1',
          title: 'Top Level',
          needsProducerReview: true,
          parentId: null,
          updatedAt: '2026-09-04T10:00:00Z',
        },
      ],
      getItemByIdFn: () => null,
      runAuditShow: () => JSON.stringify(AUDIT_READY),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: () => {},
    });
    assert.equal(report.hasBlockingItems, true);
    assert.equal(report.blockingItems[0].workItemId, 'SA-TOP1');
    assert.equal(report.escalatedItems.length, 0);
  });
});

describe('checkFinalValidation - covered child planned override (AC1 reporting)', () => {
  test('a covered child flagged for producer review is reported as a planned override', async () => {
    const mod = await import(FINAL_VALIDATION_PATH);
    const report = await mod.checkFinalValidation({
      getItemsFn: () => [
        {
          id: 'SA-C1',
          title: 'Child One',
          needsProducerReview: true,
          parentId: 'SA-P1',
          updatedAt: '2026-09-04T10:00:00Z',
        },
      ],
      getItemByIdFn: (id) => (id === 'SA-P1' ? PARENT_IN_REVIEW : null),
      runAuditShow: (id) => JSON.stringify(id === 'SA-P1' ? AUDIT_READY : AUDIT_NOT_READY),
      runAuditCommand: () => 'ok',
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      runCloseCommand: () => {},
    });

    assert.equal(report.coveredChildren.length, 1);
    assert.equal(report.plannedOverrides.length, 1);
    assert.equal(report.plannedOverrides[0].workItemId, 'SA-C1');
    assert.equal(report.plannedOverrides[0].ancestorId, 'SA-P1');
    // Escalation is only for uncovered children.
    assert.equal(report.escalatedItems.length, 0);
    assert.equal(report.hasBlockingItems, false);
  });
});

// ── Close-step override (AC1, AC3, AC4) ──────────────────────────────────────

describe('closeWorkItemsAfterRelease - child override', () => {
  test('AC1: closes a flagged child after clearing its flag, with an explanatory comment', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const overrides = [];
    const closes = [];
    const result = mod.closeWorkItemsAfterRelease('1.2.3', {
      getCandidateItemsFn: () => [
        { id: 'SA-P1', title: 'Parent One', needsProducerReview: false, parentId: null },
        { id: 'SA-C1', title: 'Child One', needsProducerReview: true, parentId: 'SA-P1' },
      ],
      getDescendantsFn: () => [],
      getAncestorAuditFn: () => ({ outcome: 'covered', ancestorId: 'SA-P1' }),
      runOverrideCommand: (childId, ancestorId, reason) => {
        overrides.push({ childId, ancestorId, reason });
      },
      runCloseCommand: (itemId) => { closes.push(itemId); },
    });

    assert.deepEqual(
      overrides.map((o) => o.childId),
      ['SA-C1'],
      'the flagged covered child must be overridden',
    );
    assert.equal(overrides[0].ancestorId, 'SA-P1');
    assert.match(
      overrides[0].reason,
      /SA-P1/,
      'the override comment must name the authorising ancestor',
    );
    assert.ok(closes.includes('SA-P1'), 'parent must be closed');
    assert.ok(closes.includes('SA-C1'), 'overridden child must be closed');
    assert.equal(result.overriddenCount, 1);
    assert.equal(result.overriddenItems[0].id, 'SA-C1');
    assert.equal(result.skippedCount, 0);
  });

  test('AC3: refuses to clear a child flag when the ancestor is not covered', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const overrides = [];
    const closes = [];
    const result = mod.closeWorkItemsAfterRelease('1.2.3', {
      getCandidateItemsFn: () => [
        { id: 'SA-P1', title: 'Parent One', needsProducerReview: false, parentId: null },
        { id: 'SA-C1', title: 'Child One', needsProducerReview: true, parentId: 'SA-P1' },
      ],
      getDescendantsFn: () => [],
      getAncestorAuditFn: () => ({ outcome: 'uncovered', ancestorId: 'SA-P1' }),
      runOverrideCommand: (childId) => { overrides.push(childId); },
      runCloseCommand: (itemId) => { closes.push(itemId); },
    });

    assert.equal(overrides.length, 0, 'no override may be performed without a passing parent audit');
    assert.ok(!closes.includes('SA-C1'), 'the uncovered flagged child must not be closed');
    assert.equal(result.overriddenCount, 0);
    assert.equal(result.skippedCount, 1);
    assert.equal(result.skippedItems[0].id, 'SA-C1');
  });

  test('AC3: a lookup failure on the ancestor audit is treated as not-covered (never overrides)', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const overrides = [];
    const result = mod.closeWorkItemsAfterRelease('1.2.3', {
      getCandidateItemsFn: () => [
        { id: 'SA-C1', title: 'Child One', needsProducerReview: true, parentId: 'SA-P1' },
      ],
      getDescendantsFn: () => [],
      getAncestorAuditFn: () => { throw new Error('wl show failed'); },
      runOverrideCommand: (childId) => { overrides.push(childId); },
      runCloseCommand: () => {},
    });

    assert.equal(overrides.length, 0);
    assert.equal(result.overriddenCount, 0);
    assert.equal(result.skippedCount, 1);
  });

  test('AC4: dry-run reports the planned override without mutating the worklog', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const overrides = [];
    const closes = [];
    const result = mod.closeWorkItemsAfterRelease('1.2.3', {
      dryRun: true,
      getCandidateItemsFn: () => [
        { id: 'SA-P1', title: 'Parent One', needsProducerReview: false, parentId: null },
        { id: 'SA-C1', title: 'Child One', needsProducerReview: true, parentId: 'SA-P1' },
      ],
      getDescendantsFn: () => [],
      getAncestorAuditFn: () => ({ outcome: 'covered', ancestorId: 'SA-P1' }),
      runOverrideCommand: (childId) => { overrides.push(childId); },
      runCloseCommand: (itemId) => { closes.push(itemId); },
    });

    assert.equal(overrides.length, 0, 'dry-run must not clear any flag');
    assert.equal(closes.length, 0, 'dry-run must not close any item');
    assert.equal(result.dryRun, true);
    assert.equal(result.overriddenCount, 1, 'the planned override is still reported');
    assert.equal(result.closedCount, 0);
  });

  test('a covered child with a null/undefined flag is not overridden (only true is)', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const overrides = [];
    const result = mod.closeWorkItemsAfterRelease('1.2.3', {
      getCandidateItemsFn: () => [
        { id: 'SA-C1', title: 'Child One', needsProducerReview: null, parentId: 'SA-P1' },
      ],
      getDescendantsFn: () => [],
      getAncestorAuditFn: () => ({ outcome: 'covered', ancestorId: 'SA-P1' }),
      runOverrideCommand: (childId) => { overrides.push(childId); },
      runCloseCommand: () => {},
    });

    assert.equal(overrides.length, 0);
    assert.equal(result.overriddenCount, 0);
    assert.equal(result.skippedCount, 1);
  });
});

// ── Documentation (AC6) ──────────────────────────────────────────────────────

describe('ship-skill documentation', () => {
  test('AC6: SKILL.md documents the child override/escalation contract', () => {
    assert.ok(existsSync(SKILL_MD));
    const content = readFileSync(SKILL_MD, 'utf-8');
    assert.match(content, /override/i, 'SKILL.md must mention the override');
    assert.match(content, /escalat/i, 'SKILL.md must mention the escalation');
  });

  test('AC6: reference doc documents the child override/escalation contract', () => {
    assert.ok(existsSync(REFERENCE_MD));
    const content = readFileSync(REFERENCE_MD, 'utf-8');
    assert.match(content, /override/i, 'reference doc must mention the override');
    assert.match(content, /escalat/i, 'reference doc must mention the escalation');
  });

  test('AC6: reference doc lists the new injectable boundaries', () => {
    const content = readFileSync(REFERENCE_MD, 'utf-8');
    assert.ok(
      content.includes('getAncestorAuditFn') || content.includes('resolveCandidateCoverage'),
      'reference doc must document the ancestor-audit boundary',
    );
  });
});
