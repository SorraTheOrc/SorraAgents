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
});
