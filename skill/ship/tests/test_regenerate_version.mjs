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
import { hermeticGitEnv } from '../../shared/git_env.mjs';

const __filename = fileURLToPath(import.meta.url);
const MODULE_PATH = join(dirname(__filename), '..', 'scripts', 'release', 'regenerate-version.js');

const { regenerateDerivedFiles } = await import(MODULE_PATH);

function git(dir, args) {
  // Hermetic: a fixture must never inherit the finish gate's GIT_INDEX_FILE
  // (SA-0MVYS34QF000AWQX) — it would write into the gate's index.
  const res = spawnSync('git', args, { cwd: dir, encoding: 'utf-8', env: hermeticGitEnv() });
  if (res.status !== 0) throw new Error(`git ${args.join(' ')} failed: ${res.stderr}`);
  return res.stdout.trim();
}

/** Run a git command with the file transport protocol allow-listed. */
function gitFile(dir, args) {
  const res = spawnSync('git', ['-c', 'protocol.file.allow=always', ...args], {
    cwd: dir,
    encoding: 'utf-8',
    env: { ...hermeticGitEnv(), GIT_ALLOW_PROTOCOL: 'file', GIT_TERMINAL_PROMPT: '0' },
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

  const gitlink = () => gitFile(game, ['ls-files', '-s', 'core']).split(/\s+/)[1] ?? '';

  // A tracked pin file recording the submodule commit (main-street's
  // `configs/engine-pin.json`). The build rewrites it as it advances the
  // checkout — the coupling this fixture exercises (SA-0MW6LOXBO0008706).
  const pinPath = join(game, 'configs', 'engine-pin.json');
  mkdirSync(join(game, 'configs'), { recursive: true });
  writeFileSync(pinPath, JSON.stringify({ core: gitlink() }, null, 2) + '\n');

  gitFile(game, ['add', '-A']);
  gitFile(game, ['commit', '-q', '-m', 'init']);

  const readPin = () => readFileSync(pinPath, 'utf-8');
  const writePin = (sha) => writeFileSync(pinPath, JSON.stringify({ core: sha }, null, 2) + '\n');
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

  return { root, game, engine, gitlink, advanceEngine, dirtyCore, pinPath, readPin, writePin };
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

  // ── Submodule pin restoration (SA-0MW6LOXBO0008706) ───────────────────────
  // Excluding the pointer is not enough when the build also rewrites a tracked
  // pin recording the submodule SHA: the commit must carry neither the gitlink
  // nor a pin that disagrees with it.

  test('build-rewritten pin is restored alongside the restored gitlink', () => {
    const f = mkSubmoduleFixtureRepo({ version: '5.1.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    const pinBefore = f.readPin();
    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '5.1.0',
      runBuild: () => {
        f.advanceEngine();
        f.writePin(gitFile(join(f.game, 'core'), ['rev-parse', 'HEAD']));
        writeDerivedVersion(f.game);
      },
      logger: console,
    });
    assert.equal(result.committed, true);
    assert.deepEqual(result.files, ['src/version.ts']);
    const committed = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committed, ['src/version.ts'], 'the commit must contain only the genuinely derived file');
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded gitlink is unchanged');
    assert.equal(f.readPin(), pinBefore, 'the pin file is restored to the recorded SHA');
    assert.deepEqual(result.restoredGitlinks, ['core']);
    assert.deepEqual(result.restoredPins, ['configs/engine-pin.json']);
  });

  test('a pin the release bumped itself survives regeneration', () => {
    const f = mkSubmoduleFixtureRepo({ version: '8.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    f.advanceEngine();
    const releasedSha = gitFile(join(f.game, 'core'), ['rev-parse', 'HEAD']);
    f.writePin(releasedSha);
    git(f.game, ['add', '-A']);
    git(f.game, ['commit', '-q', '-m', 'release: deliberate engine advance']);
    assert.equal(f.gitlink(), releasedSha);

    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '8.0.0',
      runBuild: () => {
        f.advanceEngine();
        f.writePin(gitFile(join(f.game, 'core'), ['rev-parse', 'HEAD']));
        writeDerivedVersion(f.game);
      },
      logger: console,
    });
    assert.equal(result.committed, true);
    const committed = git(f.game, ['show', '--name-only', '--format=', 'HEAD']).split('\n').filter(Boolean);
    assert.deepEqual(committed, ['src/version.ts']);
    assert.equal(f.gitlink(), releasedSha, "the release's deliberate gitlink survives");
    assert.ok(f.readPin().includes(releasedSha), "the release's deliberate pin survives");
    assert.deepEqual(result.restoredPins, ['configs/engine-pin.json']);
  });

  test('matches abbreviated SHAs in the pin (minimum prefix)', () => {
    const f = mkSubmoduleFixtureRepo({ version: '9.0.0', derivedVersion: '1.0.0' });
    fixtures.push(f.root);
    const gitlinkBefore = f.gitlink();
    f.writePin(gitlinkBefore.slice(0, 7));
    git(f.game, ['add', '-A']);
    git(f.game, ['commit', '-q', '-m', 'abbreviate pin']);
    const pinBefore = f.readPin();

    const result = regenerateDerivedFiles({
      repoRoot: f.game,
      version: '9.0.0',
      runBuild: () => {
        f.advanceEngine();
        f.writePin(gitFile(join(f.game, 'core'), ['rev-parse', 'HEAD']).slice(0, 7));
        writeDerivedVersion(f.game);
      },
      logger: console,
    });
    assert.equal(result.committed, true);
    assert.equal(f.gitlink(), gitlinkBefore, 'the recorded gitlink is unchanged');
    assert.equal(f.readPin(), pinBefore, 'an abbreviated pin is restored too');
    assert.deepEqual(result.restoredPins, ['configs/engine-pin.json']);
  });
});
