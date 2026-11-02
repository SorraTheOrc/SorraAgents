/**
 * Tests for regenerate-version.js (SA-0MV7WAUHZ009UWY2) — mirrored,
 * skill-local copy of tests/node/test-regenerate-version.mjs.
 *
 * The ship skill ships its own concise test copies so the skill can be
 * validated standalone (CI runs the authoritative copy under tests/node/).
 * Covers the three observable paths of the derived-version regeneration step:
 * no build script (no-op), successful regeneration + commit, and a failing
 * build (non-fatal).
 *
 * Build runner injected via `runBuild`; fixtures are throwaway git repos in
 * tmpdir. No network, no live worklog.
 */

import { describe, test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const MODULE_PATH = join(dirname(__filename), '..', 'scripts', 'release', 'regenerate-version.js');

const { regenerateDerivedFiles } = await import(MODULE_PATH);

function git(dir, args) {
  const res = spawnSync('git', args, { cwd: dir, encoding: 'utf-8' });
  if (res.status !== 0) throw new Error(`git ${args.join(' ')} failed: ${res.stderr}`);
  return res.stdout.trim();
}

/** Run a git command with the file transport protocol allow-listed. */
function gitFile(dir, args) {
  const res = spawnSync('git', ['-c', 'protocol.file.allow=always', ...args], {
    cwd: dir,
    encoding: 'utf-8',
    env: { ...process.env, GIT_ALLOW_PROTOCOL: 'file', GIT_TERMINAL_PROMPT: '0' },
  });
  if (res.status !== 0) throw new Error(`git ${args.join(' ')} failed: ${res.stderr}`);
  return res.stdout.trim();
}

function mkFixtureRepo({ version, buildScript = null, derivedVersion = '0.0.0' }) {
  const dir = mkdtempSync(join(tmpdir(), 'regen-skill-'));
  git(dir, ['init', '-q']);
  git(dir, ['config', 'user.name', 'Test']);
  git(dir, ['config', 'user.email', 'test@example.com']);
  git(dir, ['config', 'commit.gpgsign', 'false']);

  const pkg = { name: 'fixture', version, type: 'module' };
  if (buildScript) pkg.scripts = { build: buildScript };
  writeFileSync(join(dir, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');
  mkdirSync(join(dir, 'src'), { recursive: true });
  writeFileSync(join(dir, 'src', 'version.ts'), `export const WORKLOG_VERSION = '${derivedVersion}';\n`);
  git(dir, ['add', '-A']);
  git(dir, ['commit', '-q', '-m', 'initial']);
  return dir;
}

function writeDerivedVersion(dir) {
  const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf-8'));
  writeFileSync(join(dir, 'src', 'version.ts'), `export const WORKLOG_VERSION = '${pkg.version}';\n`);
}

/**
 * Throwaway fixture repo with a local-path `core` submodule (mirrors
 * main-street's engine layout) plus the standard derived-version files.
 */
function mkSubmoduleFixtureRepo({ version, buildScript = 'node build.js', derivedVersion = '0.0.0' } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'regen-gitlink-'));
  const engine = join(root, 'engine');
  const game = join(root, 'game');
  mkdirSync(engine, { recursive: true });
  mkdirSync(game, { recursive: true });

  gitFile(engine, ['init', '-q', '-b', 'dev']);
  gitFile(engine, ['config', 'user.email', 'fixture@test']);
  gitFile(engine, ['config', 'user.name', 'Fixture']);
  writeFileSync(join(engine, 'version.txt'), 'v1\n');
  gitFile(engine, ['add', 'version.txt']);
  gitFile(engine, ['commit', '-q', '-m', 'engine v1']);

  gitFile(game, ['init', '-q', '-b', 'main']);
  gitFile(game, ['config', 'user.email', 'fixture@test']);
  gitFile(game, ['config', 'user.name', 'Fixture']);
  gitFile(game, ['submodule', 'add', engine, 'core']);
  gitFile(game, ['config', '-f', '.gitmodules', 'submodule.core.branch', 'dev']);

  const pkg = { name: 'fixture', version, type: 'module' };
  if (buildScript) pkg.scripts = { build: buildScript };
  writeFileSync(join(game, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');
  mkdirSync(join(game, 'src'), { recursive: true });
  writeFileSync(join(game, 'src', 'version.ts'), `export const WORKLOG_VERSION = '${derivedVersion}';\n`);

  gitFile(game, ['add', '-A']);
  gitFile(game, ['commit', '-q', '-m', 'init']);

  const gitlink = () => gitFile(game, ['ls-files', '-s', 'core']).split(/\s+/)[1] ?? '';
  const advanceEngine = () => {
    writeFileSync(join(engine, 'version.txt'), `v${Date.now()}\n`);
    gitFile(engine, ['add', 'version.txt']);
    gitFile(engine, ['commit', '-q', '-m', 'advance']);
    gitFile(join(game, 'core'), ['fetch', 'origin']);
    gitFile(join(game, 'core'), ['checkout', '-q', 'FETCH_HEAD']);
  };
  const dirtyCore = () => {
    const p = join(game, 'core', 'version.txt');
    writeFileSync(p, readFileSync(p, 'utf-8') + 'wip\n');
  };

  return { root, game, engine, gitlink, advanceEngine, dirtyCore };
}

const fixtures = [];
after(() => { for (const d of fixtures) rmSync(d, { recursive: true, force: true }); });

describe('regenerate-version (skill-local)', () => {
  test('no build script → no-op, no commit', () => {
    const dir = mkFixtureRepo({ version: '1.0.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    const result = regenerateDerivedFiles({ repoRoot: dir, version: '1.0.1', runBuild: () => {}, logger: console });
    assert.deepEqual(result, { ran: false, success: true, committed: false, reason: 'no build script' });
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before);
  });

  test('success → regenerates and commits the tracked derived file', () => {
    const dir = mkFixtureRepo({ version: '1.2.3', buildScript: 'node build.js', derivedVersion: '1.0.0' });
    fixtures.push(dir);
    const result = regenerateDerivedFiles({ repoRoot: dir, version: '1.2.3', runBuild: () => writeDerivedVersion(dir), logger: console });
    assert.equal(result.success, true);
    assert.equal(result.committed, true);
    assert.deepEqual(result.files, ['src/version.ts']);
    assert.match(git(dir, ['log', '-1', '--format=%s']), /Regenerate derived version files for v1\.2\.3/);
  });

  test('failing build → non-fatal, warning logged, no commit', () => {
    const dir = mkFixtureRepo({ version: '1.0.0', buildScript: 'node build.js', derivedVersion: '0.9.0' });
    fixtures.push(dir);
    const before = git(dir, ['rev-parse', 'HEAD']);
    const warnings = [];
    const result = regenerateDerivedFiles({
      repoRoot: dir,
      version: '1.0.0',
      runBuild: () => { throw new Error('build exploded'); },
      logger: { log() {}, info() {}, warn: (m) => warnings.push(m), error: (m) => warnings.push(m) },
    });
    assert.equal(result.success, false);
    assert.equal(result.committed, false);
    assert.equal(git(dir, ['rev-parse', 'HEAD']), before);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /Warning: derived version file regeneration failed/);
  });

  // ── Submodule pointer (gitlink) exclusion ─────────────────────────────────
  // A consumer build may advance a submodule checkout (main-street's
  // `engine:sync`), so `git add -u` would otherwise stage the gitlink. The
  // derived-files commit must never contain a submodule pointer bump.

  test('advanced submodule checkout → commits only the regenerated tracked file', () => {
    const f = mkSubmoduleFixtureRepo({ version: '5.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    f.advanceEngine();
    const result = regenerateDerivedFiles({ repoRoot: f.game, version: '5.0.0', runBuild: () => writeDerivedVersion(f.game), logger: console });
    assert.equal(result.committed, true);
    assert.deepEqual(result.files, ['src/version.ts']);
    const committed = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committed, ['src/version.ts'], 'the derived-files commit must not contain a gitlink bump');
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded submodule pointer is unchanged');
  });

  test('advanced submodule checkout with no other changes → no commit', () => {
    const f = mkSubmoduleFixtureRepo({ version: '6.0.0', derivedVersion: '6.0.0' });
    fixtures.push(f.root);
    f.advanceEngine();
    const before = git(f.game, ['rev-parse', 'HEAD']);
    const result = regenerateDerivedFiles({ repoRoot: f.game, version: '6.0.0', runBuild: () => {}, logger: console });
    assert.deepEqual(result, { ran: true, success: true, committed: false, reason: 'no changes' });
    assert.equal(git(f.game, ['rev-parse', 'HEAD']), before, 'no commit when only the gitlink moved');
  });

  test('dirty submodule working tree → still no gitlink staging', () => {
    const f = mkSubmoduleFixtureRepo({ version: '7.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    f.advanceEngine();
    f.dirtyCore();
    const result = regenerateDerivedFiles({ repoRoot: f.game, version: '7.0.0', runBuild: () => writeDerivedVersion(f.game), logger: console });
    assert.equal(result.committed, true);
    const committed = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committed, ['src/version.ts']);
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded submodule pointer is unchanged');
  });
});
