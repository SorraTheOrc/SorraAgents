/**
 * Unit/integration tests for the shell worklog-ref gate
 * (skill/ship/scripts/release/worklog-ref-gate.sh) — SA-0MVO7XZAB0095R6F.
 *
 * The gate used to be inlined in merge-dev-to-main.sh and only special-cased
 * the literal ref name `refs/worklog/data`, so `wl sync` bookkeeping refs
 * (`refs/worklog/fetched-check`, `refs/worklog/tmp-reauthor`) — all of which
 * carry the worklog data file — aborted every release even though the JS gate
 * (check-worklog-refs.js) passed. These tests exercise the real gate logic
 * against throwaway repositories (never the live checkout) and assert that the
 * bash and JS implementations reach the SAME verdict on the same repo state.
 *
 * Coverage:
 *   - AC1: bookkeeping refs carrying .worklog/worklog-data.jsonl pass
 *          (data, fetched-check, tmp-reauthor, and an arbitrary new name).
 *   - AC2: an orphan ref without the data file still aborts the gate.
 *   - AC3: refs/worklog/remotes/** mirror refs are excluded.
 *   - AC4: parity with check-worklog-refs.js on every scenario above, plus a
 *          merge-script integration check proving merge-dev-to-main.sh aborts
 *          on a dangerous ref and does NOT abort on bookkeeping refs.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, execSync, spawnSync } from 'node:child_process';
import { copyFileSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, '..', '..');
const GATE = join(REPO_ROOT, 'skill/ship/scripts/release/worklog-ref-gate.sh');
const MERGE_SCRIPT = join(
  REPO_ROOT,
  'skill/ship/scripts/release/merge-dev-to-main.sh',
);
const JS_GATE = join(REPO_ROOT, 'skill/ship/scripts/check-worklog-refs.js');

const DATA_FILE = '.worklog/worklog-data.jsonl';

/** Deterministic git identity so scratch repos never depend on global config. */
const GIT_ENV = {
  ...process.env,
  GIT_AUTHOR_NAME: 'Ref Gate Test',
  GIT_AUTHOR_EMAIL: 'ref-gate@example.invalid',
  GIT_COMMITTER_NAME: 'Ref Gate Test',
  GIT_COMMITTER_EMAIL: 'ref-gate@example.invalid',
  GIT_CONFIG_GLOBAL: '/dev/null',
  GIT_CONFIG_SYSTEM: '/dev/null',
};

/** Create a throwaway repo with a data-file commit and a data-free commit. */
function makeScratchRepo() {
  const dir = mkdtempSync(join(tmpdir(), 'wl-ref-gate-'));
  const git = (...args) =>
    execFileSync('git', args, { cwd: dir, env: GIT_ENV, encoding: 'utf-8' });
  const revParse = (rev) => git('rev-parse', rev).trim();

  git('init', '-q', '-b', 'main');
  // Base commit: no worklog data file.
  git('commit', '-q', '--allow-empty', '-m', 'base');
  const baseCommit = revParse('HEAD');

  // Second commit: introduces the worklog data file (sync bookkeeping).
  mkdirSync(join(dir, '.worklog'), { recursive: true });
  writeFileSync(join(dir, DATA_FILE), '{"id":"SA-TEST-DATA"}\n');
  git('add', DATA_FILE);
  git('commit', '-q', '-m', 'worklog data');
  const dataCommit = revParse('HEAD');

  // Mirror the live checkout: the release-time Code Freeze marker is ignored,
  // so setting it does not make the tree dirty for the merge script's gate.
  writeFileSync(join(dir, '.gitignore'), '.worklog/code-freeze.json\n');
  git('add', '.gitignore');
  git('commit', '-q', '-m', 'ignore code-freeze marker');

  return { dir, git, baseCommit, dataCommit };
}

/** Run the extracted bash gate against a repo. */
function runShellGate(repoPath) {
  const result = spawnSync('bash', [GATE, repoPath], {
    encoding: 'utf-8',
    env: GIT_ENV,
  });
  return {
    status: result.status,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
  };
}

/**
 * Run the JS gate (check-worklog-refs.js) against a repo in a subprocess, so
 * its `git` calls resolve in that repo rather than the live checkout.
 */
function runJsGate(repoPath) {
  const script = `
    import { checkWorklogRefs } from ${JSON.stringify(JS_GATE)};
    const report = checkWorklogRefs();
    process.stdout.write(JSON.stringify({ hasWorklogRefs: report.hasWorklogRefs, refs: report.refs }));
  `;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    cwd: repoPath,
    encoding: 'utf-8',
    env: GIT_ENV,
  });
  assert.equal(
    result.status,
    0,
    `JS gate subprocess failed: ${result.stderr || result.stdout}`,
  );
  return JSON.parse(result.stdout);
}

describe('worklog-ref-gate.sh — worklog data bookkeeping refs (AC1)', () => {
  let repo;

  beforeEach(() => {
    repo = makeScratchRepo();
  });

  afterEach(() => {
    rmSync(repo.dir, { recursive: true, force: true });
  });

  test('passes when only wl-sync bookkeeping refs carrying the data file exist', () => {
    // The three names `wl sync` currently maintains, all pointing at the
    // commit that contains .worklog/worklog-data.jsonl.
    repo.git('update-ref', 'refs/worklog/data', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/fetched-check', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/tmp-reauthor', repo.dataCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 0, `expected clean pass, got stderr: ${gate.stderr}`);
    assert.doesNotMatch(gate.stderr, /^ {2}- /m);
  });

  test('passes for a future bookkeeping ref name, not just the known three', () => {
    // The rule is content-based, so a new wl-sync ref must not require a code
    // change here.
    repo.git('update-ref', 'refs/worklog/some-future-bookkeeping-ref', repo.dataCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 0, `expected clean pass, got stderr: ${gate.stderr}`);
  });

  test('passes when no worklog refs exist at all', () => {
    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 0);
    assert.equal(gate.stderr.trim(), '');
  });
});

describe('worklog-ref-gate.sh — orphan refs (AC2)', () => {
  let repo;

  beforeEach(() => {
    repo = makeScratchRepo();
  });

  afterEach(() => {
    rmSync(repo.dir, { recursive: true, force: true });
  });

  test('aborts and names an orphan ref that does not carry the data file', () => {
    repo.git('update-ref', 'refs/worklog/orphan', repo.baseCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 1, 'gate must fail closed on a dangerous ref');
    assert.match(gate.stderr, /refs\/worklog\/orphan/);
  });

  test('aborts when a dangerous orphan accompanies legitimate bookkeeping refs', () => {
    repo.git('update-ref', 'refs/worklog/fetched-check', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/orphan', repo.baseCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 1);
    assert.match(gate.stderr, /refs\/worklog\/orphan/);
    assert.doesNotMatch(gate.stderr, /refs\/worklog\/fetched-check/);
  });

  test('aborts when a data-file ref and an orphan share the data-less commit', () => {
    // Bookkeeping ref on the data commit passes; orphan on the base commit
    // alone must still be reported (no masking by a sibling ref).
    repo.git('update-ref', 'refs/worklog/data', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/orphan', repo.baseCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 1);
    assert.match(gate.stderr, /refs\/worklog\/orphan/);
  });
});

describe('worklog-ref-gate.sh — remote-tracking mirrors (AC3)', () => {
  let repo;

  beforeEach(() => {
    repo = makeScratchRepo();
  });

  afterEach(() => {
    rmSync(repo.dir, { recursive: true, force: true });
  });

  test('excludes refs/worklog/remotes/** even when the ref has no data file', () => {
    repo.git('update-ref', 'refs/worklog/remotes/origin/worklog/data', repo.baseCommit);

    const gate = runShellGate(repo.dir);

    assert.equal(gate.status, 0, `mirror refs must be excluded: ${gate.stderr}`);
  });
});

describe('worklog-ref-gate.sh — usage errors', () => {
  test('rejects a path that is not a git repository', () => {
    const dir = mkdtempSync(join(tmpdir(), 'wl-ref-gate-norepo-'));
    try {
      const gate = runShellGate(dir);
      assert.equal(gate.status, 2);
      assert.match(gate.stderr, /not a git repository/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test('rejects more than one argument', () => {
    const result = spawnSync('bash', [GATE, '.', './extra'], {
      encoding: 'utf-8',
      env: GIT_ENV,
    });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /at most one argument/);
  });
});

describe('bash and JS gates agree (AC4 — divergence guard)', () => {
  let repo;

  beforeEach(() => {
    repo = makeScratchRepo();
  });

  afterEach(() => {
    rmSync(repo.dir, { recursive: true, force: true });
  });

  /**
   * Assert both implementations reach the same verdict for the current state of
   * the scratch repo. This is the regression guard for the original bug: the
   * bash gate blocked while the JS gate allowed the same repo.
   */
  function assertParity(expectedDangerous) {
    const shell = runShellGate(repo.dir);
    const js = runJsGate(repo.dir);

    assert.equal(
      shell.status === 1,
      js.hasWorklogRefs,
      `gate divergence: bash(status=${shell.status}) vs js(hasWorklogRefs=${js.hasWorklogRefs}); ` +
        `stderr: ${shell.stderr}`,
    );
    assert.equal(js.hasWorklogRefs, expectedDangerous);
  }

  test('parity: only bookkeeping refs → both allow the release', () => {
    repo.git('update-ref', 'refs/worklog/data', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/fetched-check', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/tmp-reauthor', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/remotes/origin/worklog/data', repo.baseCommit);

    assertParity(false);
  });

  test('parity: dangerous orphan present → both block the release', () => {
    repo.git('update-ref', 'refs/worklog/data', repo.dataCommit);
    repo.git('update-ref', 'refs/worklog/orphan', repo.baseCommit);

    assertParity(true);
  });

  test('parity: no worklog refs → both allow the release', () => {
    assertParity(false);
  });
});

describe('merge-dev-to-main.sh — gate integration (AC1/AC2)', () => {
  const hasGh = spawnSync('gh', ['--version'], { encoding: 'utf-8' }).status === 0;

  function runMergeScript(repoDir, scriptPath = MERGE_SCRIPT) {
    // No remote is configured, so a script that gets PAST the worklog-ref gate
    // fails later at `git fetch`; a script that is BLOCKED exits at the gate.
    return spawnSync('bash', [scriptPath, '--work-item-id', 'SA-TEST'], {
      cwd: repoDir,
      encoding: 'utf-8',
      env: { ...GIT_ENV, WORKLOG_SKIP_PRE_PUSH: '1' },
      timeout: 60_000,
    });
  }

  test('fails closed with an actionable message when the gate script is missing', () => {
    if (!hasGh) return; // gh is a documented script prerequisite
    const repo = makeScratchRepo();
    // A partial install/copy: the release script without its sibling gate,
    // kept OUTSIDE the repo so the fixture tree stays clean.
    const loneDir = mkdtempSync(join(tmpdir(), 'wl-lone-script-'));
    const lone = join(loneDir, 'merge-dev-to-main.sh');
    copyFileSync(MERGE_SCRIPT, lone);
    try {
      const result = runMergeScript(repo.dir, lone);

      assert.notEqual(result.status, 0, 'a missing gate must never let the release proceed');
      assert.match(result.stderr, /worklog-ref gate not found/);
      assert.match(result.stderr, /worklog-ref-gate\.sh/, 'the message must name the missing script');
    } finally {
      rmSync(repo.dir, { recursive: true, force: true });
      rmSync(loneDir, { recursive: true, force: true });
    }
  });

  test('aborts at the gate when a dangerous worklog ref exists', () => {
    if (!hasGh) return; // gh is a documented script prerequisite
    const repo = makeScratchRepo();
    try {
      repo.git('update-ref', 'refs/worklog/orphan', repo.baseCommit);

      const result = runMergeScript(repo.dir);

      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /Dangerous worklog refs detected/);
    } finally {
      rmSync(repo.dir, { recursive: true, force: true });
    }
  });

  test('does not abort at the gate for wl-sync bookkeeping refs', () => {
    if (!hasGh) return; // gh is a documented script prerequisite
    const repo = makeScratchRepo();
    try {
      repo.git('update-ref', 'refs/worklog/data', repo.dataCommit);
      repo.git('update-ref', 'refs/worklog/fetched-check', repo.dataCommit);
      repo.git('update-ref', 'refs/worklog/tmp-reauthor', repo.dataCommit);

      const result = runMergeScript(repo.dir);

      // It may fail for other reasons (no remote to fetch), but never at the
      // worklog-ref gate.
      assert.doesNotMatch(result.stderr, /Dangerous worklog refs detected/);
    } finally {
      rmSync(repo.dir, { recursive: true, force: true });
    }
  });
});
