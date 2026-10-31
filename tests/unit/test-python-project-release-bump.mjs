/**
 * Tests for the release version-bump step on Python-only repos
 * (SA-0MVN2B0DJ00135W0).
 *
 * The canonical release (`skill/ship/scripts/release/merge-dev-to-main.sh`)
 * used to hard-require `package.json`, so a repository whose only version
 * source is `pyproject.toml` could not be released. These tests exercise the
 * real script end-to-end in a throwaway repository (bare `origin`, `dev` and
 * `main` branches) and assert the observable outcome:
 *
 *  - a Python-only repo gets its `[project].version` bumped and committed on
 *    the release branch, and the bump commit touches nothing else;
 *  - the existing `package.json` behaviour is unchanged;
 *  - a repo whose version source cannot be resolved aborts loudly, naming both
 *    candidate files.
 *
 * The full-suite release gate is bypassed (`SHIP_TEST_SKIP=1`) because it would
 * run *this* repository's suite against the throwaway fixture; the gate itself
 * is covered by tests/test_release_full_suite_gate.py.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const RELEASE_SCRIPTS = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release');

const PYPROJECT = `[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "demo-python-service"
version = "0.1.0"
description = "Fixture repo with no package.json"
requires-python = ">=3.11"
`;

const PACKAGE_JSON = `{
  "name": "demo-node-service",
  "version": "0.1.0",
  "private": true
}
`;

function git(cwd, ...args) {
  return spawnSync('git', args, { cwd, encoding: 'utf-8' });
}

/** Copy the release scripts the merge script needs into a fixture repo. */
function installReleaseScripts(repoDir) {
  const releaseDir = join(repoDir, 'skill', 'ship', 'scripts', 'release');
  mkdirSync(releaseDir, { recursive: true });
  for (const name of ['bump-version.js', 'merge-dev-to-main.sh', 'worklog-ref-gate.sh']) {
    copyFileSync(join(RELEASE_SCRIPTS, name), join(releaseDir, name));
  }
  return releaseDir;
}

/**
 * Stub `gh` so the script can create a PR without touching GitHub.
 *
 * The stub lives *outside* the fixture repo: an untracked `bin/` directory
 * inside it would make the release script's clean-worktree check abort.
 */
function installGhStub(workDir) {
  const binDir = join(workDir, 'bin');
  mkdirSync(binDir, { recursive: true });
  const ghPath = join(binDir, 'gh');
  writeFileSync(
    ghPath,
    '#!/bin/bash\nif [ "$1" = "pr" ] && [ "$2" = "create" ]; then\n'
      + '  echo "https://github.com/example/demo/pull/1"\nfi\nexit 0\n',
    'utf-8',
  );
  chmodSync(ghPath, 0o755);
  return binDir;
}

/**
 * Build a throwaway repository with a bare `origin` holding `dev` and `main`.
 *
 * @param {Record<string, string>} files - Files committed to the initial commit.
 * @returns {{dir: string, binDir: string, cleanup: () => void}}
 */
function makeReleaseFixture(files) {
  const workDir = mkdtempSync(join(tmpdir(), 'python-release-fixture-'));
  const repoDir = join(workDir, 'repo');
  const originDir = join(workDir, 'origin.git');
  mkdirSync(repoDir, { recursive: true });

  spawnSync('git', ['init', '--bare', '--initial-branch=main', originDir], { encoding: 'utf-8' });
  git(repoDir, 'init', '-b', 'dev');
  git(repoDir, 'config', 'user.email', 'fixture@example.com');
  git(repoDir, 'config', 'user.name', 'Fixture');
  git(repoDir, 'config', 'commit.gpgsign', 'false');

  // `.worklog/` holds the release Code Freeze marker; ignoring it keeps the
  // merge script's clean-worktree precondition satisfied.
  writeFileSync(join(repoDir, '.gitignore'), '.worklog/\n', 'utf-8');
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(repoDir, name), content, 'utf-8');
  }

  installReleaseScripts(repoDir);

  git(repoDir, 'add', '-A');
  git(repoDir, 'commit', '-m', 'Initial commit');
  git(repoDir, 'remote', 'add', 'origin', originDir);
  git(repoDir, 'push', 'origin', 'dev');
  git(repoDir, 'branch', 'main');
  git(repoDir, 'push', 'origin', 'main');

  return {
    dir: repoDir,
    binDir: installGhStub(workDir),
    cleanup: () => rmSync(workDir, { recursive: true, force: true }),
  };
}

/** Run the canonical merge script inside *repoDir*. */
function runMerge(repoDir, binDir, args = []) {
  return spawnSync(
    'bash',
    [join(repoDir, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh'), ...args],
    {
      cwd: repoDir,
      encoding: 'utf-8',
      timeout: 120_000,
      env: {
        ...process.env,
        PATH: `${binDir}:${process.env.PATH}`,
        // The full-suite gate would run this repository's suite against the
        // fixture repo; it is covered separately (tests/test_release_full_suite_gate.py).
        SHIP_TEST_SKIP: '1',
      },
    },
  );
}

/** The single `release/dev-to-main-*` branch created by the run. */
function releaseBranch(repoDir) {
  const res = git(repoDir, 'branch', '--list', 'release/dev-to-main-*', '--format=%(refname:short)');
  const branches = res.stdout.split('\n').map((b) => b.trim()).filter(Boolean);
  assert.equal(branches.length, 1, `expected exactly one release branch, got: ${branches.join(', ')}`);
  return branches[0];
}

describe('release bump for Python-only repos (pyproject.toml)', () => {
  test('bumps [project].version and commits only that file', () => {
    const { dir, binDir, cleanup } = makeReleaseFixture({ 'pyproject.toml': PYPROJECT });
    try {
      const res = runMerge(dir, binDir);
      assert.equal(res.status, 0, `release should succeed\nstdout: ${res.stdout}\nstderr: ${res.stderr}`);
      assert.match(res.stdout, /Version bumped to v0\.1\.1/);

      const branch = releaseBranch(dir);
      const bumped = git(dir, 'show', `${branch}:pyproject.toml`).stdout;
      assert.match(bumped, /^version = "0\.1\.1"$/m, 'pyproject.toml must carry the bumped version');
      assert.ok(
        bumped.includes('description = "Fixture repo with no package.json"'),
        'unrelated pyproject.toml content must be preserved',
      );

      const bumpCommit = git(dir, 'log', '--format=%H %s', '--', 'pyproject.toml', branch)
        .stdout.trim().split('\n')[0];
      assert.match(bumpCommit, /Bump version to v0\.1\.1/, 'bump commit message');
      const changed = git(dir, 'show', '--name-only', '--pretty=format:', bumpCommit.split(' ')[0])
        .stdout.trim().split('\n').filter(Boolean);
      assert.deepEqual(changed, ['pyproject.toml'], 'the bump commit must touch only pyproject.toml');
    } finally {
      cleanup();
    }
  });

  test('keeps package.json behaviour unchanged when a package.json exists', () => {
    const { dir, binDir, cleanup } = makeReleaseFixture({
      'package.json': PACKAGE_JSON,
      'pyproject.toml': PYPROJECT,
    });
    try {
      const res = runMerge(dir, binDir, ['--bump', 'minor']);
      assert.equal(res.status, 0, `release should succeed\nstdout: ${res.stdout}\nstderr: ${res.stderr}`);
      assert.match(res.stdout, /Version bumped to v0\.2\.0/);

      const branch = releaseBranch(dir);
      const pkg = JSON.parse(git(dir, 'show', `${branch}:package.json`).stdout);
      assert.equal(pkg.version, '0.2.0');
      const pyproject = git(dir, 'show', `${branch}:pyproject.toml`).stdout;
      assert.match(pyproject, /^version = "0\.1\.0"$/m, 'pyproject.toml must be left alone');
    } finally {
      cleanup();
    }
  });

  test('aborts with an actionable message when no version source declares a version', () => {
    const dynamicPyproject = '[project]\nname = "demo"\ndynamic = ["version"]\n';
    const { dir, binDir, cleanup } = makeReleaseFixture({ 'pyproject.toml': dynamicPyproject });
    try {
      const res = runMerge(dir, binDir);
      assert.notEqual(res.status, 0, 'release must abort');
      const combined = `${res.stdout}\n${res.stderr}`;
      assert.match(combined, /Version bump failed/);
      assert.match(combined, /pyproject\.toml/);
      assert.match(combined, /package\.json/);
      assert.match(combined, /dynamic/, 'the cause must be surfaced, not swallowed');
      assert.ok(
        !existsSync(join(dir, '.worklog', 'code-freeze.json')),
        'the Code Freeze marker must be cleared on abort',
      );
    } finally {
      cleanup();
    }
  });
});
