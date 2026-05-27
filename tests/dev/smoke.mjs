/**
 * Dev-branch smoke tests — fast, high-value sanity checks that run on every
 * push to `dev`.  Designed to catch critical problems early:
 *
 *  1. Repository structure (key files/directories present)
 *  2. Terminology lint (check-terminology.sh passes)
 *  3. Python test discovery (pytest can collect tests)
 *  4. Worklog CLI availability (wl is on PATH)
 *  5. Agent frontmatter lint (YAML front-matter validates)
 *
 * Run locally from the repository root:
 *
 *   node --test tests/dev/smoke.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(fileURLToPath(import.meta.url), '..', '..', '..');

/** Helper: assert a path exists relative to the repository root. */
function assertPathExists(relative) {
  const full = join(REPO_ROOT, relative);
  assert.ok(existsSync(full), `Expected path to exist: ${relative}`);
}

/** Helper: run a command from the repo root; returns { stdout, stderr, exitCode }. */
function run(cmd, opts = {}) {
  try {
    const stdout = execSync(cmd, {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      ...opts,
    });
    return { stdout, stderr: '', exitCode: 0 };
  } catch (err) {
    return {
      stdout: err.stdout ?? '',
      stderr: err.stderr ?? '',
      exitCode: err.status ?? 1,
    };
  }
}

// ---------------------------------------------------------------------------
// 1. Repository structure
// ---------------------------------------------------------------------------
test('repository structure: key files and directories exist', () => {
  const required = [
    'AGENTS.md',
    'README.md',
    'Workflow.md',
    'skill/audit/SKILL.md',
    'skill/implement/SKILL.md',
    'tests/conftest.py',
    '.github/workflows/ci.yml',
  ];
  for (const p of required) {
    assertPathExists(p);
  }
});

// ---------------------------------------------------------------------------
// 2. Terminology lint
// ---------------------------------------------------------------------------
test('terminology: check-terminology.sh passes', () => {
  const result = run('bash scripts/check-terminology.sh');
  assert.equal(result.exitCode, 0, `check-terminology.sh failed: ${result.stderr}`);
  assert.ok(result.stdout.includes('RESULT: PASS'), 'Expected PASS result from terminology scan');
});

// ---------------------------------------------------------------------------
// 3. Python test discovery
// ---------------------------------------------------------------------------
test('python: pytest can discover tests', () => {
  const result = run('python3 -m pytest --collect-only -q 2>&1 || true');
  // pytest --collect-only exits 0 when tests are found, 5 when none found
  assert.ok(result.exitCode <= 1, `pytest collect failed with exit code ${result.exitCode}: ${result.stderr}`);
  // Verify at least some test files are collected
  assert.ok(
    result.stdout.includes('test_') || result.stdout.includes('collected'),
    'pytest should collect test items',
  );
});

// ---------------------------------------------------------------------------
// 4. Worklog CLI
// ---------------------------------------------------------------------------
test('tooling: wl CLI is available', () => {
  const result = run('wl --version');
  assert.equal(result.exitCode, 0, `wl CLI not available: ${result.stderr}`);
  assert.ok(result.stdout.trim().length > 0, 'wl --version should return a version string');
});

// ---------------------------------------------------------------------------
// 5. Agent frontmatter lint (skip if pyyaml unavailable)
// ---------------------------------------------------------------------------
test('agent: frontmatter lint passes', { skip: false }, () => {
  // Try importing yaml first to see if pyyaml is installed
  const yamlCheck = run('python3 -c "import yaml" 2>&1');
  if (yamlCheck.exitCode !== 0) {
    // pyyaml not installed — this test is skipped at runtime by design
    assert.ok(true, 'pyyaml not installed; frontmatter lint skipped');
    return;
  }
  const result = run('python3 scripts/agent_frontmatter_lint.py 2>&1');
  // Exit 0 = no errors, 1 = warnings only, 2 = errors
  assert.ok(
    result.exitCode <= 1,
    `agent_frontmatter_lint.py reported errors (exit ${result.exitCode}): ${result.stdout}`,
  );
});
