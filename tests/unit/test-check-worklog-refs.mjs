/**
 * Unit tests for check-worklog-refs.js
 *
 * Validates that the worklog-ref gate ignores remote-tracking mirror refs
 * (refs/worklog/remotes/...) and any refs/worklog/* ref that carries the
 * worklog data file (wl-sync bookkeeping), while still detecting the
 * dangerous orphan refs that do NOT contain the data file.
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

describe('checkWorklogRefs — data-bearing sync refs', () => {
  // `wl sync` maintains several bookkeeping refs under refs/worklog/ that
  // carry the worklog data file, exactly like refs/worklog/data (e.g.
  // refs/worklog/fetched-check, refs/worklog/tmp-reauthor). They are worklog
  // DATA, never code branches, and must not trip the gate
  // (SA-0MVMD7NTC0086HWK).
  //
  // The test refs are deliberately NOT named after the ambient bookkeeping
  // refs: those may exist on a developer machine, and deleting them in
  // cleanup would mutate the operator's repository state.
  const DATA_REF = 'refs/worklog/test-fetched-check';
  const DATA_REF_2 = 'refs/worklog/test-tmp-reauthor';
  const ORPHAN_REF = 'refs/worklog/test-orphan-check';

  /**
   * Create a commit that contains `.worklog/worklog-data.jsonl`, mirroring
   * what `wl sync` pushes into its bookkeeping refs.
   */
  function dataBearingCommit() {
    const blob = execSync('git hash-object -w --stdin', {
      input: '{}\n',
      encoding: 'utf-8',
    }).trim();
    const worklogTree = execSync('git mktree', {
      input: `100644 blob ${blob}\tworklog-data.jsonl\n`,
      encoding: 'utf-8',
    }).trim();
    const rootTree = execSync('git mktree', {
      input: `040000 tree ${worklogTree}\t.worklog\n`,
      encoding: 'utf-8',
    }).trim();
    return execSync(`git commit-tree ${rootTree} -m "worklog data"`, {
      encoding: 'utf-8',
    }).trim();
  }

  after(() => {
    deleteRef(DATA_REF);
    deleteRef(DATA_REF_2);
    deleteRef(ORPHAN_REF);
  });

  test('ignores a fetched-check style ref holding the data file', () => {
    createRef(DATA_REF, dataBearingCommit());
    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, false);
    assert.equal(report.refs.length, 0);
  });

  test('ignores a tmp-reauthor style ref holding the data file', () => {
    createRef(DATA_REF_2, dataBearingCommit());
    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, false);
    assert.equal(report.refs.length, 0);
  });

  test('still flags a refs/worklog ref that does NOT hold the data file', () => {
    // An empty tree has no .worklog/worklog-data.jsonl → a true orphan.
    const tree = execSync('git mktree', { input: '', encoding: 'utf-8' }).trim();
    const commit = execSync(
      `git commit-tree ${tree} -m orphan`, { encoding: 'utf-8' },
    ).trim();
    createRef(ORPHAN_REF, commit);
    const report = checkWorklogRefs();
    assert.equal(report.hasWorklogRefs, true);
    assert.ok(report.refs.includes(ORPHAN_REF));
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
