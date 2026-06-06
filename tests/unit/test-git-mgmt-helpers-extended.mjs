/**
 * Unit tests for skill/git-management/scripts/git-mgmt-helpers.mjs — additional coverage
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseArgs,
  hasFlag,
  getFlag,
  validateWorkItemId,
  makeSlug,
} from '../../skill/git-management/scripts/git-mgmt-helpers.mjs';

describe('parseArgs edge cases', () => {
  test('handles multiple flags of different types', () => {
    const { flags, positional } = parseArgs(['--dry-run', '--name', 'test', 'arg1']);
    assert.equal(hasFlag(flags, 'dry-run'), true);
    assert.equal(getFlag(flags, 'name'), 'test');
    assert.deepEqual(positional, ['arg1']);
  });

  test('handles flags after positional args', () => {
    const { flags, positional } = parseArgs(['arg1', '--json']);
    assert.deepEqual(positional, ['arg1']);
    assert.equal(getFlag(flags, 'json'), true);
  });
});

describe('validateWorkItemId edge cases', () => {
  test('rejects null', () => {
    const result = validateWorkItemId(null);
    assert.equal(result.valid, false);
  });

  test('trims whitespace from ID', () => {
    const result = validateWorkItemId('  SA-0MPMI7FWI004PXHS  ');
    assert.equal(result.valid, true);
  });
});

describe('makeSlug edge cases', () => {
  test('handles unicode-like characters', () => {
    assert.equal(makeSlug('hello—world'), 'hello-world');
  });

  test('handles numbers in description', () => {
    assert.equal(makeSlug('fix issue 123'), 'fix-issue-123');
  });
});
