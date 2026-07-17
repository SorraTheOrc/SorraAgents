/**
 * Unit tests for skill/ship/scripts/ship.js
 *
 * Validates PROTECTED_BRANCHES, validatePushTarget(), and worklog/ ref protection.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { PROTECTED_BRANCHES, validatePushTarget } from '../../skill/ship/scripts/ship.js';

// ── PROTECTED_BRANCHES ────────────────────────────────────────────────────────

describe('PROTECTED_BRANCHES', () => {
  test('contains main', () => {
    assert.ok(PROTECTED_BRANCHES.includes('main'));
  });

  test('contains master', () => {
    assert.ok(PROTECTED_BRANCHES.includes('master'));
  });

  test('contains HEAD', () => {
    assert.ok(PROTECTED_BRANCHES.includes('HEAD'));
  });

  test('contains worklog/ prefix pattern', () => {
    assert.ok(PROTECTED_BRANCHES.includes('worklog/'));
  });

  test('is frozen (immutable)', () => {
    assert.throws(() => {
      PROTECTED_BRANCHES.push('new-entry');
    }, /Cannot add property/);
  });
});

// ── validatePushTarget ───────────────────────────────────────────────────────

describe('validatePushTarget', () => {
  test('rejects push to main', () => {
    const result = validatePushTarget('main');
    assert.equal(result.allowed, false);
    assert.ok(result.reason);
  });

  test('rejects push to master', () => {
    const result = validatePushTarget('master');
    assert.equal(result.allowed, false);
  });

  test('rejects push to HEAD', () => {
    const result = validatePushTarget('HEAD');
    assert.equal(result.allowed, false);
  });

  test('rejects push to worklog/data (prefix match)', () => {
    const result = validatePushTarget('worklog/data');
    assert.equal(result.allowed, false);
    assert.ok(result.reason);
  });

  test('rejects push to worklog/remotes/origin/worklog/data (prefix match)', () => {
    const result = validatePushTarget('worklog/remotes/origin/worklog/data');
    assert.equal(result.allowed, false);
  });

  test('rejects any branch starting with worklog/ (prefix match)', () => {
    const result = validatePushTarget('worklog/something-else');
    assert.equal(result.allowed, false);
  });

  test('allows push to dev branch', () => {
    const result = validatePushTarget('dev');
    assert.equal(result.allowed, true);
  });

  test('allows push to feature branches', () => {
    const result = validatePushTarget('wl-SA-001-feature');
    assert.equal(result.allowed, true);
  });

  test('allows push to release branches', () => {
    const result = validatePushTarget('release/v1.0');
    assert.equal(result.allowed, true);
  });

  test('rejects empty branch name', () => {
    const result = validatePushTarget('');
    assert.equal(result.allowed, false);
  });

  test('rejects null branch name', () => {
    const result = validatePushTarget(null);
    assert.equal(result.allowed, false);
  });

  test('rejects undefined branch name', () => {
    const result = validatePushTarget(undefined);
    assert.equal(result.allowed, false);
  });

  test('rejects non-string branch name', () => {
    const result = validatePushTarget(123);
    assert.equal(result.allowed, false);
  });
});
