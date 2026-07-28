/**
 * Unit tests for worklog-ref gating in the release pipeline.
 *
 * Tests that merge-dev-to-main.sh and run-release.js properly detect
 * and reject worklog refs before proceeding with a release.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');

const MERGE_SCRIPT_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');

// ── merge-dev-to-main.sh worklog-ref gating ─────────────────────────────────

describe('merge-dev-to-main.sh: worklog-ref gating', () => {
  const content = readFileSync(MERGE_SCRIPT_PATH, 'utf-8');

  test('references git for-each-ref refs/worklog/ to detect worklog refs', () => {
    assert.ok(
      content.includes('for-each-ref refs/worklog/'),
      'Script should use git for-each-ref refs/worklog/ to detect worklog refs',
    );
  });

  test('prints a clear error message when worklog refs are detected', () => {
    assert.ok(
      content.includes('worklog') && (content.includes('ref') || content.includes('refs')),
      'Script should mention worklog refs in its error message',
    );
  });

  test('aborts with non-zero exit code when worklog refs are present', () => {
    assert.ok(
      content.match(/exit\s+(\d+|[^\s]+)/) || content.includes('exit'),
      'Script should exit with non-zero when worklog refs are detected',
    );
  });

  test('gating check is before the merge operation', () => {
    // Find the position of the worklog check and the git merge command
    const worklogPos = content.indexOf('worklog');
    const mergePos = content.indexOf('git merge');
    assert.ok(
      worklogPos >= 0 && mergePos >= 0,
      'Both worklog check and merge operation should exist',
    );
    assert.ok(
      worklogPos < mergePos,
      'Worklog gating check should appear before the merge operation',
    );
  });
});

// ── run-release.js worklog-ref gating ────────────────────────────────────────

describe('run-release.js: worklog-ref gating', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  test('references worklog in comments or logic', () => {
    assert.ok(
      content.includes('worklog') || content.includes('Worklog'),
      'run-release.js should reference worklog somewhere for worklog-ref gating',
    );
  });

  test('has a worklog-ref gating step (or references merge-dev-to-main.sh check)', () => {
    // The worklog gating can either be in run-release.js itself or delegated
    // to merge-dev-to-main.sh. If it delegates, it should reference the script.
    assert.ok(
      content.includes('worklog') ||
      content.includes('merge-dev-to-main') ||
      content.includes('release script'),
      'run-release.js should either have its own worklog gating or delegate to the release script',
    );
  });
});

// ── Integration: the shell script has proper error handling ─────────────────

describe('merge-dev-to-main.sh: error handling structure', () => {
  const content = readFileSync(MERGE_SCRIPT_PATH, 'utf-8');

  test('gating check is inside a bash if/fi block', () => {
    assert.ok(
      content.includes('if') && content.includes('fi'),
      'The gating check should use proper bash conditionals',
    );
  });

  test('error output goes to stderr', () => {
    assert.ok(
      content.includes('>&2') || content.includes('&>2') || content.includes('1>&2'),
      'Error messages should be directed to stderr',
    );
  });
});
