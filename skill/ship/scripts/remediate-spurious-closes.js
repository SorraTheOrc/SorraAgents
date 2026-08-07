#!/usr/bin/env node
// remediate-spurious-closes.js — idempotent remediation sweep for work items
// spuriously closed by the unit test suite (SA-0MSJ2XMQL006CVQS).
//
// Root cause: tests/unit/test-close-work-items-after-release.mjs invoked the
// real `closeWorkItemsAfterRelease('1.0.0' / '1.2.3')` export against the live
// worklog, closing ~368 real work items with the reason "Shipped in v1.0.0" /
// "Shipped in v1.2.3" even though no such release ever happened. The reason
// strings v1.0.0/v1.2.3 never existed as release tags — legitimate releases
// use real versions (e.g. "Shipped in v0.1.11").
//
// This helper:
//   1. Lists ALL work items (`wl list --json`).
//   2. For each item, lists its comments (`wl comment list <id> --json`).
//   3. Finds spurious close comments: author === 'worklog' with content
//      exactly 'Closed with reason: Shipped in v1.0.0' or
//      'Closed with reason: Shipped in v1.2.3'.
//   4. Deletes those comments (`wl comment delete <comment-id>`).
//   5. Restores each affected item to `status=completed, stage=in_review`
//      (`wl update <id> --status completed --stage in_review`) — the valid
//      status/stage pair for a release-ready item.
//
// The sweep is idempotent and safe to re-run: after a successful sweep no
// spurious comments remain, so re-running performs no further mutations.
// Legitimate close comments (real versions such as v0.1.11) are never touched.
//
// Usage:
//   node skill/ship/scripts/remediate-spurious-closes.js

import { execSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

// ── Spurious close comment definitions ───────────────────────────────────────

/**
 * The exact close-comment contents produced by the test-isolation bug.
 *
 * `wl close --reason "Shipped in v<version>"` records a comment of the form
 * `Closed with reason: Shipped in v<version>` (author `worklog`). The two
 * hardcoded versions below were used by the buggy unit test file and never
 * correspond to a real release tag.
 */
export const SPURIOUS_CLOSE_COMMENTS = Object.freeze([
  'Closed with reason: Shipped in v1.0.0',
  'Closed with reason: Shipped in v1.2.3',
]);

/**
 * Determine whether a comment is a spurious close comment.
 *
 * Matches strictly: author must be `worklog` and the comment content must be
 * exactly one of {@link SPURIOUS_CLOSE_COMMENTS}. Comments for legitimate
 * releases (e.g. "Shipped in v0.1.11") or other authors are never matched.
 *
 * @param {object|null|undefined} comment - A comment object from `wl comment list`.
 * @returns {boolean} True if the comment is a spurious close comment.
 */
export function isSpuriousCloseComment(comment) {
  return Boolean(comment)
    && typeof comment === 'object'
    && comment.author === 'worklog'
    && typeof comment.comment === 'string'
    && SPURIOUS_CLOSE_COMMENTS.includes(comment.comment);
}

// ── Worklog boundary (injectable for tests) ─────────────────────────────────

function runWl(args) {
  return execSync(`wl ${args.join(' ')} --json`, {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    maxBuffer: 10 * 1024 * 1024,
  });
}

/**
 * Default worklog boundary: list all work items.
 *
 * `wl list --json` (no status filter) returns every item, including
 * completed/closed ones, so a single call covers the whole database.
 *
 * @returns {Array<{id: string, title: string}>} All work items.
 */
export function listAllWorkItems() {
  const data = JSON.parse(runWl(['list']));
  return (data.workItems && Array.isArray(data.workItems)) ? data.workItems : [];
}

/**
 * Default worklog boundary: list comments for a work item.
 *
 * @param {string} itemId - The work item id.
 * @returns {Array<{id: string, author: string, comment: string}>} Comments.
 */
export function listComments(itemId) {
  const data = JSON.parse(runWl(['comment', 'list', itemId]));
  return (data.comments && Array.isArray(data.comments)) ? data.comments : [];
}

/**
 * Default worklog boundary: delete a comment.
 *
 * @param {string} commentId - The comment id.
 */
export function deleteComment(commentId) {
  runWl(['comment', 'delete', commentId]);
}

/**
 * Default worklog boundary: restore an item to its pre-close state.
 *
 * Items spuriously closed by the test suite were implementation-complete and
 * waiting to be released (`stage=in_review`) before the close. Restoring them
 * re-enters the release candidate set so a real, verified release can close
 * them properly.
 *
 * The worklog validates status/stage combinations: `stage=in_review` is only
 * compatible with `status=completed` (status `open` is limited to stages
 * idea/intake_complete/plan_complete/in_progress), so the restore uses the
 * consistent `completed/in_review` pair (AGENTS.md: "When advancing to
 * in_review, set status to completed").
 *
 * @param {string} itemId - The work item id.
 */
export function restoreItem(itemId) {
  runWl(['update', itemId, '--status', 'completed', '--stage', 'in_review']);
}

// ── Remediation sweep ────────────────────────────────────────────────────────

/**
 * Perform the remediation sweep.
 *
 * Scans every work item for spurious close comments, deletes them, and
 * restores each affected item to `status=completed, stage=in_review`.
 * Idempotent: re-running after a successful sweep is a no-op.
 *
 * @param {object} [options] - Injectable worklog boundary (used by unit tests).
 * @param {typeof listAllWorkItems} [options.listAllWorkItems]
 * @param {typeof listComments} [options.listComments]
 * @param {typeof deleteComment} [options.deleteComment]
 * @param {typeof restoreItem} [options.restoreItem]
 * @param {(msg: string) => void} [options.log] - Log sink (defaults to console.log).
 * @returns {{
 *   success: boolean,
 *   scannedItems: number,
 *   affectedItems: number,
 *   commentsDeleted: number,
 *   restoredItems: number,
 *   errors: Array<{ itemId: string, message: string }>,
 *   affected: Array<{ id: string, title: string, commentsDeleted: string[] }>
 * }}
 */
export function remediateSpuriousCloses(options = {}) {
  const {
    listAllWorkItems: listItems = listAllWorkItems,
    listComments: listItemComments = listComments,
    deleteComment: removeComment = deleteComment,
    restoreItem: restoreAffectedItem = restoreItem,
    log = (msg) => console.log(msg),
  } = options;

  const items = listItems();
  log(`Scanning ${items.length} work item(s) for spurious close comments...`);

  const affected = [];
  const errors = [];
  let commentsDeleted = 0;
  let restoredItems = 0;

  for (const item of items) {
    let comments;
    try {
      comments = listItemComments(item.id);
    } catch (err) {
      errors.push({ itemId: item.id, message: `Failed to list comments: ${err.message}` });
      continue;
    }

    const spurious = comments.filter((c) => isSpuriousCloseComment(c));
    if (spurious.length === 0) {
      continue;
    }

    log(`  ○ ${item.title || item.id} (${item.id}) — ${spurious.length} spurious close comment(s)`);

    const deletedIds = [];
    for (const comment of spurious) {
      try {
        removeComment(comment.id);
        deletedIds.push(comment.id);
        commentsDeleted++;
      } catch (err) {
        errors.push({
          itemId: item.id,
          message: `Failed to delete comment ${comment.id}: ${err.message}`,
        });
      }
    }

    try {
      restoreAffectedItem(item.id);
      restoredItems++;
    } catch (err) {
      errors.push({ itemId: item.id, message: `Failed to restore item: ${err.message}` });
    }

    affected.push({
      id: item.id,
      title: item.title || item.id,
      commentsDeleted: deletedIds,
    });
  }

  const summary = [
    `Sweep complete: ${affected.length} affected item(s), ${commentsDeleted} spurious comment(s) deleted, ${restoredItems} item(s) restored.`,
    errors.length > 0 ? ` ${errors.length} error(s).` : '',
  ].join('');
  log(summary);

  return {
    success: errors.length === 0,
    scannedItems: items.length,
    affectedItems: affected.length,
    commentsDeleted,
    restoredItems,
    errors,
    affected,
  };
}

// ── CLI Entry Point ──────────────────────────────────────────────────────────

const isMainModule = process.argv[1] &&
  (realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1])));

if (isMainModule) {
  const result = remediateSpuriousCloses();
  if (result.errors.length > 0) {
    console.error('\nErrors:');
    for (const err of result.errors) {
      console.error(`  - ${err.itemId}: ${err.message}`);
    }
    process.exitCode = 1;
  }
}
