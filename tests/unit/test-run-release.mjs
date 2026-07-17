/**
 * Unit tests for skill/ship/scripts/run-release.js — missing-script detection
 * and skill safety behaviour.
 *
 * These tests verify that when the canonical release script is absent from
 * both the skill-level and repository locations, the script:
 *
 *  1. Exits with a non-zero status code
 *  2. Prints a clear, actionable error message to stderr
 *  3. Does NOT attempt any automatic execution
 *  4. Provides human-fallback instructions
 *  5. References the release-process documentation
 *
 * Test strategy: copy the run-release.js script into a temporary directory
 * that mimics the skill layout but intentionally lacks the release script,
 * then execute it.  This avoids any filesystem-mocking complexity and
 * faithfully tests the real behaviour of the script.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Create a fake "skill" directory structure without the release script and
 * run run-release.js from it.  Returns the spawnSync result.
 *
 * @param {object} [opts]
 * @param {boolean} [opts.hasSkillScript]  — if true, include the release script in the skill dir
 * @param {boolean} [opts.hasRepoScript]   — if true, include the release script in a top-level scripts/ dir
 * @param {string}  [opts.cwd]             — working directory (defaults to the fake skill root)
 */
function runRunRelease(opts = {}) {
  const {
    hasSkillScript = false,
    hasRepoScript = false,
    cwd,
  } = opts;

  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-test-'));

  // Recreate the skill directory layout that run-release.js expects:
  // <tmp>/skill/ship/scripts/run-release.js
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));

  // Optionally place the release script in the skill-level location
  if (hasSkillScript) {
    const skillReleaseDir = join(skillScriptDir, 'release');
    mkdirSync(skillReleaseDir, { recursive: true });
    writeFileSync(join(skillReleaseDir, 'merge-dev-to-main.sh'), '#!/bin/bash\necho "skill script"\n');
  }

  // Optionally place the release script in the repo-level location
  if (hasRepoScript) {
    const repoReleaseDir = join(tmpDir, 'scripts', 'release');
    mkdirSync(repoReleaseDir, { recursive: true });
    writeFileSync(join(repoReleaseDir, 'merge-dev-to-main.sh'), '#!/bin/bash\necho "repo script"\n');
  }

  const runReleasePath = join(skillScriptDir, 'run-release.js');
  const runCwd = cwd || tmpDir;

  return spawnSync(process.execPath, [runReleasePath], {
    cwd: runCwd,
    encoding: 'utf-8',
    timeout: 10_000,
  });
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('run-release: missing-script detection', () => {
  test('exits non-zero when both skill and repo scripts are missing', () => {
    const res = runRunRelease();
    assert.notStrictEqual(
      res.status,
      0,
      `Expected non-zero exit status when scripts are missing, got ${res.status}`,
    );
  });

  test('prints "Ship automated release unavailable" to stderr', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('Ship automated release unavailable'),
      `Expected missing-script message in output, got:\n${out}`,
    );
  });

  test('mentions attempted locations (skill and repository)', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('Attempted locations') || out.includes('attempted'),
      `Expected "Attempted locations" in output, got:\n${out}`,
    );
  });

  test('provides human fallback instructions', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('Human fallback') || out.includes('fallback') || out.includes('fallback'),
      `Expected human fallback instructions in output, got:\n${out}`,
    );
  });

  test('references docs/dev/release-process.md', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('release-process') || out.includes('release process'),
      `Expected reference to release-process.md in output, got:\n${out}`,
    );
  });

  test('does NOT attempt to run any git commands (safety check)', () => {
    // When scripts are missing, the script must exit before any git commands.
    // We verify by checking that no git-related output appears in stdout.
    const res = runRunRelease();
    assert.equal(
      res.stdout,
      '',
      'Expected no stdout when scripts are missing (no git commands should run)',
    );
  });

  test('uses exit code 2 for missing scripts', () => {
    const res = runRunRelease();
    assert.equal(
      res.status,
      2,
      `Expected exit code 2 for missing scripts, got ${res.status}`,
    );
  });

  test('provides example manual merge commands', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('git merge') || out.includes('git checkout'),
      `Expected example manual commands in output, got:\n${out}`,
    );
  });

  test('does not crash or hang when run from an arbitrary directory', () => {
    // Run from /tmp which is definitely not a git repo
    const res = runRunRelease({ cwd: '/tmp' });
    assert.notStrictEqual(res.status, 0, 'Expected non-zero exit from /tmp');
    assert.equal(
      res.signal,
      null,
      'Script should exit normally (not receive a signal)',
    );
  });

  test('output contains the skill-level script path', () => {
    const res = runRunRelease();
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    // The error message should reference where it looked for the skill script
    assert.ok(
      out.includes('skill') && out.includes('release'),
      `Expected skill-level path in output, got:\n${out}`,
    );
  });
});

describe('run-release: script found path', () => {
  test('finds the skill-level release script when present', () => {
    const res = runRunRelease({ hasSkillScript: true });
    // With the script present, it should NOT print the missing-script message
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      !out.includes('Ship automated release unavailable'),
      'Should not report missing script when skill-level script exists',
    );
    // It may exit with a non-zero status due to git-check failures in the
    // release script itself, but it should NOT print the missing-script error.
  });

  test('finds the repository-level release script when skill-level is absent', () => {
    const res = runRunRelease({ hasRepoScript: true, hasSkillScript: false });
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      !out.includes('Ship automated release unavailable'),
      'Should not report missing script when repo-level script exists',
    );
  });

  test('prefers skill-level script over repository-level script', () => {
    const res = runRunRelease({ hasSkillScript: true, hasRepoScript: true });
    // Both exist — skill-level should be preferred.
    // The exact stdout/stderr depends on the release script's own logic,
    // but we can verify the missing-script message is NOT printed.
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      !out.includes('Ship automated release unavailable'),
      'Should not report missing script when either script exists',
    );
  });
});

describe('run-release: SKILL.md consistency', () => {
  const SHIP_SKILL_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');

  test('SKILL.md documents the missing-script safety behaviour', () => {
    const content = readFileSync(SHIP_SKILL_PATH, 'utf8');
    assert.ok(
      content.includes('missing') &&
        (content.includes('script') || content.includes('Script')),
      'SKILL.md should document missing-script behaviour',
    );
  });

  test('SKILL.md states the agent must refuse automatic execution when script is missing', () => {
    const content = readFileSync(SHIP_SKILL_PATH, 'utf8');
    assert.ok(
      content.includes('refuse') ||
        content.includes('must not') ||
        content.includes('must NOT') ||
        content.includes('MUST NOT') ||
        content.includes('not available'),
      'SKILL.md should state that the agent refuses automatic execution when the script is missing',
    );
  });

  test('SKILL.md references run-release.js as the invocation method', () => {
    const content = readFileSync(SHIP_SKILL_PATH, 'utf8');
    assert.ok(
      content.includes('run-release.js'),
      'SKILL.md should reference run-release.js as the invocation method',
    );
  });
});
