/**
 * Unit tests for close-work-items-after-release in run-release.js
 *
 * Tests that after a successful release, work items are closed
 * with the reason "Shipped in v<version>".
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SKILL_MD_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const RELEASE_PROCESS_PATH = join(REPO_ROOT, 'docs', 'dev', 'release-process.md');

// ---------------------------------------------------------------------------
// 1. Module file exists and exports expected functions
// ---------------------------------------------------------------------------
test('close-work-items: run-release.js exists', () => {
  assert.ok(
    existsSync(RUN_RELEASE_PATH),
    'run-release.js should exist',
  );
});

test('close-work-items: run-release.js exports closeWorkItemsAfterRelease', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(
    typeof mod.closeWorkItemsAfterRelease,
    'function',
    'run-release.js should export closeWorkItemsAfterRelease function',
  );
});

test('close-work-items: run-release.js imports getCandidateItems from check-audit-gate.js', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  assert.ok(
    content.includes('getCandidateItems'),
    'run-release.js should import getCandidateItems from check-audit-gate.js',
  );
});



// ---------------------------------------------------------------------------
// 3. closeWorkItemsAfterRelease returns expected structure
// ---------------------------------------------------------------------------
// Test-isolation note (SA-0MSJ2XMQL006CVQS): these tests MUST NOT invoke
// closeWorkItemsAfterRelease with the default worklog boundary — the buggy
// versions called it with hardcoded versions ('1.0.0'/'1.2.3') against the
// LIVE worklog, spuriously closing ~368 real work items. Every call below
// injects fake candidate/close functions so the suite never mutates the
// production worklog.

test('close-work-items: closeWorkItemsAfterRelease returns expected structure', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease('1.0.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [],
    runCloseCommand: () => {},
  });

  assert.ok(typeof result === 'object');
  assert.ok('success' in result);
  assert.ok('message' in result);
  assert.equal(typeof result.success, 'boolean');
  assert.equal(typeof result.message, 'string');
});

// ---------------------------------------------------------------------------
// 4. closeWorkItemsAfterRelease accepts version string (empty candidate set)
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease accepts a version string', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  // With empty candidate set (no items to close), it should succeed gracefully.
  // Candidate query is injected so the live worklog is never touched.
  const result = mod.closeWorkItemsAfterRelease('1.2.3', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [],
    runCloseCommand: () => {},
  });

  assert.equal(
    typeof result.message,
    'string',
    'closeWorkItemsAfterRelease should return a message string',
  );
  // Empty candidate set should say "no items" or similar
  assert.ok(
    result.message.includes('No work items') ||
    result.message.includes('no candidate') ||
    result.message.includes('no items'),
    'closeWorkItemsAfterRelease should handle empty candidate set gracefully',
  );
  assert.equal(result.closedCount, 0, 'no items should be closed');
});

// ---------------------------------------------------------------------------
// 4b. closeWorkItemsAfterRelease closes candidates via the injected close fn
// ---------------------------------------------------------------------------
test('close-work-items: closes only needsProducerReview=false candidates', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const closed = [];
  const skipped = [];
  const result = mod.closeWorkItemsAfterRelease('0.2.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-CLOSE1', title: 'Close Me', needsProducerReview: false },
      { id: 'SA-CLOSE2', title: 'Close Me Too', needsProducerReview: false },
      { id: 'SA-SKIP1', title: 'Review Me', needsProducerReview: true },
      { id: 'SA-SKIP2', title: 'Unknown Review', needsProducerReview: null },
    ],
    runCloseCommand: (itemId, reason) => {
      closed.push({ itemId, reason });
    },
    getDescendantsFn: () => [],
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(
    closed.map((c) => c.itemId).sort(),
    ['SA-CLOSE1', 'SA-CLOSE2'],
    'only needsProducerReview=false items should be closed',
  );
  assert.ok(
    closed.every((c) => c.reason === 'Shipped in v0.2.0'),
    'close reason should be "Shipped in v<version>"',
  );
  assert.equal(result.closedCount, 2);
  assert.equal(result.skippedCount, 2);
  assert.deepEqual(
    result.skippedItems.map((s) => s.id).sort(),
    ['SA-SKIP1', 'SA-SKIP2'],
    'skipped items should be reported',
  );
});

// ---------------------------------------------------------------------------
// 4c. closeWorkItemsAfterRelease reports individual close failures
// ---------------------------------------------------------------------------
test('close-work-items: reports close failures without aborting the sweep', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease('0.2.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-OK', title: 'OK', needsProducerReview: false },
      { id: 'SA-FAIL', title: 'Fails', needsProducerReview: false },
    ],
    runCloseCommand: (itemId) => {
      if (itemId === 'SA-FAIL') {
        throw new Error('wl close failed');
      }
    },
    getDescendantsFn: () => [],
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.equal(result.closedCount, 1);
  assert.equal(result.errorCount, 1);
  assert.equal(result.success, false);
  assert.ok(result.message.includes('SA-FAIL'), 'message should mention the failing item');
});

// ---------------------------------------------------------------------------
// 5. closeWorkItemsAfterRelease handles missing version gracefully
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease handles missing version', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease(null, {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [{ id: 'SA-X', title: 'X', needsProducerReview: false }],
    runCloseCommand: () => { throw new Error('should not be called'); },
  });

  assert.ok(
    result.message.includes('No version'),
    'closeWorkItemsAfterRelease should report when no version is provided',
  );
  assert.equal(result.closedCount, 0, 'no items should be closed without a version');
});

// ---------------------------------------------------------------------------
// 6. SKILL.md documents the close work items step
// ---------------------------------------------------------------------------
test('close-work-items: SKILL.md documents the close work items step', () => {
  const content = readFileSync(SKILL_MD_PATH, 'utf-8');

  assert.ok(
    (content.includes('close') && content.includes('work item') &&
     (content.includes('release') || content.includes('Shipped'))) ||
    content.includes('Close work items') ||
    content.includes('items are closed') ||
    content.includes('work items are automatically closed'),
    'SKILL.md should document the close-work-items step after release',
  );
});

// ---------------------------------------------------------------------------
// 7. docs/dev/release-process.md documents auto-close
// ---------------------------------------------------------------------------
test('close-work-items: release-process.md documents auto-close after release', () => {
  assert.ok(
    existsSync(RELEASE_PROCESS_PATH),
    'docs/dev/release-process.md should exist',
  );

  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');

  assert.ok(
    content.includes('close') ||
    content.includes('closed') ||
    content.includes('closing'),
    'release-process.md should mention closing work items after release',
  );
});

// ---------------------------------------------------------------------------
// AC9/AC10: close step scopes to the candidate set (SA-0MU2OY1N9000XL2H)
// ---------------------------------------------------------------------------
test('close-work-items: getDescendants is exported', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(typeof mod.getDescendants, 'function');
});

test('close-work-items: refuses to force-close a candidate with collateral descendants', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.3.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
      { id: 'SA-CHILD', title: 'Child', needsProducerReview: false },
    ],
    // SA-PARENT has a descendant (SA-OTHER) outside the candidate set
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-CHILD', 'SA-OTHER'] : []),
    // SA-OTHER is non-terminal, so it remains collateral.
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    // No passing parent audit here, so the fallback cascade must not fire.
    getParentAuditFn: () => ({ readyToClose: false, auditedAt: null }),
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(closed, ['SA-CHILD'], 'only the collateral-free candidate is closed');
  assert.equal(result.refusedCount, 1);
  assert.equal(result.refusedItems[0].id, 'SA-PARENT');
  assert.deepEqual(result.refusedItems[0].collateral, ['SA-OTHER']);
});

test('close-work-items: force-closes a candidate whose descendants are all candidates', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const verified = [];
  const result = mod.closeWorkItemsAfterRelease('0.3.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
      { id: 'SA-CHILD', title: 'Child', needsProducerReview: false },
    ],
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-CHILD'] : []),
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: (id) => {
      verified.push(id);
      return { status: 'completed', stage: 'done' };
    },
  });

  // A fully in-set subtree is closed by ONE command on the parent: `wl close
  // --force` already annotates every descendant, so closing the child again
  // would duplicate its close comment (SA-0MUECDK14002J37J).
  assert.deepEqual(closed, ['SA-PARENT'],
    'only the topmost candidate issues a close command');
  assert.ok(verified.includes('SA-CHILD'),
    'the swept descendant is verified, never re-closed');
  assert.equal(result.closedCount, 2, 'parent and swept child both count as closed');
  assert.equal(result.refusedCount, 0);
});

// ---------------------------------------------------------------------------
// AC4: terminal (done/deleted) descendants are not collateral
// (SA-0MUR7Y3BJ004FGPP / defence-in-depth for SA-0MUJKPDAA002VVDP)
// ---------------------------------------------------------------------------
test('close-work-items: closes a parent whose descendants are all terminal', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.4.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) =>
      (id === 'SA-PARENT' ? ['SA-DONE', 'SA-DELETED'] : []),
    getItemLifecycleFn: (id) => {
      if (id === 'SA-DONE') return { status: 'completed', stage: 'done' };
      if (id === 'SA-DELETED') return { status: 'deleted', stage: 'done' };
      return null;
    },
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(closed, ['SA-PARENT'],
    'a parent whose descendants are all terminal must be closed');
  assert.equal(result.refusedCount, 0,
    'terminal descendants must not be treated as collateral');
  assert.deepEqual(result.refusedItems, []);
});

test('close-work-items: still refuses a parent with a non-terminal descendant', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.4.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-PLAN'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: () => ({ readyToClose: false, auditedAt: null }),
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(closed, [],
    'a genuinely non-terminal descendant must still refuse the close');
  assert.equal(result.refusedCount, 1);
  assert.deepEqual(result.refusedItems[0].collateral, ['SA-PLAN'],
    'the refusal must name the non-terminal descendant');
});

test('close-work-items: mixed terminal/non-terminal refusal names only non-terminal', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.4.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) =>
      (id === 'SA-PARENT' ? ['SA-DONE', 'SA-PLAN'] : []),
    getItemLifecycleFn: (id) =>
      (id === 'SA-DONE'
        ? { status: 'completed', stage: 'done' }
        : { status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: () => ({ readyToClose: false, auditedAt: null }),
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(closed, []);
  assert.equal(result.refusedCount, 1);
  assert.deepEqual(result.refusedItems[0].collateral, ['SA-PLAN'],
    'only the non-terminal descendant is collateral');
});

// ---------------------------------------------------------------------------
// AC2/AC4(c): a descendant held back solely by needsProducerReview=true is
// reported with a distinct refusal reason, not lumped under the generic
// collateral list (SA-0MUJKPDAA002VVDP)
// ---------------------------------------------------------------------------
test('close-work-items: npr descendant refusal reports a distinct producer-review reason', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.4.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
      {
        id: 'SA-NPR',
        title: 'Needs Review Child',
        needsProducerReview: true,
        parentId: 'SA-PARENT',
      },
    ],
    // The npr child is not covered by an audit-ready ancestor, so it stays
    // skipped rather than being overridden/closed.
    getAncestorAuditFn: () => ({ outcome: 'uncovered', ancestorId: null }),
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-NPR'] : []),
    getItemLifecycleFn: () => ({ status: 'in_progress', stage: 'in_review' }),
    getParentAuditFn: () => ({ readyToClose: false, auditedAt: null }),
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(closed, [], 'the parent must still be refused');
  assert.equal(result.refusedCount, 1);
  assert.equal(result.refusedItems[0].id, 'SA-PARENT');
  assert.ok(
    result.refusedItems[0].reason.includes('producer review'),
    `reason should name the producer-review cause, got: ${result.refusedItems[0].reason}`,
  );
  assert.ok(
    !result.refusedItems[0].reason.includes('outside the candidate set'),
    `reason must not be lumped under the generic collateral message, got: ${result.refusedItems[0].reason}`,
  );
  assert.deepEqual(
    result.refusedItems[0].needsProducerReview,
    ['SA-NPR'],
    'the refusal should name the producer-review descendant(s) separately',
  );
});

test('close-work-items: mixed generic/npr collateral reports both causes distinctly', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.4.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
      {
        id: 'SA-NPR',
        title: 'Needs Review Child',
        needsProducerReview: true,
        parentId: 'SA-PARENT',
      },
    ],
    getAncestorAuditFn: () => ({ outcome: 'uncovered', ancestorId: null }),
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-NPR', 'SA-PLAN'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: () => ({ readyToClose: false, auditedAt: null }),
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(closed, [], 'the parent must still be refused');
  assert.equal(result.refusedCount, 1);
  assert.deepEqual(
    result.refusedItems[0].collateral,
    ['SA-NPR', 'SA-PLAN'],
    'the full collateral list is preserved for callers',
  );
  assert.deepEqual(
    result.refusedItems[0].needsProducerReview,
    ['SA-NPR'],
    'the producer-review descendant is classified separately',
  );
  assert.ok(
    result.refusedItems[0].reason.includes('SA-PLAN'),
    'the generic descendant is still named',
  );
  assert.ok(
    result.refusedItems[0].reason.includes('producer review'),
    'the npr cause is also named',
  );
});

// ---------------------------------------------------------------------------
// Fallback cascade: an audit-ready parent terminalises its non-terminal
// descendants before the close (SA-0MUR7Y3BJ004FGPP recurrence). This covers
// audits recorded OUTSIDE the audit runner (manual `wl audit-set`), which
// bypass the runner's own descendant cascade and would otherwise leave the
// parent refused and stuck in_review after a release.
// --------------------------------------------------------------------------
const NOT_AUDIT_READY = () => ({ readyToClose: false, auditedAt: null });
const AUDIT_READY = (auditedAt = '2026-10-08T12:00:00.000Z') =>
  () => ({ readyToClose: true, auditedAt });

test('close-work-items: cascades non-terminal descendants of an audit-ready parent', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const cascaded = [];
  const result = mod.closeWorkItemsAfterRelease('0.5.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) =>
      (id === 'SA-PARENT' ? ['SA-OPEN', 'SA-BLOCKED'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: AUDIT_READY('2026-10-08T09:30:00.000Z'),
    runCascadeCommand: (childId, parentId, auditTimestamp) => {
      cascaded.push({ childId, parentId, auditTimestamp });
    },
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(
    cascaded.map((c) => c.childId).sort(),
    ['SA-BLOCKED', 'SA-OPEN'],
    'every non-terminal descendant must be cascaded',
  );
  assert.ok(
    cascaded.every((c) => c.parentId === 'SA-PARENT'),
    'each cascade must name the authorising parent',
  );
  assert.ok(
    cascaded.every((c) => c.auditTimestamp === '2026-10-08T09:30:00.000Z'),
    'each cascade must name the authorising audit timestamp',
  );
  assert.deepEqual(closed, ['SA-PARENT'],
    'the parent must be closed after its descendants are cascaded');
  assert.equal(result.refusedCount, 0);
  assert.equal(result.cascadedCount, 2);
  assert.equal(result.cascadedItems.length, 2);
});

test('close-work-items: does not cascade when the parent audit is not passing', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const cascaded = [];
  const result = mod.closeWorkItemsAfterRelease('0.5.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-OPEN'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: NOT_AUDIT_READY,
    runCascadeCommand: (childId) => { cascaded.push(childId); },
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(cascaded, [],
    'no cascade is authorised without a passing parent audit');
  assert.deepEqual(closed, [], 'the parent must still be refused');
  assert.equal(result.refusedCount, 1);
  assert.deepEqual(result.refusedItems[0].collateral, ['SA-OPEN']);
  assert.equal(result.cascadedCount, 0);
});

test('close-work-items: dry-run reports the cascade without mutating descendants', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const cascaded = [];
  const result = mod.closeWorkItemsAfterRelease('0.5.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) => (id === 'SA-PARENT' ? ['SA-OPEN'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: AUDIT_READY(),
    runCascadeCommand: (childId) => { cascaded.push(childId); },
    runCloseCommand: (itemId) => { closed.push(itemId); },
    dryRun: true,
  });

  assert.deepEqual(cascaded, [], 'dry-run must not cascade descendants');
  assert.deepEqual(closed, [], 'dry-run must not close the parent');
  assert.equal(result.refusedCount, 0,
    'a dry-run cascade means the parent is not refused');
  assert.equal(result.dryRun, true);
  assert.equal(result.cascadedCount, 1,
    'the dry-run cascade is still reported');
});

test('close-work-items: a cascade failure leaves that child as collateral and refuses the parent', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.5.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) =>
      (id === 'SA-PARENT' ? ['SA-OPEN', 'SA-STUCK'] : []),
    getItemLifecycleFn: () => ({ status: 'open', stage: 'plan_complete' }),
    getParentAuditFn: AUDIT_READY(),
    runCascadeCommand: (childId) => {
      if (childId === 'SA-STUCK') throw new Error('wl unavailable');
    },
    runCloseCommand: (itemId) => { closed.push(itemId); },
  });

  assert.deepEqual(closed, [],
    'a partially-cascaded subtree must not be force-closed');
  assert.equal(result.refusedCount, 1);
  assert.deepEqual(result.refusedItems[0].collateral, ['SA-STUCK'],
    'only the child whose cascade failed remains collateral');
  assert.equal(result.cascadedCount, 1);
});

test('close-work-items: already-terminal descendants are never cascaded', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const cascaded = [];
  const result = mod.closeWorkItemsAfterRelease('0.5.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-PARENT', title: 'Parent', needsProducerReview: false },
    ],
    getDescendantsFn: (id) =>
      (id === 'SA-PARENT' ? ['SA-DONE', 'SA-DELETED'] : []),
    getItemLifecycleFn: (id) => {
      if (id === 'SA-DONE') return { status: 'completed', stage: 'done' };
      if (id === 'SA-DELETED') return { status: 'deleted', stage: 'done' };
      return null;
    },
    getParentAuditFn: AUDIT_READY(),
    runCascadeCommand: (childId) => { cascaded.push(childId); },
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(cascaded, [],
    'terminal descendants must never be cascaded or commented on');
  assert.deepEqual(closed, ['SA-PARENT']);
  assert.equal(result.cascadedCount, 0);
});

// ---------------------------------------------------------------------------
// 8. Release Process docs are updated with auto-close mention
// ---------------------------------------------------------------------------
test('close-work-items: Post-merge section mentions auto-close', () => {
  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');

  // Should mention auto-closing somewhere in the document
  assert.ok(
    content.includes('auto-clos') ||
    content.includes('automatically closed') ||
    content.includes('work items are closed'),
    'release-process.md should mention that work items are automatically closed',
  );
});

// ---------------------------------------------------------------------------
// F3: Filter closeWorkItemsAfterRelease by needsProducerReview
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease filters by needsProducerReview=false', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // Verify the function checks needsProducerReview before closing
  assert.ok(
    content.includes('needsProducerReview'),
    'closeWorkItemsAfterRelease should reference needsProducerReview for filtering',
  );
});

test('close-work-items: closeWorkItemsAfterRelease logs skipped items', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // Find the closeWorkItemsAfterRelease function body
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  // Should mention skipping items
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skip') || fnBody.includes('Skip'),
    'closeWorkItemsAfterRelease should log skipped items',
  );
});

test('close-work-items: return value includes skippedCount', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skippedCount'),
    'closeWorkItemsAfterRelease return value should include skippedCount',
  );
});

test('close-work-items: return value includes skippedItems', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skippedItems'),
    'closeWorkItemsAfterRelease return value should include skippedItems',
  );
});

test('close-work-items: release-process.md mentions producer-review filtering', () => {
  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');
  assert.ok(
    content.includes('needsProducerReview') ||
    content.includes('producer review') ||
    content.includes('Producer review'),
    'release-process.md should mention producer-review filtering in close step',
  );
});

// ---------------------------------------------------------------------------
// F4: closeWorkItemsAfterRelease force-closes candidates (AC3)
// ---------------------------------------------------------------------------
// The audit gate (check-audit-gate.js) already verified audit readiness for
// every candidate before the release proceeds. The close step therefore uses
// `wl close --force` so a parent is closed even when a descendant is stuck in
// a non-terminal state (e.g. left at in_progress by a crashed audit) — the
// release close completes in a single pass instead of leaving items dangling.

import { spawnSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { makeTempDir } from '../helpers/temp-dir.mjs';

test('close-work-items: wl close is invoked with --force for candidates', () => {
  const tmpDir = makeTempDir('close-work-items-force-test-');

  // Mock `wl` on PATH: answers the candidate queries used by
  // getCandidateItems() and records every `wl close` invocation.
  const closeLog = join(tmpDir, 'close.log');
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });
  const wlMock = join(binDir, 'wl');
  writeFileSync(wlMock, `#!/usr/bin/env bash
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      echo '{"success":true,"workItems":[{"id":"SA-PARENT1","title":"Parent One","needsProducerReview":false},{"id":"SA-PARENT2","title":"Parent Two","needsProducerReview":false}]}'
    else
      echo '{"success":true,"workItems":[]}'
    fi
    ;;
  show)
    # getDescendants() reads children; the close-state verification reads the
    # persisted lifecycle. Report every mock item as terminal so a successful
    # wl close verifies without triggering a retry (SA-0MUXA5ZNZ0019QY5).
    echo '{"success":true,"workItem":{"id":"'"$2"'","status":"completed","stage":"done"},"children":[]}'
    ;;
  close)
    echo "$@" >> "$WL_CLOSE_LOG"
    echo '{"success":true}'
    ;;
  *)
    echo '{"success":true}'
    ;;
esac
`, { mode: 0o755 });
  writeFileSync(join(binDir, 'gh'), '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });

  // Driver script: import the real run-release.js and exercise the close step.
  const driver = join(tmpDir, 'driver.mjs');
  writeFileSync(driver, `
import { closeWorkItemsAfterRelease } from ${JSON.stringify(RUN_RELEASE_PATH)};
const result = closeWorkItemsAfterRelease('9.9.9');
console.log(JSON.stringify(result));
`);

  const res = spawnSync('node', [driver], {
    encoding: 'utf-8',
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH}`,
      WL_CLOSE_LOG: closeLog,
    },
    timeout: 30000,
  });
  assert.equal(res.status, 0, `driver failed: ${res.stderr}`);

  const closeLines = readFileSync(closeLog, 'utf-8').trim().split('\n').filter(Boolean);
  assert.equal(closeLines.length, 2, `expected 2 close invocations, got: ${closeLines}`);
  for (const line of closeLines) {
    assert.ok(
      line.includes('--force'),
      `wl close should include --force (single-pass close, AC3), got: ${line}`,
    );
  }
});

// ---------------------------------------------------------------------------
// SA-0MUXA5ZNZ0019QY5: verify persisted close state, bounded retry, and report
// unclosed items. A `wl close` can exit 0 yet lose the update to the
// concurrent-`wl sync` lost-update race (WL-0MUXA5RI2009EUTJ), so the close
// step re-reads persisted state via the injectable `getCloseStateFn` boundary
// instead of trusting the exit code. Every test here injects the boundary so
// the suite never touches the live worklog.
// ---------------------------------------------------------------------------
const CLOSED_STATE = () => ({ status: 'completed', stage: 'done' });
const UNCLOSED_STATE = () => ({ status: 'completed', stage: 'in_review' });

test('close-verify: all closes persist -> success with no unclosed items', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-A', title: 'A', needsProducerReview: false },
      { id: 'SA-B', title: 'B', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: CLOSED_STATE,
  });

  assert.deepEqual(closes.slice().sort(), ['SA-A', 'SA-B'], 'each candidate is closed once');
  assert.equal(result.closedCount, 2);
  assert.equal(result.unclosedCount, 0);
  assert.deepEqual(result.unclosedItems, []);
  assert.equal(result.success, true);
  assert.match(result.message, /All 2 work item\(s\) closed successfully/);
});

test('close-verify: a no-op close (exit 0, state unchanged) is retried then reported unclosed', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-OK', title: 'OK', needsProducerReview: false },
      { id: 'SA-LOST', title: 'Lost', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: (id) => (id === 'SA-OK'
      ? { status: 'completed', stage: 'done' }
      : { status: 'completed', stage: 'in_review' }),
  });

  assert.equal(
    closes.filter((id) => id === 'SA-LOST').length,
    2,
    'the lost close must be retried exactly once',
  );
  assert.equal(
    closes.filter((id) => id === 'SA-OK').length,
    1,
    'a persisted close must not be retried',
  );
  assert.equal(result.closedCount, 1);
  assert.equal(result.unclosedCount, 1);
  assert.equal(result.unclosedItems[0].id, 'SA-LOST');
  assert.equal(result.success, false);
  assert.ok(result.message.includes('SA-LOST'), 'message must name the unclosed item');
  assert.ok(
    !/All \d+ work item\(s\) closed successfully/.test(result.message),
    'must never claim blanket success when a close did not persist',
  );
});

test('close-verify: a retry that re-persists reports the item closed', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  let reads = 0;
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-FLAKY', title: 'Flaky', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: () => {
      reads++;
      // First verification is a lost read; the post-retry read is terminal.
      return reads === 1
        ? { status: 'in_review', stage: 'in_review' }
        : { status: 'completed', stage: 'done' };
    },
  });

  assert.equal(closes.length, 2, 'the close must be retried once');
  assert.equal(result.closedCount, 1);
  assert.equal(result.unclosedCount, 0);
  assert.equal(result.success, true);
});

test('close-verify: an unreadable/null verification state is unclosed (fail-safe)', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-UNREADABLE', title: 'Unreadable', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: () => {},
    getCloseStateFn: () => null,
  });

  assert.equal(result.closedCount, 0);
  assert.equal(result.unclosedCount, 1);
  assert.equal(result.success, false);
  assert.equal(result.unclosedItems[0].id, 'SA-UNREADABLE');
});

test('close-verify: a throwing verification boundary is unclosed (fail-safe)', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-THROW', title: 'Throw', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: () => {},
    getCloseStateFn: () => { throw new Error('wl show timeout'); },
  });

  assert.equal(result.closedCount, 0);
  assert.equal(result.unclosedCount, 1);
  assert.equal(result.success, false);
});

test('close-verify: dry-run neither closes nor verifies', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  let verified = 0;
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    dryRun: true,
    getCandidateItemsFn: () => [
      { id: 'SA-DRY', title: 'Dry', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: () => { closes.push('x'); },
    getCloseStateFn: () => { verified++; return { status: 'completed', stage: 'done' }; },
  });

  assert.equal(closes.length, 0, 'dry-run must not close');
  assert.equal(verified, 0, 'dry-run must not verify');
  assert.equal(result.unclosedCount, 0);
  assert.equal(result.dryRun, true);
});

test('close-verify: unclosed items never contribute to closedCount', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const result = mod.closeWorkItemsAfterRelease('0.6.0', {
    // Idempotency boundary injected so the suite never reads the live worklog.
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-P1', title: 'Persists', needsProducerReview: false },
      { id: 'SA-L1', title: 'Lost One', needsProducerReview: false },
      { id: 'SA-L2', title: 'Lost Two', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: () => {},
    getCloseStateFn: (id) => (id === 'SA-P1' ? CLOSED_STATE() : UNCLOSED_STATE()),
  });

  assert.equal(result.closedCount, 1, 'only the persisted close counts as closed');
  assert.equal(result.unclosedCount, 2);
  assert.deepEqual(
    result.unclosedItems.map((i) => i.id).sort(),
    ['SA-L1', 'SA-L2'],
    'every unclosed item is listed by id',
  );
});

// ---------------------------------------------------------------------------
// SA-0MUECDK14002J37J: idempotent post-release close sweep
// ---------------------------------------------------------------------------
const MARKED_COMMENT = (v) => [{ comment: `Closed with reason: Shipped in v${v}` }];

test('idempotency: run-release.js exports shippedMarkerPattern and carriesShippedMarker', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(typeof mod.shippedMarkerPattern, 'function');
  assert.equal(typeof mod.carriesShippedMarker, 'function');
});

test('idempotency: a candidate already carrying the marker is skipped without a close or annotation', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  const overrides = [];
  const result = mod.closeWorkItemsAfterRelease('0.7.0', {
    getCommentsFn: (id) => (id.startsWith('SA-DONE') ? MARKED_COMMENT('0.7.0') : []),
    getCandidateItemsFn: () => [
      { id: 'SA-FRESH', title: 'Fresh', needsProducerReview: false },
      { id: 'SA-DONE', title: 'Already Shipped', needsProducerReview: false },
      { id: 'SA-DONE-NPR', title: 'Already Shipped + NPR', needsProducerReview: true, parentId: 'SA-P' },
    ],
    getDescendantsFn: () => [],
    getAncestorAuditFn: () => ({ outcome: 'covered', ancestorId: 'SA-P' }),
    runOverrideCommand: (childId) => { overrides.push(childId); },
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(closes, ['SA-FRESH'],
    'only items not yet shipped for this version are closed');
  assert.deepEqual(overrides, [],
    'an already-shipped flagged child is never overridden/annotated');
  assert.equal(result.closedCount, 1);
  assert.equal(result.alreadyShippedCount, 2);
  assert.deepEqual(
    result.alreadyShippedItems.map((i) => i.id).sort(),
    ['SA-DONE', 'SA-DONE-NPR'],
  );
  assert.match(result.message, /2 item\(s\) skipped \(already shipped in v0\.7\.0\)/);
});

test('idempotency: re-running the sweep after a close is a no-op (no duplicate comments)', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const commentsByItem = new Map(); // Simulated persistent comment store.
  const closes = [];
  const run = () => mod.closeWorkItemsAfterRelease('0.8.0', {
    getCommentsFn: (id) => commentsByItem.get(id) || [],
    getCandidateItemsFn: () => [
      { id: 'SA-R1', title: 'Released One', needsProducerReview: false },
      { id: 'SA-R2', title: 'Released Two', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => {
      closes.push(itemId);
      commentsByItem.set(
        itemId,
        (commentsByItem.get(itemId) || []).concat(MARKED_COMMENT('0.8.0')),
      );
    },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  const first = run();
  const second = run();
  assert.equal(first.closedCount, 2);
  assert.equal(first.alreadyShippedCount, 0);
  assert.equal(second.closedCount, 0, 'the second sweep closes nothing');
  assert.equal(second.alreadyShippedCount, 2, 'already-shipped items are reported, not re-closed');
  assert.deepEqual(closes, ['SA-R1', 'SA-R2'], 'each item is closed exactly once');
  assert.match(second.message, /already shipped in v0\.8\.0/);
});

test('idempotency: marker match is version-exact (v0.1.1 does not suppress v0.1.18)', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  const result = mod.closeWorkItemsAfterRelease('0.1.18', {
    getCommentsFn: () => MARKED_COMMENT('0.1.1'),
    getCandidateItemsFn: () => [
      { id: 'SA-V', title: 'V18', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(closes, ['SA-V'],
    'a marker for another version never suppresses the close');
  assert.equal(result.alreadyShippedCount, 0);
});

test('idempotency: an unreadable comment source is fail-safe and does not block the close', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closes = [];
  const result = mod.closeWorkItemsAfterRelease('0.9.0', {
    getCommentsFn: () => { throw new Error('wl down'); },
    getCandidateItemsFn: () => [
      { id: 'SA-X', title: 'X', needsProducerReview: false },
    ],
    getDescendantsFn: () => [],
    runCloseCommand: (itemId) => { closes.push(itemId); },
    getCloseStateFn: () => ({ status: 'completed', stage: 'done' }),
  });

  assert.deepEqual(closes, ['SA-X'],
    'a failing marker read must never block an otherwise valid close');
  assert.equal(result.closedCount, 1);
  assert.equal(result.alreadyShippedCount, 0);
});

test('idempotency: a swept descendant that did not persist is reported unclosed', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const closed = [];
  const result = mod.closeWorkItemsAfterRelease('0.10.0', {
    getCommentsFn: () => [],
    getCandidateItemsFn: () => [
      { id: 'SA-P', title: 'Parent', needsProducerReview: false },
      { id: 'SA-C', title: 'Child', needsProducerReview: false },
    ],
    getDescendantsFn: (id) => (id === 'SA-P' ? ['SA-C'] : []),
    runCloseCommand: (itemId) => { closed.push(itemId); },
    getCloseStateFn: (id) =>
      (id === 'SA-P' ? CLOSED_STATE() : UNCLOSED_STATE()),
  });

  assert.deepEqual(closed, ['SA-P'],
    'only the parent issues a close command');
  assert.equal(result.closedCount, 1);
  assert.equal(result.unclosedCount, 1);
  assert.deepEqual(result.unclosedItems.map((i) => i.id), ['SA-C']);
  assert.match(result.unclosedItems[0].reason, /Swept by recursive close of SA-P/);
});
