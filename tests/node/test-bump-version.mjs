/**
 * Tests for bump-version.js — version increment helper for the ship release process.
 *
 * These tests verify that the bumpVersion function correctly:
 * - Reads a package.json-like object and increments the version
 * - Supports major, minor, patch (default) bump types
 * - Handles invalid version strings gracefully
 * - Handles missing version field gracefully
 * - Resolves the version source by convention: `package.json` when present,
 *   else `pyproject.toml` `[project].version` (SA-0MVN2B0DJ00135W0)
 *
 * Run with:
 *   node tests/node/test-bump-version.mjs
 */

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import {
  bumpVersion,
  readPyprojectVersion,
  replaceProjectVersion,
  resolveVersionSource,
} from '../../skill/ship/scripts/release/bump-version.js';

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

// ── pyproject.toml version source (SA-0MVN2B0DJ00135W0) ────────────────────

/**
 * A realistic pyproject.toml. It deliberately contains:
 * - comments that must survive the rewrite,
 * - a decoy `version` key in another table ([tool.pytest.ini_options]), and
 * - a version assignment mid-table (not the first key).
 */
const PYPROJECT_FIXTURE = `[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "tts-server"
# The released version — bumped by the ship skill's release pipeline.
version = "0.1.0"   # keep in sync with the git tag
description = "A text-to-speech server"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.100",  # pinned for the v0.1 cycle
]

[project.optional-dependencies]
dev = ["pytest"]

[tool.pytest.ini_options]
version = "9.9.9"   # NOT the project version — must never be touched
`;

console.log('pyproject.toml version resolution (pure helpers):\n');

test('readPyprojectVersion reads [project].version, ignoring other tables', () => {
  assert.equal(readPyprojectVersion(PYPROJECT_FIXTURE), '0.1.0');
});

test('replaceProjectVersion rewrites only the [project].version value', () => {
  const updated = replaceProjectVersion(PYPROJECT_FIXTURE, '0.1.1');
  assert.equal(
    updated,
    PYPROJECT_FIXTURE.replace('version = "0.1.0"', 'version = "0.1.1"'),
    'every byte outside the [project].version value must be preserved',
  );
  assert.ok(updated.includes('version = "9.9.9"'), 'decoy version key must be untouched');
  assert.ok(updated.includes('# keep in sync with the git tag'), 'trailing comments must survive');
});

test('replaceProjectVersion preserves single quotes', () => {
  const src = '[project]\nname = "x"\nversion = \'2.3.4\'  # note\n';
  assert.equal(replaceProjectVersion(src, '2.4.0'), '[project]\nname = "x"\nversion = \'2.4.0\'  # note\n');
});

test('replaceProjectVersion preserves CRLF line endings', () => {
  const src = '[project]\r\nname = "x"\r\nversion = "1.2.3"\r\n';
  assert.equal(replaceProjectVersion(src, '1.2.4'), '[project]\r\nname = "x"\r\nversion = "1.2.4"\r\n');
  assert.equal(readPyprojectVersion(src), '1.2.3');
});

test('readPyprojectVersion rejects a dynamic version with an actionable message', () => {
  assert.throws(
    () => readPyprojectVersion('[project]\nname = "x"\nversion = "dynamic"\n'),
    /dynamic/i,
  );
});

test('readPyprojectVersion rejects a missing [project].version with candidate sections named', () => {
  assert.throws(
    () => readPyprojectVersion('[project]\nname = "x"\ndynamic = ["version"]\n'),
    (err) => /\[project\]\.version/.test(err.message)
      && /\[tool\.poetry\]/.test(err.message)
      && /dynamic/.test(err.message),
  );
});

console.log('\n--- All pyproject.toml helper tests complete ---\n');

// ── CLI tests against fixture repositories ─────────────────────────────────

/** Create a throwaway git repo root holding *files*. */
function makeFixtureRepo(files = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'bump-version-fixture-'));
  spawnSync('git', ['init', '-b', 'dev'], { cwd: dir, encoding: 'utf-8' });
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(join(dir, name), content, 'utf-8');
  }
  return dir;
}

/** Run the bump-version CLI with *cwd* as the repository root. */
function runCli(cwd, args) {
  const scriptPath = resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../skill/ship/scripts/release/bump-version.js',
  );
  return spawnSync('node', [scriptPath, ...args], { encoding: 'utf-8', cwd });
}

function withFixture(files, fn) {
  const dir = makeFixtureRepo(files);
  try {
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const SIMPLE_PYPROJECT = '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n';

console.log('pyproject.toml version source (CLI):\n');

for (const [bump, expected] of [['patch', '0.1.1'], ['minor', '0.2.0'], ['major', '1.0.0']]) {
  test(`CLI: pyproject.toml ${bump} bump writes the new version`, () => {
    withFixture({ 'pyproject.toml': SIMPLE_PYPROJECT }, (dir) => {
      const res = runCli(dir, ['--bump', bump]);
      assert.equal(res.status, 0, `expected success, stderr: ${res.stderr}`);
      assert.equal(res.stdout.trim(), expected);
      assert.equal(
        readFileSync(join(dir, 'pyproject.toml'), 'utf-8'),
        SIMPLE_PYPROJECT.replace('"0.1.0"', `"${expected}"`),
      );
    });
  });
}

test('CLI: pyproject.toml --dry-run does not write', () => {
  withFixture({ 'pyproject.toml': SIMPLE_PYPROJECT }, (dir) => {
    const res = runCli(dir, ['--bump', 'minor', '--dry-run']);
    assert.equal(res.status, 0, `expected success, stderr: ${res.stderr}`);
    assert.match(res.stdout, /New version/);
    assert.equal(readFileSync(join(dir, 'pyproject.toml'), 'utf-8'), SIMPLE_PYPROJECT);
  });
});

test('CLI: package.json wins over pyproject.toml when both exist', () => {
  const pkg = '{\n  "name": "demo",\n  "version": "2.0.0"\n}\n';
  withFixture({ 'package.json': pkg, 'pyproject.toml': SIMPLE_PYPROJECT }, (dir) => {
    const res = runCli(dir, ['--bump', 'patch']);
    assert.equal(res.status, 0, `expected success, stderr: ${res.stderr}`);
    assert.equal(JSON.parse(readFileSync(join(dir, 'package.json'), 'utf-8')).version, '2.0.1');
    assert.equal(readFileSync(join(dir, 'pyproject.toml'), 'utf-8'), SIMPLE_PYPROJECT);
  });
});

test('CLI: neither package.json nor pyproject.toml → combined actionable error', () => {
  withFixture({ 'README.md': '# demo\n' }, (dir) => {
    const res = runCli(dir, ['--bump', 'patch']);
    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /package\.json/);
    assert.match(res.stderr, /pyproject\.toml/);
  });
});

test('CLI: dynamic version → non-zero exit, file unmodified', () => {
  const src = '[project]\nname = "demo"\ndynamic = ["version"]\n';
  withFixture({ 'pyproject.toml': src }, (dir) => {
    const res = runCli(dir, ['--bump', 'patch']);
    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /dynamic/i);
    assert.equal(readFileSync(join(dir, 'pyproject.toml'), 'utf-8'), src);
  });
});

test('CLI: invalid semver in pyproject.toml → non-zero exit, file unmodified', () => {
  const src = '[project]\nname = "demo"\nversion = "not-a-version"\n';
  withFixture({ 'pyproject.toml': src }, (dir) => {
    const res = runCli(dir, ['--bump', 'patch']);
    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /Cannot parse version/i);
    assert.equal(readFileSync(join(dir, 'pyproject.toml'), 'utf-8'), src);
  });
});

console.log('\nresolveVersionSource():\n');

test('resolveVersionSource prefers package.json and reports its version', () => {
  withFixture(
    { 'package.json': '{\n  "version": "1.0.0"\n}\n', 'pyproject.toml': SIMPLE_PYPROJECT },
    (dir) => {
      const source = resolveVersionSource(dir);
      assert.equal(source.kind, 'package.json');
      assert.equal(source.version, '1.0.0');
    },
  );
});

test('resolveVersionSource falls back to pyproject.toml for Python-only repos', () => {
  withFixture({ 'pyproject.toml': SIMPLE_PYPROJECT }, (dir) => {
    const source = resolveVersionSource(dir);
    assert.equal(source.kind, 'pyproject.toml');
    assert.equal(source.version, '0.1.0');
    assert.ok(source.path.endsWith('pyproject.toml'));
  });
});

test('resolveVersionSource throws when no candidate file exists', () => {
  withFixture({}, (dir) => {
    assert.throws(() => resolveVersionSource(dir), /package\.json[\s\S]*pyproject\.toml/);
  });
});

console.log('\n');
