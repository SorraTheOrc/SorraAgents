/**
 * Unit tests for check-worklog-refs.js
 *
 * Validates that the worklog-ref gate ignores remote-tracking mirror refs
 * (refs/worklog/remotes/...) while still detecting the dangerous local
 * orphan ref (refs/worklog/data).
 */

import { test, describe, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import { checkWorklogRefs } from '../../skill/ship/scripts/check-worklog-refs.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Create a worklog ref in this test repo.
 */
function createRef(refName, refValue = 'HEAD') {
  try {
    execSync(`git update-ref ${refName} ${refValue}`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {
    // ignore
  }
}

/**
 * Delete a worklog ref from this test repo.
 */
function deleteRef(refName) {
  try {
    execSync(`git update-ref -d ${refName}`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch {
    // ignore
  }
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('checkWorklogRefs — no refs', () => {
  test('returns hasWorklogRefs: false when no worklog refs exist', () => {
    // Clean up any leftover refs from previous runs
    deleteRef('refs/worklog/data');
    deleteRef('refs/worklog/remotes/origin/worklog/data');

    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, false);
    assert.equal(report.refs.length, 0);
  });
});

describe('checkWorklogRefs — remote-tracking mirror refs', () => {
  before(() => {
    // Create only a remote-tracking mirror ref
    createRef('refs/worklog/remotes/origin/worklog/data');
  });

  after(() => {
    deleteRef('refs/worklog/remotes/origin/worklog/data');
  });

  test('ignores refs/worklog/remotes/... mirror refs', () => {
    const report = checkWorklogRefs();
    // Remote-tracking mirrors should NOT trigger the gate
    assert.equal(report.hasWorklogRefs, false);
  });

  test('refs list is empty when only mirrors exist', () => {
    const report = checkWorklogRefs();
    assert.equal(report.refs.length, 0);
  });

  test('message indicates no worklog refs when only mirrors exist', () => {
    const report = checkWorklogRefs();
    assert.ok(report.message.toLowerCase().includes('no worklog ref'));
  });
});

describe('checkWorklogRefs — local orphan ref', () => {
  before(() => {
    // Create the dangerous local orphan ref
    createRef('refs/worklog/data');
  });

  after(() => {
    deleteRef('refs/worklog/data');
  });

  test('detects refs/worklog/data as a worklog ref', () => {
    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, true);
  });

  test('refs list contains refs/worklog/data', () => {
    const report = checkWorklogRefs();
    assert.ok(report.refs.includes('refs/worklog/data'));
  });

  test('message warns about worklog refs', () => {
    const report = checkWorklogRefs();
    assert.ok(report.message.toLowerCase().includes('worklog ref'));
  });
});

describe('checkWorklogRefs — mixed refs', () => {
  before(() => {
    createRef('refs/worklog/data');
    createRef('refs/worklog/remotes/origin/worklog/data');
  });

  after(() => {
    deleteRef('refs/worklog/data');
    deleteRef('refs/worklog/remotes/origin/worklog/data');
  });

  test('detects local ref but ignores mirror refs', () => {
    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, true);
    // Should only contain the local ref, not the mirror
    assert.ok(report.refs.includes('refs/worklog/data'));
    assert.ok(!report.refs.includes('refs/worklog/remotes/origin/worklog/data'));
  });
});
