/**
 * Unit tests for worklog/ branch gating in merge-pr.mjs.
 *
 * Tests that merge-pr.mjs properly detects and blocks PR merges
 * involving worklog/ branches.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MERGE_PR_PATH = join(REPO_ROOT, 'skill', 'git-management', 'scripts', 'merge-pr.mjs');

describe('merge-pr.mjs: worklog/ branch gating', () => {
  const content = readFileSync(MERGE_PR_PATH, 'utf-8');

  test('checks headRefName for worklog/ prefix', () => {
    assert.ok(
      content.includes('headRefName') && content.includes('worklog/'),
      'merge-pr.mjs should check headRefName against worklog/ prefix',
    );
  });

  test('checks baseRefName for worklog/ prefix', () => {
    assert.ok(
      content.includes('baseRefName') && content.includes('worklog/'),
      'merge-pr.mjs should check baseRefName against worklog/ prefix',
    );
  });

  test('blocks merge with SAFETY_VIOLATION exit code when worklog/ branch detected', () => {
    assert.ok(
      content.includes('worklog/') && content.includes('SAFETY_VIOLATION'),
      'merge-pr.mjs should block worklog/ branches with SAFETY_VIOLATION exit code',
    );
  });

  test('produces a clear error message when worklog/ branch is involved', () => {
    assert.ok(
      content.includes('Cannot merge') && content.includes('worklog'),
      'merge-pr.mjs should produce a clear error message mentioning worklog/',
    );
  });

  test('checks both head and base branches (not just one)', () => {
    // The code should check both headRefName and baseRefName
    assert.ok(
      content.includes('headRefName') && content.includes('baseRefName') &&
      content.includes("startsWith('worklog/')"),
      'merge-pr.mjs should check both head and base branches for worklog/ prefix',
    );
  });
});
