/**
 * Unit tests for the remediation sweep helper
 * (skill/ship/scripts/remediate-spurious-closes.js, SA-0MSJ2XMQL006CVQS).
 *
 * The sweep identifies work items spuriously closed by the unit test suite
 * ("Closed with reason: Shipped in v1.0.0" / "v1.2.3", author `worklog`),
 * deletes those comments, and restores each item to status=open,
 * stage=in_review. It must be idempotent and must never touch legitimate
 * close comments (real release versions such as v0.1.11).
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SWEEP_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'remediate-spurious-closes.js');

// ── Fixtures ─────────────────────────────────────────────────────────────────

const spuriousV10 = { id: 'SA-C0SPUR10', author: 'worklog', comment: 'Closed with reason: Shipped in v1.0.0' };
const spuriousV123 = { id: 'SA-C0SPUR123', author: 'worklog', comment: 'Closed with reason: Shipped in v1.2.3' };
// Distinct id for a second item that also carries a v1.0.0 spurious close.
const spuriousV10b = { id: 'SA-C0SPUR10B', author: 'worklog', comment: 'Closed with reason: Shipped in v1.0.0' };
const legitClose = { id: 'SA-C0LEGIT', author: 'worklog', comment: 'Closed with reason: Shipped in v0.1.11' };
const otherAuthor = { id: 'SA-C0OTHER', author: 'map', comment: 'Closed with reason: Shipped in v1.0.0' };
const normalComment = { id: 'SA-C0NORM', author: 'map', comment: 'Implementation completed' };

// ── Tests: module surface ────────────────────────────────────────────────────

test('remediate-spurious-closes: module file exists', () => {
  assert.ok(existsSync(SWEEP_PATH), 'remediate-spurious-closes.js should exist');
});

test('remediate-spurious-closes: exports expected functions', async () => {
  const mod = await import(SWEEP_PATH);
  assert.equal(typeof mod.remediateSpuriousCloses, 'function');
  assert.equal(typeof mod.isSpuriousCloseComment, 'function');
  assert.ok(Array.isArray(mod.SPURIOUS_CLOSE_COMMENTS));
});

test('remediate-spurious-closes: SPURIOUS_CLOSE_COMMENTS lists the exact buggy reasons', async () => {
  const mod = await import(SWEEP_PATH);
  assert.deepEqual([...mod.SPURIOUS_CLOSE_COMMENTS].sort(), [
    'Closed with reason: Shipped in v1.0.0',
    'Closed with reason: Shipped in v1.2.3',
  ]);
});

// ── Tests: isSpuriousCloseComment matching ───────────────────────────────────

describe('isSpuriousCloseComment', () => {
  test('matches exact spurious close comments from author worklog', async () => {
    const mod = await import(SWEEP_PATH);
    assert.equal(mod.isSpuriousCloseComment(spuriousV10), true);
    assert.equal(mod.isSpuriousCloseComment(spuriousV123), true);
  });

  test('rejects legitimate close comments (real release versions)', async () => {
    const mod = await import(SWEEP_PATH);
    assert.equal(mod.isSpuriousCloseComment(legitClose), false);
  });

  test('rejects comments from other authors', async () => {
    const mod = await import(SWEEP_PATH);
    assert.equal(mod.isSpuriousCloseComment(otherAuthor), false);
  });

  test('rejects normal comments and nullish input', async () => {
    const mod = await import(SWEEP_PATH);
    assert.equal(mod.isSpuriousCloseComment(normalComment), false);
    assert.equal(mod.isSpuriousCloseComment(null), false);
    assert.equal(mod.isSpuriousCloseComment(undefined), false);
  });
});

// ── Tests: remediateSpuriousCloses (injected worklog boundary) ──────────────

describe('remediateSpuriousCloses', () => {
  test('deletes spurious comments and restores affected items only', async () => {
    const mod = await import(SWEEP_PATH);

    const deleted = [];
    const restored = [];
    const logs = [];

    const items = [
      { id: 'SA-ITEM1', title: 'Item One' },   // 2 spurious comments
      { id: 'SA-ITEM2', title: 'Item Two' },   // 1 spurious + 1 legitimate close
      { id: 'SA-ITEM3', title: 'Item Three' }, // clean
    ];

    const commentsByItem = {
      'SA-ITEM1': [spuriousV10, spuriousV123],
      'SA-ITEM2': [legitClose, spuriousV10b],
      'SA-ITEM3': [normalComment],
    };

    const result = mod.remediateSpuriousCloses({
      listAllWorkItems: () => items,
      listComments: (itemId) => commentsByItem[itemId] || [],
      deleteComment: (commentId) => deleted.push(commentId),
      restoreItem: (itemId) => restored.push(itemId),
      log: (msg) => logs.push(msg),
    });

    assert.deepEqual(deleted.sort(), ['SA-C0SPUR10', 'SA-C0SPUR10B', 'SA-C0SPUR123'], 'only spurious comments deleted');
    assert.deepEqual(restored.sort(), ['SA-ITEM1', 'SA-ITEM2'], 'only affected items restored');
    assert.equal(result.affectedItems, 2);
    assert.equal(result.commentsDeleted, 3);
    assert.equal(result.restoredItems, 2);
    assert.equal(result.scannedItems, 3);
    assert.equal(result.success, true);
    assert.deepEqual(result.errors, []);
    // The legitimate close comment must not be deleted.
    assert.ok(!deleted.includes('SA-C0LEGIT'));
  });

  test('is idempotent: a clean worklog triggers no mutations', async () => {
    const mod = await import(SWEEP_PATH);

    const deleted = [];
    const restored = [];

    const result = mod.remediateSpuriousCloses({
      listAllWorkItems: () => [{ id: 'SA-CLEAN', title: 'Clean' }],
      listComments: () => [normalComment, legitClose],
      deleteComment: (commentId) => deleted.push(commentId),
      restoreItem: (itemId) => restored.push(itemId),
      log: () => {},
    });

    assert.equal(result.affectedItems, 0);
    assert.equal(result.commentsDeleted, 0);
    assert.equal(result.restoredItems, 0);
    assert.equal(deleted.length, 0);
    assert.equal(restored.length, 0);
    assert.equal(result.success, true);
  });

  test('reports per-item errors without aborting the sweep', async () => {
    const mod = await import(SWEEP_PATH);

    const result = mod.remediateSpuriousCloses({
      listAllWorkItems: () => [{ id: 'SA-ERR', title: 'Err' }, { id: 'SA-OK', title: 'Ok' }],
      listComments: (itemId) => (itemId === 'SA-ERR'
        ? [spuriousV10]
        : [{ id: 'SA-C0OK', author: 'worklog', comment: 'Closed with reason: Shipped in v1.2.3' }]),
      deleteComment: (commentId) => {
        if (commentId === 'SA-C0SPUR10') throw new Error('delete failed');
      },
      restoreItem: (itemId) => {
        if (itemId === 'SA-OK') throw new Error('restore failed');
      },
      log: () => {},
    });

    assert.equal(result.success, false);
    assert.ok(result.errors.length >= 2, 'delete and restore failures should be reported');
    assert.equal(result.commentsDeleted, 1, 'the non-failing delete should still count');
  });
});

// ── Integration: real script with a mocked `wl` on PATH ─────────────────────

describe('remediateSpuriousCloses CLI (mocked wl on PATH)', () => {
  test('deletes spurious comments and restores items via the real CLI wiring', () => {
    const tmpDir = mkdtempSync(join(tmpdir(), 'remediate-spurious-test-'));
    const binDir = join(tmpDir, 'bin');
    mkdirSync(binDir, { recursive: true });

    // Mock `wl`: returns one item with one spurious close comment, then
    // records every `comment delete` and `update` invocation.
    const actionLog = join(tmpDir, 'actions.log');
    const wlMock = join(binDir, 'wl');
    writeFileSync(wlMock, `#!/usr/bin/env bash
case "$1" in
  list)
    echo '{"success":true,"workItems":[{"id":"SA-MOCK1","title":"Mock One"},{"id":"SA-MOCK2","title":"Mock Two"}]}'
    ;;
  comment)
    if [[ "$2" == "list" ]]; then
      if [[ "$3" == "SA-MOCK1" ]]; then
        echo '{"success":true,"comments":[{"id":"SA-C0MOCKSPUR","author":"worklog","comment":"Closed with reason: Shipped in v1.0.0"}]}'
      else
        echo '{"success":true,"comments":[{"id":"SA-C0MOCKNORM","author":"map","comment":"normal"}]}'
      fi
    elif [[ "$2" == "delete" ]]; then
      echo "\$@" >> "$ACTION_LOG"
      echo '{"success":true}'
    fi
    ;;
  update)
    echo "\$@" >> "$ACTION_LOG"
    echo '{"success":true}'
    ;;
  *)
    echo '{"success":true}'
    ;;
esac
`, { mode: 0o755 });

    const res = spawnSync('node', [SWEEP_PATH], {
      encoding: 'utf-8',
      env: { ...process.env, PATH: `${binDir}:${process.env.PATH}`, ACTION_LOG: actionLog },
      timeout: 30000,
    });

    assert.equal(res.status, 0, `sweep failed: ${res.stderr}`);

    const actions = readFileSync(actionLog, 'utf-8').trim().split('\n').filter(Boolean);
    const deletes = actions.filter((a) => a.includes('comment delete'));
    const updates = actions.filter((a) => a.includes('update'));

    assert.deepEqual(deletes, ['comment delete SA-C0MOCKSPUR --json'], 'only the spurious comment should be deleted');
    assert.equal(updates.length, 1, 'the affected item should be restored');
    assert.ok(
      updates[0].includes('--status completed') && updates[0].includes('--stage in_review'),
      `restore should set completed/in_review, got: ${updates[0]}`,
    );
    assert.ok(
      res.stdout.includes('Sweep complete'),
      `sweep should print a summary, got: ${res.stdout}`,
    );
  });
});
