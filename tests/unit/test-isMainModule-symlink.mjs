/**
 * Tests for isMainModule symlink-aware guard in ship skill scripts.
 *
 * These tests verify that bump-version.js (and by extension run-release.js)
 * correctly detect when they are executed as the main module through a
 * symlink. The isMainModule guard uses fs.realpathSync() on both sides
 * of the comparison so that symlinked paths resolve to the same real path.
 *
 * Run with:
 *   node --test tests/unit/test-isMainModule-symlink.mjs
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  symlinkSync,
  realpathSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = resolve(dirname(__filename), '..', '..');
const BUMP_VERSION_SRC = join(
  REPO_ROOT,
  'skill',
  'ship',
  'scripts',
  'release',
  'bump-version.js',
);

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Create a temporary directory with a minimal package.json and a symlink to
 * bump-version.js. Then execute bump-version.js via the symlink with --dry-run.
 *
 * @param {object} [opts]
 * @param {string} [opts.version]  - version to write in package.json (default "0.1.0")
 * @param {string[]} [opts.args]   - extra CLI args (default ["--dry-run"])
 * @returns {object} spawnSync result
 */
function runBumpVersionViaSymlink(opts = {}) {
  const { version = '0.1.0', args = ['--dry-run'] } = opts;

  // Create a temp directory with a minimal package.json
  const tmpDir = mkdtempSync(join(tmpdir(), 'bump-version-symlink-test-'));
  writeFileSync(
    join(tmpDir, 'package.json'),
    JSON.stringify({ name: 'test-pkg', version }) + '\n',
  );

  // Create the symlink to bump-version.js
  const symlinkPath = join(tmpDir, 'bump-version.js');
  symlinkSync(BUMP_VERSION_SRC, symlinkPath);

  return spawnSync(process.execPath, [symlinkPath, ...args], {
    cwd: tmpDir,
    encoding: 'utf-8',
    timeout: 10_000,
  });
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('isMainModule via symlink (bump-version.js)', () => {
  test('exits with code 0 when executed through a symlink', () => {
    const res = runBumpVersionViaSymlink();
    assert.equal(
      res.status,
      0,
      `Expected exit code 0 when running via symlink, got ${res.status}. stderr: ${res.stderr}`,
    );
  });

  test('prints version output when executed through a symlink (--dry-run)', () => {
    const res = runBumpVersionViaSymlink({ args: ['--dry-run'] });
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('Current version:'),
      `Expected version output via symlink, got:\n${out}`,
    );
    assert.ok(
      out.includes('New version'),
      `Expected "New version" in output via symlink, got:\n${out}`,
    );
  });

  test('supports --bump patch via symlink', () => {
    const res = runBumpVersionViaSymlink({
      args: ['--bump', 'patch', '--dry-run'],
    });
    assert.equal(
      res.status,
      0,
      `Expected exit code 0 for --bump patch via symlink, got ${res.status}`,
    );
  });

  test('supports --bump minor via symlink', () => {
    const res = runBumpVersionViaSymlink({
      args: ['--bump', 'minor', '--dry-run'],
    });
    assert.equal(
      res.status,
      0,
      `Expected exit code 0 for --bump minor via symlink, got ${res.status}`,
    );
  });

  test('supports --bump major via symlink', () => {
    const res = runBumpVersionViaSymlink({
      args: ['--bump', 'major', '--dry-run'],
    });
    assert.equal(
      res.status,
      0,
      `Expected exit code 0 for --bump major via symlink, got ${res.status}`,
    );
  });

  test('--help works via symlink', () => {
    const res = runBumpVersionViaSymlink({ args: ['--help'] });
    assert.equal(
      res.status,
      0,
      `Expected exit code 0 for --help via symlink, got ${res.status}`,
    );
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('Usage:'),
      `Expected usage output via symlink, got:\n${out}`,
    );
  });

  test('reports error for invalid --bump type via symlink', () => {
    const res = runBumpVersionViaSymlink({
      args: ['--bump', 'invalid', '--dry-run'],
    });
    assert.equal(
      res.status,
      1,
      `Expected exit code 1 for invalid --bump via symlink, got ${res.status}`,
    );
  });

  test('script and symlink resolve to the same real path', () => {
    // Verify the real paths match — this confirms the symlink target exists
    const realScriptPath = realpathSync(BUMP_VERSION_SRC);

    const tmpDir = mkdtempSync(join(tmpdir(), 'bump-version-realpath-test-'));
    const symlinkPath = join(tmpDir, 'bump-version.js');
    symlinkSync(BUMP_VERSION_SRC, symlinkPath);
    const realSymlinkPath = realpathSync(symlinkPath);

    assert.equal(
      realScriptPath,
      realSymlinkPath,
      `Symlink realpath should match script realpath.\n  Script: ${realScriptPath}\n  Symlink: ${realSymlinkPath}`,
    );
  });
});

describe('isMainModule guard behaviour', () => {
  test('script runs correctly when imported as module (no CLI execution)', async () => {
    // Verify that importing bump-version.js does NOT trigger the CLI main()
    // This confirms the isMainModule guard still prevents auto-execution on import
    const mod = await import(BUMP_VERSION_SRC);
    assert.equal(typeof mod.bumpVersion, 'function', 'bumpVersion should be exported');
    assert.equal(typeof mod.readVersion, 'function', 'readVersion should be exported');
    assert.equal(typeof mod.writeVersion, 'function', 'writeVersion should be exported');
    // No side-effects should have occurred from the import
  });
});
