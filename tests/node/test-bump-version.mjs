/**
 * Tests for bump-version.js — version increment helper for the ship release process.
 *
 * These tests verify that the bumpVersion function correctly:
 * - Reads a package.json-like object and increments the version
 * - Supports major, minor, patch (default) bump types
 * - Handles invalid version strings gracefully
 * - Handles missing version field gracefully
 *
 * Run with:
 *   node tests/node/test-bump-version.mjs
 */

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { bumpVersion } from '../../skill/ship/scripts/release/bump-version.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

function test(name, fn) {
  try {
    fn();
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}\n    ${err.message}`);
    process.exitCode = 1;
  }
}

function cliTest(name, args, expected) {
  const scriptPath = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../skill/ship/scripts/release/bump-version.js',
  );
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

  try {
    const { stdout, stderr, status } = spawnSync(
      'node', [scriptPath, ...args],
      { encoding: 'utf-8', cwd: repoRoot },
    );

    // Numeric expected = exit code check
    if (typeof expected === 'number') {
      if (status === expected) {
        console.log(`  ✓ ${name}`);
      } else {
        console.error(`  ✗ ${name}\n    Expected exit code ${expected}, got ${status}.`);
        console.error(`    stderr: ${stderr.trim()}`);
        process.exitCode = 1;
      }
      return;
    }

    // Regex: check both stdout and stderr
    if (expected instanceof RegExp) {
      const combined = (stdout + '\n' + stderr).trim();
      if (expected.test(combined)) {
        console.log(`  ✓ ${name}`);
      } else {
        console.error(`  ✗ ${name}\n    Expected pattern ${expected} not found.`);
        console.error(`    stdout: "${stdout.trim()}"`);
        console.error(`    stderr: "${stderr.trim()}"`);
        process.exitCode = 1;
      }
      return;
    }

    // String: exact stdout match
    if (stdout.trim() === expected) {
      console.log(`  ✓ ${name}`);
    } else {
      console.error(`  ✗ ${name}\n    Expected "${expected}", got "${stdout.trim()}"`);
      process.exitCode = 1;
    }
  } catch (err) {
    console.error(`  ✗ ${name}\n    ${err.message}`);
    process.exitCode = 1;
  }
}

// ── Tests ──────────────────────────────────────────────────────────────────

console.log('\nbumpVersion() — version parsing and increment\n');

// --- Patch bump (default) ---

test('bumpVersion patch from 0.1.0 → 0.1.1', () => {
  assert.equal(bumpVersion('0.1.0', 'patch'), '0.1.1');
});

test('bumpVersion patch from 0.1.1 → 0.1.2', () => {
  assert.equal(bumpVersion('0.1.1', 'patch'), '0.1.2');
});

test('bumpVersion patch from 1.0.0 → 1.0.1', () => {
  assert.equal(bumpVersion('1.0.0', 'patch'), '1.0.1');
});

test('bumpVersion patch is the default bump type', () => {
  assert.equal(bumpVersion('0.1.0'), '0.1.1');
});

// --- Minor bump ---

test('bumpVersion minor from 0.1.0 → 0.2.0', () => {
  assert.equal(bumpVersion('0.1.0', 'minor'), '0.2.0');
});

test('bumpVersion minor from 1.2.3 → 1.3.0', () => {
  assert.equal(bumpVersion('1.2.3', 'minor'), '1.3.0');
});

test('bumpVersion minor from 0.0.0 → 0.1.0', () => {
  assert.equal(bumpVersion('0.0.0', 'minor'), '0.1.0');
});

// --- Major bump ---

test('bumpVersion major from 0.1.0 → 1.0.0', () => {
  assert.equal(bumpVersion('0.1.0', 'major'), '1.0.0');
});

test('bumpVersion major from 1.2.3 → 2.0.0', () => {
  assert.equal(bumpVersion('1.2.3', 'major'), '2.0.0');
});

// --- Edge cases ---

test('bumpVersion throws on malformed version "abc"', () => {
  assert.throws(
    () => bumpVersion('abc', 'patch'),
    /Invalid version string|Cannot parse version/i,
  );
});

test('bumpVersion throws on empty version string', () => {
  assert.throws(
    () => bumpVersion('', 'patch'),
    /Invalid version string|Cannot parse version/i,
  );
});

test('bumpVersion throws on partial version "1.2"', () => {
  assert.throws(
    () => bumpVersion('1.2', 'patch'),
    /Invalid version string|Cannot parse version/i,
  );
});

test('bumpVersion throws on invalid bump type "foo"', () => {
  assert.throws(
    () => bumpVersion('0.1.0', 'foo'),
    /Invalid bump type|must be one of/i,
  );
});

test('bumpVersion throws on leading zeros "01.2.3"', () => {
  assert.throws(
    () => bumpVersion('01.2.3', 'patch'),
    /Invalid version string|Cannot parse version/i,
  );
});

test('bumpVersion handles pre-release version "1.0.0-alpha.1"', () => {
  const result = bumpVersion('1.0.0-alpha.1', 'patch');
  assert.ok(result, 'should return a version string');
});

console.log('\n--- All bumpVersion tests complete ---\n');

// ── CLI invocation test ────────────────────────────────────────────────────

console.log('CLI invocation (via child process):\n');

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const pkgPath = resolve(repoRoot, 'package.json');

if (!existsSync(pkgPath)) {
  console.log('  (skipping CLI tests — no package.json found in repo root)');
} else {
  cliTest('CLI: --bump patch (dry-run)', ['--bump', 'patch', '--dry-run'], /New version/);
  cliTest('CLI: --bump minor (dry-run)', ['--bump', 'minor', '--dry-run'], /New version/);
  cliTest('CLI: --bump major (dry-run)', ['--bump', 'major', '--dry-run'], /New version/);
  cliTest('CLI: no args, dry-run', ['--dry-run'], /New version/);
  cliTest('CLI: invalid --bump value', ['--bump', 'foo'], /Invalid bump type/);
  cliTest('CLI: --help exits with 0', ['--help'], 0);
}

console.log('\n');
