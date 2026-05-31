/**
 * Unit tests for agent/git-helpers.js
 *
 * Tests canonical branch name generation, validation, and push-policy enforcement.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  makeBranchName,
  validateBranchName,
  BLOCKED_BRANCHS,
  isBranchBlocked,
  BRANCH_NAME_PATTERN,
} from '../../skill/ship/scripts/git-helpers.js';

// ── makeBranchName ──────────────────────────────────────────────────────────

describe('makeBranchName', () => {
  test('generates canonical branch name with wl- prefix', () => {
    const name = makeBranchName('SA-0MPDZDPZB00121IE', 'branch-naming-policy');
    assert.equal(name, 'wl-SA-0MPDZDPZB00121IE-branch-naming-policy');
  });

  test('lowercases the short description', () => {
    const name = makeBranchName('SA-001', 'Fix-Bug');
    assert.equal(name, 'wl-SA-001-fix-bug');
  });

  test('replaces spaces with hyphens in short description', () => {
    const name = makeBranchName('SA-002', 'add new feature');
    assert.equal(name, 'wl-SA-002-add-new-feature');
  });

  test('strips leading/trailing whitespace from short description', () => {
    const name = makeBranchName('SA-003', '  trimmed  ');
    assert.equal(name, 'wl-SA-003-trimmed');
  });

  test('collapses multiple consecutive hyphens into one', () => {
    const name = makeBranchName('SA-004', 'fix--double--dash');
    assert.equal(name, 'wl-SA-004-fix-double-dash');
  });

  test('throws if workItemId is empty', () => {
    assert.throws(
      () => makeBranchName('', 'desc'),
      /workItemId is required/
    );
  });

  test('throws if shortDesc is empty after trimming', () => {
    assert.throws(
      () => makeBranchName('SA-005', '   '),
      /shortDesc is required/
    );
  });

  test('truncates long branch names to a safe length', () => {
    const longDesc = 'a'.repeat(300);
    const name = makeBranchName('SA-006', longDesc);
    assert.ok(name.length <= 250, `Branch name too long: ${name.length} chars`);
    assert.ok(name.startsWith('wl-SA-006-'));
  });
});

// ── validateBranchName ──────────────────────────────────────────────────────

describe('validateBranchName', () => {
  test('accepts a valid wl-<id>-<desc> pattern', () => {
    const result = validateBranchName('wl-SA-0MPDZDPZB00121IE-branch-naming-policy');
    assert.equal(result.valid, true);
    assert.equal(result.reason, undefined);
  });

  test('accepts names with multiple hyphens in the description', () => {
    const result = validateBranchName('wl-SA-001-some-long-description-here');
    assert.equal(result.valid, true);
  });

  test('rejects names missing the wl- prefix', () => {
    const result = validateBranchName('feature-SA-001-something');
    assert.equal(result.valid, false);
    assert.ok(result.reason);
  });

  test('rejects names with no work-item id segment', () => {
    const result = validateBranchName('wl--some-desc');
    assert.equal(result.valid, false);
  });

  test('rejects plain main branch', () => {
    const result = validateBranchName('main');
    assert.equal(result.valid, false);
  });

  test('rejects names with uppercase characters', () => {
    const result = validateBranchName('wl-SA-001-UpperCase');
    assert.equal(result.valid, false);
  });

  test('rejects names with underscores', () => {
    const result = validateBranchName('wl-SA-001-bad_name');
    assert.equal(result.valid, false);
  });

  test('rejects names with spaces', () => {
    const result = validateBranchName('wl-SA-001-bad name');
    assert.equal(result.valid, false);
  });

  test('rejects names with special characters', () => {
    const result = validateBranchName('wl-SA-001-bad@name!');
    assert.equal(result.valid, false);
  });

  test('rejects empty string', () => {
    const result = validateBranchName('');
    assert.equal(result.valid, false);
  });
});

// ── isBranchBlocked ─────────────────────────────────────────────────────────

describe('isBranchBlocked', () => {
  test('blocks push to main', () => {
    assert.equal(isBranchBlocked('main'), true);
  });

  test('blocks push to HEAD', () => {
    assert.equal(isBranchBlocked('HEAD'), true);
  });

  test('blocks push to master', () => {
    assert.equal(isBranchBlocked('master'), true);
  });

  test('allows push to feature branches', () => {
    assert.equal(isBranchBlocked('wl-SA-001-feature'), false);
  });

  test('allows push to dev branch', () => {
    assert.equal(isBranchBlocked('dev'), false);
  });

  test('allows push to release branches', () => {
    assert.equal(isBranchBlocked('release/v1.0'), false);
  });

  test('BLOCKED_BRANCHS contains main', () => {
    assert.ok(BLOCKED_BRANCHS.includes('main'));
  });
});

// ── BRANCH_NAME_PATTERN export ──────────────────────────────────────────────

describe('BRANCH_NAME_PATTERN', () => {
  test('exports a RegExp instance', () => {
    assert.ok(BRANCH_NAME_PATTERN instanceof RegExp);
  });

  test('pattern matches valid branch names', () => {
    assert.ok(BRANCH_NAME_PATTERN.test('wl-SA-0MPDZDPZB00121IE-test'));
  });

  test('pattern rejects invalid branch names', () => {
    assert.equal(BRANCH_NAME_PATTERN.test('feature-bad-name'), false);
  });
});
