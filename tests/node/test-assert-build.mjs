/**
 * Tests for assert-build.mjs — the fatal build/type-check release gate
 * (SA-0MWKQ5TXL005CLX5).
 *
 * Covers the pure decision boundary: build-script discovery and the
 * pass/fail/no-op outcome when the build runner is injected. The end-to-end
 * CLI behaviour against a real merged tree (npm run build red/green, bypass,
 * dry-run, tagging) is covered by tests/test_release_build_gate.py.
 *
 * Run with:
 *   node tests/node/test-assert-build.mjs
 */

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  assertBuild,
  readBuildScript,
} from '../../skill/ship/scripts/release/assert-build.mjs';
import { hermeticGitEnv } from '../helpers/hermeticGitEnv.mjs';

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

function withFixture(files, fn) {
  const dir = mkdtempSync(join(tmpdir(), 'assert-build-'));
  try {
    for (const [name, content] of Object.entries(files)) {
      const path = join(dir, name);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, content);
    }
    fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function packageJson(overrides = {}) {
  return JSON.stringify({ name: 'fixture', version: '1.0.0', ...overrides }, null, 2) + '\n';
}

/** A logger that records errors instead of printing during tests. */
function recordingLogger() {
  const errors = [];
  return {
    errors,
    error: (message) => errors.push(String(message)),
  };
}

// ── readBuildScript ─────────────────────────────────────────────────────────


test('readBuildScript returns the declared build command', () => {
  withFixture(
    { 'package.json': packageJson({ scripts: { build: 'tsc -b' } }) },
    (dir) => {
      assert.equal(readBuildScript(dir), 'tsc -b');
    },
  );
});

test('readBuildScript returns null without a package.json', () => {
  withFixture({}, (dir) => {
    assert.equal(readBuildScript(dir), null);
  });
});

test('readBuildScript returns null when scripts.build is absent', () => {
  withFixture(
    { 'package.json': packageJson({ scripts: { test: 'node --test' } }) },
    (dir) => {
      assert.equal(readBuildScript(dir), null);
    },
  );
});

test('readBuildScript returns null when scripts.build is blank', () => {
  withFixture(
    { 'package.json': packageJson({ scripts: { build: '   ' } }) },
    (dir) => {
      assert.equal(readBuildScript(dir), null);
    },
  );
});

test('readBuildScript returns null for invalid package.json', () => {
  withFixture({ 'package.json': '{ not json' }, (dir) => {
    assert.equal(readBuildScript(dir), null);
  });
});

// ── assertBuild: decision boundary ──────────────────────────────────────────


test('assertBuild is a no-op (and never runs the build) without a build script', () => {
  withFixture({ 'package.json': packageJson() }, (dir) => {
    let ran = false;
    const logger = recordingLogger();
    const result = assertBuild({ repoRoot: dir, runBuild: () => { ran = true; }, logger });

    assert.equal(ran, false, 'build must never run when there is no build script');
    assert.deepEqual(result, { ran: false, success: true, reason: 'no build script' });
    assert.ok(
      logger.errors.some((line) => /no build script/i.test(line)),
      `expected a no-op log, got ${JSON.stringify(logger.errors)}`,
    );
  });
});

test('assertBuild succeeds when the injected build resolves', () => {
  withFixture(
    { 'package.json': packageJson({ scripts: { build: 'true' } }) },
    (dir) => {
      let calls = 0;
      const logger = recordingLogger();
      const result = assertBuild({
        repoRoot: dir,
        runBuild: () => { calls += 1; },
        logger,
      });

      assert.equal(calls, 1, 'the build must run exactly once');
      assert.deepEqual(result, { ran: true, success: true, reason: 'build passed' });
      assert.equal(logger.errors.length, 0);
    },
  );
});

test('assertBuild reports failure (never throws) when the injected build throws', () => {
  withFixture(
    { 'package.json': packageJson({ scripts: { build: 'false' } }) },
    (dir) => {
      const result = assertBuild({
        repoRoot: dir,
        runBuild: () => {
          throw new Error('TS2305: Module has no exported member');
        },
        logger: recordingLogger(),
      });

      assert.equal(result.ran, true);
      assert.equal(result.success, false);
      assert.match(result.reason, /TS2305/);
    },
  );
});

// ── CLI: exit codes and messages (no external build dependency) ─────────────

const CLI_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../skill/ship/scripts/release/assert-build.mjs',
);

function runCli(cwd) {
  // Hermetic: a fixture must never inherit the finish gate's GIT_INDEX_FILE
  // (SA-0MVYS34QF000AWQX) or another repository override.
  return spawnSync('node', [CLI_PATH], { encoding: 'utf-8', cwd, env: hermeticGitEnv() });
}

function gitInit(dir) {
  const run = (args) =>
    spawnSync('git', ['-c', 'user.name=t', '-c', 'user.email=t@t', ...args], {
      cwd: dir,
      encoding: 'utf-8',
      env: hermeticGitEnv(),
    });
  run(['init', '-q']);
}

test('CLI exits 0 as a no-op when package.json declares no build', () => {
  withFixture({ 'package.json': packageJson() }, (dir) => {
    gitInit(dir);
    const result = runCli(dir);
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stderr, /scripts\.build.*no-op/i);
  });
});

test('CLI exits 1 when the build fails and names the failure', () => {
  withFixture(
    {
      'package.json': packageJson({ scripts: { build: 'node build.js' } }),
      'build.js': 'process.exit(1);\n',
    },
    (dir) => {
      gitInit(dir);
      const result = runCli(dir);
      assert.equal(result.status, 1, result.stderr);
      assert.match(result.stderr, /Build failed/i);
    },
  );
});

test('CLI exits 0 when the build succeeds', () => {
  withFixture(
    {
      'package.json': packageJson({ scripts: { build: 'node build.js' } }),
      'build.js': 'process.exit(0);\n',
    },
    (dir) => {
      gitInit(dir);
      const result = runCli(dir);
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stderr, /succeeded/i);
    },
  );
});

console.log('\n');
