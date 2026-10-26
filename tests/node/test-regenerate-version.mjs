/**
 * Tests for regenerate-version.js — post-bump derived version file
 * regeneration for the ship release flow (SA-0MV7WAUHZ009UWY2).
 *
 * Covers the ACs of the ship-flow fix:
 *  - AC1: a repo with a `build` script regenerates its tracked derived
 *    version file and commits the result.
 *  - AC2: a failing build is non-fatal — the function reports failure, logs a
 *    warning, and never aborts the release (the CLI still exits 0).
 *  - AC3: no `build` script → no-op (repos without a build are unaffected).
 *  - Tracked files only: build droppings (untracked `dist/` artefacts) are
 *    never staged.
 *
 * The build runner is injectable (`runBuild`) for deterministic, fast unit
 * tests; a small number of CLI-level tests exercise the real
 * `npm run build` → `git add -u` → commit path in a throwaway fixture repo.
 * No network, no live worklog, no writes outside the temp fixtures.
 */

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, rmSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPT_PATH = join(
  __dirname, '..', '..', 'skill', 'ship', 'scripts', 'release', 'regenerate-version.js',
);

// Import under test. Registry-free, side-effect light (resolves git root).
const { regenerateDerivedFiles, readBuildScript } = await import(SCRIPT_PATH);

// ── Fixture helpers ─────────────────────────────────────────────────────────

/** Run a git command in *dir*, throwing on a non-zero exit. */
function git(dir, args) {
  const res = spawnSync('git', args, { cwd: dir, encoding: 'utf-8' });
  if (res.status !== 0) {
    throw new Error(`git ${args.join(' ')} failed: ${res.stderr || res.stdout}`);
  }
  return res.stdout.trim();
}

/**
 * Run a git command with the file transport protocol allow-listed.
 *
 * Local-path submodule operations (clone/fetch/update) refuse the `file`
 * protocol by default; this mirrors the main-street engine-sync fixtures by
 * allowing it explicitly so the fixture stays fully network-free.
 */
function gitFile(dir, args) {
  const res = spawnSync('git', ['-c', 'protocol.file.allow=always', ...args], {
    cwd: dir,
    encoding: 'utf-8',
    env: { ...process.env, GIT_ALLOW_PROTOCOL: 'file', GIT_TERMINAL_PROMPT: '0' },
  });
  if (res.status !== 0) {
    throw new Error(`git ${args.join(' ')} failed: ${res.stderr || res.stdout}`);
  }
  return res.stdout.trim();
}

/**
 * Build a throwaway git repository containing a tracked derived version file
 * and (optionally) a build script.
 *
 * @param {{version:string, buildScript?:string|null, derivedVersion?:string}} opts
 * @returns {string} fixture repo path
 */
function mkFixtureRepo({ version, buildScript = null, derivedVersion = '0.0.0' } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'regen-version-'));

  git(dir, ['init', '-q']);
  git(dir, ['config', 'user.name', 'Test']);
  git(dir, ['config', 'user.email', 'test@example.com']);
  git(dir, ['config', 'commit.gpgsign', 'false']);

  const pkg = { name: 'fixture', version, type: 'module' };
  if (buildScript) pkg.scripts = { build: buildScript };
  writeFileSync(join(dir, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');

  // The derived file tracks the package.json version, as ContextHub's
  // src/version.ts does.
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(
    join(dir, 'src', 'version.ts'),
    `export const WORKLOG_VERSION = '${derivedVersion}';\n`,
  );

  git(dir, ['add', '-A']);
  git(dir, ['commit', '-q', '-m', 'initial']);

  return dir;
}

/** A build runner that regenerates the derived file from package.json. */
function writeDerivedVersion(dir) {
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf-8'));
  writeFileSync(
    join(dir, 'src', 'version.ts'),
    `export const WORKLOG_VERSION = '${pkg.version}';\n`,
  );
}

/**
 * Build a throwaway fixture repo whose `core` submodule points at a
 * local-path engine repo, mirroring main-street's engine layout (submodule
 * tracked on `dev`, gitlink recorded in the superproject). Carries the
 * standard derived-version files so regeneration can be exercised alongside
 * a drifting submodule pointer.
 *
 * @param {{version:string, buildScript?:string, derivedVersion?:string}} opts
 * @returns {{root:string, game:string, engine:string, gitlink:() => string,
 *   advanceEngine:() => void, dirtyCore:() => void}}
 */
function mkSubmoduleFixtureRepo({ version, buildScript = 'node build.js', derivedVersion = '0.0.0' } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'regen-gitlink-'));
  const engine = join(root, 'engine');
  const game = join(root, 'game');
  mkdirSync(engine, { recursive: true });
  mkdirSync(game, { recursive: true });

  // Engine repo on `dev` (Tableau-Card-Engine's active branch).
  gitFile(engine, ['init', '-q', '-b', 'dev']);
  gitFile(engine, ['config', 'user.email', 'fixture@test']);
  gitFile(engine, ['config', 'user.name', 'Fixture']);
  writeFileSync(join(engine, 'version.txt'), 'v1\n');
  gitFile(engine, ['add', 'version.txt']);
  gitFile(engine, ['commit', '-q', '-m', 'engine v1']);

  // Game repo with the `core` submodule pinned to the engine's first commit.
  gitFile(game, ['init', '-q', '-b', 'main']);
  gitFile(game, ['config', 'user.email', 'fixture@test']);
  gitFile(game, ['config', 'user.name', 'Fixture']);
  gitFile(game, ['submodule', 'add', engine, 'core']);
  gitFile(game, ['config', '-f', '.gitmodules', 'submodule.core.branch', 'dev']);

  // Derived-version files alongside the submodule.
  const pkg = { name: 'fixture', version, type: 'module' };
  if (buildScript) pkg.scripts = { build: buildScript };
  writeFileSync(join(game, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');
  mkdirSync(join(game, 'src'), { recursive: true });
  writeFileSync(
    join(game, 'src', 'version.ts'),
    `export const WORKLOG_VERSION = '${derivedVersion}';\n`,
  );

  gitFile(game, ['add', '-A']);
  gitFile(game, ['commit', '-q', '-m', 'init']);

  /** The gitlink (mode 160000 entry) recorded for `core` in the index. */
  const gitlink = () => gitFile(game, ['ls-files', '-s', 'core']).split(/\s+/)[1] ?? '';

  /** Advance the engine tip and move the worktree `core` checkout after it. */
  const advanceEngine = () => {
    writeFileSync(join(engine, 'version.txt'), `v${Date.now()}\n`);
    gitFile(engine, ['add', 'version.txt']);
    gitFile(engine, ['commit', '-q', '-m', 'advance']);
    gitFile(join(game, 'core'), ['fetch', 'origin']);
    gitFile(join(game, 'core'), ['checkout', '-q', 'FETCH_HEAD']);
  };

  /** Leave an uncommitted change inside the `core` checkout. */
  const dirtyCore = () => {
    const p = join(game, 'core', 'version.txt');
    writeFileSync(p, readFileSync(p, 'utf-8') + 'wip\n');
  };

  return { root, game, engine, gitlink, advanceEngine, dirtyCore };
}

/** Collect log calls into a simple record for assertions. */
function makeLogger() {
  const logs = { info: [], warn: [], error: [] };
  return {
    logger: {
      log: (...a) => logs.info.push(a.join(' ')),
      info: (...a) => logs.info.push(a.join(' ')),
      warn: (...a) => logs.warn.push(a.join(' ')),
      error: (...a) => logs.error.push(a.join(' ')),
    },
    logs,
  };
}

const fixtures = [];
after(() => {
  for (const dir of fixtures) rmSync(dir, { recursive: true, force: true });
});

// ── readBuildScript ─────────────────────────────────────────────────────────

describe('readBuildScript', () => {
  it('returns the build script string when present', () => {
    const dir = mkFixtureRepo({ version: '1.0.0', buildScript: 'node build.js' });
    fixtures.push(dir);
    assert.equal(readBuildScript(join(dir, 'package.json')), 'node build.js');
  });

  it('returns null when package.json has no build script', () => {
    const dir = mkFixtureRepo({ version: '1.0.0' });
    fixtures.push(dir);
    assert.equal(readBuildScript(join(dir, 'package.json')), null);
  });

  it('returns null when package.json is missing or malformed', () => {
    const dir = mkdtempSync(join(tmpdir(), 'regen-nopkg-'));
    fixtures.push(dir);
    assert.equal(readBuildScript(join(dir, 'package.json')), null);
    writeFileSync(join(dir, 'package.json'), '{ not json');
    assert.equal(readBuildScript(join(dir, 'package.json')), null);
  });

  it('returns null for an empty/whitespace build script', () => {
    const dir = mkdtempSync(join(tmpdir(), 'regen-emptybuild-'));
    fixtures.push(dir);
    writeFileSync(join(dir, 'package.json'), JSON.stringify({ version: '1.0.0', scripts: { build: '   ' } }));
    assert.equal(readBuildScript(join(dir, 'package.json')), null);
  });
});

// ── regenerateDerivedFiles — no build script (AC3) ──────────────────────────

describe('regenerateDerivedFiles — repos without a build script', () => {
  it('is a no-op (no build, no commit) when scripts.build is absent', () => {
    const dir = mkFixtureRepo({ version: '1.0.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    let buildCalled = false;
    const { logger, logs } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '1.0.1',
      runBuild: () => { buildCalled = true; writeDerivedVersion(dir); },
      logger,
    });

    assert.deepEqual(result, { ran: false, success: true, committed: false, reason: 'no build script' });
    assert.equal(buildCalled, false, 'the build must not run without a build script');
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before, 'no commit should be created');
    assert.equal(logs.error.length, 0);
  });
});

// ── regenerateDerivedFiles — success path (AC1) ─────────────────────────────

describe('regenerateDerivedFiles — success path', () => {
  it('regenerates and commits the tracked derived version file', () => {
    const dir = mkFixtureRepo({ version: '1.2.3', buildScript: 'node build.js', derivedVersion: '1.0.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    const { logger, logs } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '1.2.3',
      runBuild: () => writeDerivedVersion(dir),
      logger,
    });

    assert.equal(result.ran, true);
    assert.equal(result.success, true);
    assert.equal(result.committed, true);
    assert.deepEqual(result.files, ['src/version.ts']);

    // Derived version now matches package.json.
    assert.match(readFileSync(join(dir, 'src', 'version.ts'), 'utf-8'), /1\.2\.3/);

    // A new commit exists with the expected subject.
    const after = git(dir, ['rev-parse', 'HEAD']);
    assert.notEqual(after, before, 'a regeneration commit must be created');
    assert.match(git(dir, ['log', '-1', '--format=%s']), /Regenerate derived version files for v1\.2\.3/);
    assert.equal(logs.error.length, 0);
  });

  it('omits the version from the commit subject when version is not supplied', () => {
    const dir = mkFixtureRepo({ version: '2.0.0', buildScript: 'node build.js', derivedVersion: '1.0.0' });
    fixtures.push(dir);
    const { logger } = makeLogger();

    regenerateDerivedFiles({
      repoRoot: dir,
      runBuild: () => writeDerivedVersion(dir),
      logger,
    });

    assert.match(git(dir, ['log', '-1', '--format=%s']), /Regenerate derived version files$/);
  });

  it('stages tracked changes only — untracked build droppings are left alone', () => {
    const dir = mkFixtureRepo({ version: '3.1.4', buildScript: 'node build.js', derivedVersion: '1.0.0' });
    fixtures.push(dir);
    const { logger } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '3.1.4',
      runBuild: () => {
        writeDerivedVersion(dir);
        // Build droppings: an untracked artefact (like dist/).
        mkdirSync(join(dir, 'dist'), { recursive: true });
        writeFileSync(join(dir, 'dist', 'bundle.js'), '// generated\n');
      },
      logger,
    });

    assert.equal(result.committed, true);
    // The untracked artefact must not be part of the commit.
    const committedFiles = git(dir, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committedFiles, ['src/version.ts']);
    assert.equal(existsSync(join(dir, 'dist', 'bundle.js')), true, 'the artefact should remain on disk, untracked');
  });

  it('creates no commit when the build makes no tracked changes', () => {
    const dir = mkFixtureRepo({ version: '1.0.0', buildScript: 'node build.js', derivedVersion: '1.0.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    const { logger } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '1.0.0',
      runBuild: () => {}, // build succeeds but nothing changes
      logger,
    });

    assert.deepEqual(result, { ran: true, success: true, committed: false, reason: 'no changes' });
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before, 'no commit when there is nothing to commit');
  });
});

// ── regenerateDerivedFiles — failure path (AC2) ─────────────────────────────

describe('regenerateDerivedFiles — failing build is non-fatal', () => {
  it('reports failure, logs a warning and makes no commit', () => {
    const dir = mkFixtureRepo({ version: '1.0.0', buildScript: 'node build.js', derivedVersion: '0.9.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    const beforeContent = readFileSync(join(dir, 'src', 'version.ts'), 'utf-8');
    const { logger, logs } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '1.0.0',
      runBuild: () => { throw new Error('build exploded'); },
      logger,
    });

    assert.equal(result.ran, true);
    assert.equal(result.success, false);
    assert.equal(result.committed, false);
    assert.match(result.reason, /build exploded/);

    // No commit and the derived file is untouched.
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before, 'a failed build must not commit');
    assert.equal(readFileSync(join(dir, 'src', 'version.ts'), 'utf-8'), beforeContent);

    // The failure is surfaced as a warning, not an abortion.
    assert.equal(logs.warn.length + logs.error.length, 1);
    assert.match(logs.warn.concat(logs.error)[0], /Warning: derived version file regeneration failed/);
  });
});

// ── regenerateDerivedFiles — submodule pointer (gitlink) exclusion ──────────
//
// A consumer build may advance a submodule checkout (main-street's
// `engine:sync` does exactly this), so `git add -u` would otherwise stage the
// gitlink and the derived-files commit would silently bump the submodule
// pointer. The regeneration step must never stage submodule pointers.

describe('regenerateDerivedFiles — submodule pointer (gitlink) exclusion', () => {
  it('commits only the regenerated file, never an advanced submodule gitlink', () => {
    const f = mkSubmoduleFixtureRepo({ version: '5.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    const { logger } = makeLogger();

    f.advanceEngine(); // worktree `core` now differs from the recorded gitlink

    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '5.0.0',
      runBuild: () => writeDerivedVersion(f.game),
      logger,
    });

    assert.equal(result.committed, true);
    assert.deepEqual(result.files, ['src/version.ts']);
    const committedFiles = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committedFiles, ['src/version.ts'], 'the derived-files commit must not contain a gitlink bump');
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded submodule pointer is unchanged');
  });

  it('creates no commit when the submodule bump is the only change', () => {
    const f = mkSubmoduleFixtureRepo({ version: '6.0.0', derivedVersion: '6.0.0' });
    fixtures.push(f.root);
    f.advanceEngine();
    const before = git(f.game, ['rev-parse', 'HEAD']);
    const { logger } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '6.0.0',
      runBuild: () => {}, // build succeeds; only the submodule checkout moved
      logger,
    });

    assert.deepEqual(result, { ran: true, success: true, committed: false, reason: 'no changes' });
    assert.equal(git(f.game, ['rev-parse', 'HEAD']), before, 'no commit when only the gitlink moved');
  });

  it('keeps a dirty submodule working tree out of the derived-files commit', () => {
    const f = mkSubmoduleFixtureRepo({ version: '7.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    f.advanceEngine();
    f.dirtyCore();
    const { logger } = makeLogger();

    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '7.0.0',
      runBuild: () => writeDerivedVersion(f.game),
      logger,
    });

    assert.equal(result.committed, true);
    const committedFiles = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committedFiles, ['src/version.ts']);
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded submodule pointer is unchanged');
    assert.equal(
      readFileSync(join(f.game, 'core', 'version.txt'), 'utf-8').endsWith('wip\n'),
      true,
      'uncommitted submodule content stays on disk, untracked by the commit',
    );
  });
});

// ── CLI-level tests (real npm run build) ────────────────────────────────────

/** Spawn the regeneration CLI in *cwd*. */
function runCli(cwd, args) {
  return spawnSync('node', [SCRIPT_PATH, ...args], { cwd, encoding: 'utf-8' });
}

describe('regenerate-version.js CLI (real npm run build)', () => {
  it('regenerates and commits in a fixture repo and exits 0', () => {
    const dir = mkFixtureRepo({
      version: '1.4.0',
      buildScript: 'node build.mjs',
      derivedVersion: '1.0.0',
    });
    fixtures.push(dir);
    writeFileSync(
      join(dir, 'build.mjs'),
      [
        "import { readFileSync, writeFileSync } from 'node:fs';",
        "const pkg = JSON.parse(readFileSync('package.json', 'utf-8'));",
        "writeFileSync('src/version.ts', `export const WORKLOG_VERSION = '${pkg.version}';\\n`);",
        '',
      ].join('\n'),
    );
    git(dir, ['add', '-A']);
    git(dir, ['commit', '-q', '-m', 'add build script']);

    const res = runCli(dir, ['1.4.0']);
    assert.equal(res.status, 0, `CLI should exit 0 (stderr: ${res.stderr})`);
    assert.match(readFileSync(join(dir, 'src', 'version.ts'), 'utf-8'), /1\.4\.0/);
    assert.match(git(dir, ['log', '-1', '--format=%s']), /Regenerate derived version files for v1\.4\.0/);
  });

  it('exits 0 and warns when the real build script fails (non-fatal)', () => {
    const dir = mkFixtureRepo({
      version: '1.4.0',
      buildScript: 'node build.mjs',
      derivedVersion: '1.0.0',
    });
    fixtures.push(dir);
    writeFileSync(join(dir, 'build.mjs'), 'process.exit(1);\n');
    git(dir, ['add', '-A']);
    git(dir, ['commit', '-q', '-m', 'add failing build script']);
    const before = git(dir, ['rev-parse', 'HEAD']);

    const res = runCli(dir, ['1.4.0']);
    assert.equal(res.status, 0, 'a failing build must never fail the release step');
    assert.match(res.stderr, /Warning: derived version file regeneration failed/);
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before, 'no commit on build failure');
  });

  it('is a no-op and exits 0 when there is no build script', () => {
    const dir = mkFixtureRepo({ version: '1.4.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);

    const res = runCli(dir, ['1.4.0']);
    assert.equal(res.status, 0);
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before);
  });
});
