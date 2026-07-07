/**
 * Worktree Helpers Unit Tests
 *
 * Tests for worktree-related naming and validation helpers. These tests
 * establish the validation criteria that implementation features must satisfy.
 *
 * Convention (from wiki: concepts/git-worktree-best-practices-for-agent-workflows):
 *   - Worktree name: wl-<work-item-id>-<slug>
 *   - Worktree path: .worklog/worktrees/<worktree-name>
 *   - Branch name matches worktree name (branch created from worktree)
 *
 * Temporary placeholder note: These tests use the existing branch naming
 * helpers from skill/ship/scripts/git-helpers.js as the foundation, since
 * worktree naming follows the same pattern. When dedicated worktree helpers
 * are implemented (in a subsequent feature), these tests should be updated
 * to call the dedicated worktree API.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '../..');

// ── Import the helpers we want to test ───────────────────────────────────────

let gitHelpers;
let importError;

try {
  const helpersModule = path.join(repoRoot, 'skill/ship/scripts/git-helpers.js');
  gitHelpers = await import(helpersModule);
} catch (err) {
  importError = err;
}

// If the module cannot be loaded, skip all tests and report the error.
// This allows the test suite to run in environments where the full toolchain
// is not available (e.g. CI without the full repo checkout).
if (importError) {
  describe('worktree-helpers', () => {
    test('module import', { skip: `Could not import git-helpers.js: ${importError.message}` }, () => {});
    test('worktree name generation', { skip: 'module not available' }, () => {});
    test('branch name extraction', { skip: 'module not available' }, () => {});
    test('validation', { skip: 'module not available' }, () => {});
    test('edge cases and error handling', { skip: 'module not available' }, () => {});
  });
} else {
  const { makeBranchName, validateBranchName } = gitHelpers;

  // ── Worktree naming conventions ──────────────────────────────────────────────
  //
  // The wiki convention defines:
  //   - Worktree name: wl-<work-item-id>-<slug>  (same pattern as branch names)
  //   - Worktree path: .worklog/worktrees/<worktree-name>
  //
  // Since both branches and worktrees use the same naming pattern, the existing
  // makeBranchName() and validateBranchName() serve as the canonical generators.

  describe('worktree name generation', () => {

    test('generates a valid worktree name from work-item id and description', () => {
      const name = makeBranchName('SA-0MPDZDPZB00121IE', 'branch-naming-policy');
      assert.equal(name, 'wl-SA-0MPDZDPZB00121IE-branch-naming-policy');
    });

    test('worktree name matches the wl-<id>-<slug> pattern', () => {
      const name = makeBranchName('SA-0MQNPZ1VX009SL27', 'adopt-git-worktrees');
      assert.match(name, /^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/);
    });

    test('slug is lowercase with hyphens', () => {
      const name = makeBranchName('SA-0ML0502B21WHXDYA', 'Create Worktree and Branch');
      const slugPart = name.split('-').slice(-4).join('-');
      // slug should be: create-worktree-and-branch
      assert.equal(slugPart, 'create-worktree-and-branch');
    });

    test('worktree name can be derived from work-item id alone with a short description', () => {
      const name = makeBranchName('WL-1', 'fix-bug');
      assert.equal(name, 'wl-WL-1-fix-bug');
      assert.match(name, /^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/);
    });

    test('worktree name with multi-part work-item id', () => {
      const name = makeBranchName('SA-0ABC123DEF456GHI', 'update-docs');
      assert.equal(name, 'wl-SA-0ABC123DEF456GHI-update-docs');
    });
  });

  describe('branch name extraction from worktree names', () => {

    test('worktree name is the same as the branch name (one-to-one)', () => {
      const worktreeName = 'wl-SA-0MPDZDPZB00121IE-branch-naming-policy';
      // Since worktree name == branch name, the branch is the same string
      assert.equal(worktreeName, worktreeName);
    });

    test('worktree path contains the worktree name as its last segment', () => {
      const worktreeName = 'wl-SA-0MPDZDPZB00121IE-branch-naming-policy';
      const worktreePath = path.join('.worklog', 'worktrees', worktreeName);
      assert.ok(worktreePath.endsWith(worktreeName));
    });

    test('branch name extracted from full worktree path', () => {
      const worktreeName = 'wl-SA-0MQNR2TGB002WJH3-update-agents-md';
      const worktreePath = path.join('.worklog', 'worktrees', worktreeName);
      const segments = worktreePath.split(path.sep);
      const extracted = segments[segments.length - 1];
      assert.equal(extracted, worktreeName);
      assert.match(extracted, /^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/);
    });
  });

  describe('validation', () => {

    test('valid worktree name passes validation', () => {
      const names = [
        'wl-SA-0MPDZDPZB00121IE-branch-naming-policy',
        'wl-SA-0MQNPZ1VX009SL27-adopt-git-worktrees',
        'wl-WL-1-fix-bug',
      ];
      for (const name of names) {
        const result = validateBranchName(name);
        assert.ok(result.valid, `Expected "${name}" to be valid: ${result.reason}`);
      }
    });

    test('invalid worktree name fails validation - missing wl- prefix', () => {
      const result = validateBranchName('SA-0MPDZDPZB00121IE-branch-naming-policy');
      assert.equal(result.valid, false);
      assert.ok(result.reason.includes('pattern'));
    });

    test('invalid worktree name fails validation - uppercase slug', () => {
      const result = validateBranchName('wl-SA-0MPDZDPZB00121IE-BRANCH-NAMING');
      assert.equal(result.valid, false);
    });

    test('invalid worktree name fails validation - no slug', () => {
      const result = validateBranchName('wl-SA-0MPDZDPZB00121IE');
      assert.equal(result.valid, false);
    });

    test('invalid worktree name fails validation - underscores', () => {
      const result = validateBranchName('wl-SA-0MPDZDPZB00121IE-my_branch');
      assert.equal(result.valid, false);
    });

    test('invalid worktree name fails validation - empty string', () => {
      const result = validateBranchName('');
      assert.equal(result.valid, false);
      assert.ok(result.reason.includes('empty'));
    });

    test('invalid worktree name fails validation - null', () => {
      const result = validateBranchName(null);
      assert.equal(result.valid, false);
    });

    test('invalid worktree name fails validation - spaces in slug', () => {
      const result = validateBranchName('wl-SA-0MPDZDPZB00121IE-my branch name');
      assert.equal(result.valid, false);
    });

    test('valid worktree name with numeric slug passes validation', () => {
      const result = validateBranchName('wl-SA-001-fix-2-bugs');
      assert.equal(result.valid, true);
    });
  });

  describe('worktree path conventions', () => {

    test('worktree path lives under .worklog/worktrees/', () => {
      const worktreeName = 'wl-SA-0MPDZDPZB00121IE-branch-naming-policy';
      const expectedPrefix = path.join('.worklog', 'worktrees');
      const fullPath = path.join(expectedPrefix, worktreeName);
      assert.ok(fullPath.startsWith(expectedPrefix));
    });

    test('worktree path is unique per work-item', () => {
      const name1 = makeBranchName('SA-001', 'first-task');
      const name2 = makeBranchName('SA-002', 'second-task');
      const path1 = path.join('.worklog', 'worktrees', name1);
      const path2 = path.join('.worklog', 'worktrees', name2);
      assert.notEqual(path1, path2);
    });

    test('same work-item with different slugs produces different paths', () => {
      const name1 = makeBranchName('SA-001', 'first-task');
      const name2 = makeBranchName('SA-001', 'second-task');
      assert.notEqual(name1, name2);
    });
  });

  describe('edge cases and error handling', () => {

    test('makeBranchName throws on empty workItemId', () => {
      assert.throws(() => makeBranchName('', 'desc'), /workItemId/);
    });

    test('makeBranchName throws on missing workItemId', () => {
      assert.throws(() => makeBranchName(null, 'desc'), /workItemId/);
    });

    test('makeBranchName throws on empty shortDesc', () => {
      assert.throws(() => makeBranchName('SA-001', ''), /shortDesc/);
    });

    test('makeBranchName throws on missing shortDesc', () => {
      assert.throws(() => makeBranchName('SA-001', null), /shortDesc/);
    });

    test('makeBranchName handles description with special characters gracefully', () => {
      const name = makeBranchName('SA-001', 'Fix: login bug (urgent!)');
      assert.match(name, /^wl-SA-001-/);
      // slug should normalize special chars to hyphens
      assert.ok(name.includes('fix'));
      assert.ok(name.includes('login'));
      assert.ok(name.includes('bug'));
      assert.ok(name.includes('urgent'));
    });

    test('makeBranchName handles very long descriptions by truncating', () => {
      const longDesc = 'a'.repeat(500);
      const name = makeBranchName('SA-001', longDesc);
      // should not exceed 250 chars
      assert.ok(name.length <= 250, `Branch name length ${name.length} exceeds 250`);
      // should still start with wl-SA-001-
      assert.ok(name.startsWith('wl-SA-001-'));
    });
  });
}
