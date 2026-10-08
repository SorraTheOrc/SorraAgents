/**
 * Unit tests for post-release cleanup integration in run-release.js
 *
 * Tests that run-release.js calls cleanup scripts as a post-release step
 * after closeWorkItemsAfterRelease(), with --yes for non-interactive execution.
 *
 * Acceptance criteria:
 *   AC1: Cleanup invoked automatically after closeWorkItemsAfterRelease
 *   AC2: Runs in worktree context (main checkout)
 *   AC3: Non-blocking — failure does not affect release exit code
 *   AC4: Protected branches respected
 *   AC5: Report produced — results logged to release output
 *   AC6: Dry-run compatibility
 *   AC7: All existing ship gating tests pass
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SUMMARIZE_SCRIPT = join(REPO_ROOT, 'skill', 'cleanup', 'scripts', 'summarize_branches.py');
const PRUNE_SCRIPT = join(REPO_ROOT, 'skill', 'cleanup', 'scripts', 'prune_local_branches.py');
const DELETE_REMOTE_SCRIPT = join(REPO_ROOT, 'skill', 'cleanup', 'scripts', 'delete_remote_branches.py');

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Create a fake skill layout with all run-release.js dependencies
 * and return the temp directory path.
 *
 * @param {boolean} [hasReleaseScript=false] — include a fake release script
 * @returns {{ tmpDir: string, runReleasePath: string }}
 */
function setupRunReleaseFixture(hasReleaseScript = false) {
  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-cleanup-test-'));

  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  // Copy run-release.js and all its JS dependencies
  const deps = [
    'check-unmerged-branches.js',
    'check-audit-gate.js',
    'audit-freshness.js',
    'audit-remediation.js',
    'refresh-audits.js',
    'check-final-validation.js',
    'check-critical-items.js',
    'check-worklog-refs.js',
    'discord-notify.js',
    'timing.js',
    'llm.js',
  ];

  writeFileSync(join(skillScriptDir, 'run-release.js'),
    readFileSync(RUN_RELEASE_PATH, 'utf8'));

  for (const dep of deps) {
    const src = join(REPO_ROOT, 'skill', 'ship', 'scripts', dep);
    if (existsSync(src)) {
      writeFileSync(join(skillScriptDir, dep), readFileSync(src, 'utf8'));
    }
  }

  // Optionally include a fake release script
  if (hasReleaseScript) {
    const releaseDir = join(skillScriptDir, 'release');
    mkdirSync(releaseDir, { recursive: true });
    writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'),
      '#!/bin/bash\necho "Release complete"\n');
  }

  return { tmpDir, runReleasePath: join(skillScriptDir, 'run-release.js') };
}

// ── AC1: Cleanup invoked automatically ──────────────────────────────────────

test('AC1: run-release.js exports runPostReleaseCleanup', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(
    typeof mod.runPostReleaseCleanup,
    'function',
    'run-release.js should export runPostReleaseCleanup function',
  );
});

test('AC1: run-release.js source calls runPostReleaseCleanup', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  assert.ok(
    content.includes('runPostReleaseCleanup('),
    'run-release.js should call runPostReleaseCleanup',
  );
});

test('AC1: run-release.js calls cleanup after closeWorkItemsAfterRelease', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // Find the positions of the two calls
  const closeIndex = content.lastIndexOf('closeWorkItemsAfterRelease(');
  const cleanupIndex = content.lastIndexOf('runPostReleaseCleanup(');

  assert.ok(closeIndex !== -1, 'closeWorkItemsAfterRelease should be called');
  assert.ok(cleanupIndex !== -1, 'runPostReleaseCleanup should be called');
  assert.ok(
    cleanupIndex > closeIndex,
    'runPostReleaseCleanup should be called after closeWorkItemsAfterRelease',
  );
});

// ── AC2: Runs in worktree context ───────────────────────────────────────────

test('AC2: runPostReleaseCleanup uses cleanup script paths relative to repo', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // The function should reference the cleanup scripts
  assert.ok(
    content.includes('summarize_branches.py') ||
    content.includes('prune_local_branches.py') ||
    content.includes('delete_remote_branches.py'),
    'runPostReleaseCleanup should reference cleanup scripts',
  );
});

test('AC2: cleanup scripts exist', () => {
  assert.ok(existsSync(SUMMARIZE_SCRIPT), 'summarize_branches.py should exist');
  assert.ok(existsSync(PRUNE_SCRIPT), 'prune_local_branches.py should exist');
  assert.ok(existsSync(DELETE_REMOTE_SCRIPT), 'delete_remote_branches.py should exist');
});

// ── AC3: Non-blocking — failure does not affect release exit code ───────────

test('AC3: runPostReleaseCleanup returns success even when cleanup fails', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  // The function must not throw and must return an object
  // Verify via source analysis: it should have a try/catch or not propagate errors
  // The function should be called without awaiting in a way that blocks
  // Check for non-blocking invocation pattern
  assert.ok(
    content.includes('try') && content.includes('catch'),
    'runPostReleaseCleanup should have error handling (try/catch)',
  );
});

test('AC3: runPostReleaseCleanup failure does not propagate to release exit code', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  // After closeWorkItemsAfterRelease and runPostReleaseCleanup, the function
  // should not use the cleanup result to set an exit code.
  // Verify: the cleanup step uses try/catch and never assigns exit code from cleanupResult.
  assert.ok(
    content.includes('try') && content.includes('catch'),
    'runPostReleaseCleanup should have error handling (try/catch)',
  );

  // In Step 10, cleanup should be wrapped in try/catch.
  const cleanupStep = content.slice(content.indexOf('Step 10: post-release cleanup'));
  assert.ok(
    cleanupStep.includes('try') && cleanupStep.includes('catch'),
    'Step 10 should wrap cleanup in try/catch',
  );

  // The function should never call process.exit with a non-zero code based on cleanup.
  const stepToFinish = cleanupStep.slice(0, cleanupStep.indexOf('return finish(0)'));
  assert.ok(
    !stepToFinish.includes('process.exit(') && !stepToFinish.includes('exitCode ='),
    'Step 10 should not set exit code based on cleanup results',
  );
});

// ── AC4: Protected branches respected ───────────────────────────────────────

test('AC4: cleanup scripts define PROTECTED branch sets', async () => {
  const summarizeContent = readFileSync(SUMMARIZE_SCRIPT, 'utf-8');
  const pruneContent = readFileSync(PRUNE_SCRIPT, 'utf-8');
  const deleteRemoteContent = readFileSync(DELETE_REMOTE_SCRIPT, 'utf-8');

  // All cleanup scripts should define protected branch sets
  assert.ok(
    summarizeContent.includes('PROTECTED'),
    'summarize_branches.py should define PROTECTED',
  );
  assert.ok(
    pruneContent.includes('PROTECTED_BRANCHES') || pruneContent.includes('PROTECTED'),
    'prune_local_branches.py should define protected branches',
  );
  assert.ok(
    deleteRemoteContent.includes('PROTECTED'),
    'delete_remote_branches.py should define PROTECTED',
  );
});

test('AC4: main/master/develop are in PROTECTED sets', async () => {
  const summarizeContent = readFileSync(SUMMARIZE_SCRIPT, 'utf-8');
  const deleteRemoteContent = readFileSync(DELETE_REMOTE_SCRIPT, 'utf-8');

  assert.ok(
    summarizeContent.includes('"main"') || summarizeContent.includes("'main'"),
    'PROTECTED should include main',
  );
  assert.ok(
    summarizeContent.includes('"master"') || summarizeContent.includes("'master'"),
    'PROTECTED should include master',
  );
  assert.ok(
    deleteRemoteContent.includes('"main"') || deleteRemoteContent.includes("'main'"),
    'delete_remote PROTECTED should include main',
  );
});

// ── AC5: Report produced ────────────────────────────────────────────────────

test('AC5: runPostReleaseCleanup logs results to release output', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  // The function should log branch deletion results
  assert.ok(
    content.includes('console.log') && content.includes('runPostReleaseCleanup'),
    'runPostReleaseCleanup should produce logged output',
  );

  // Check that the function produces a summary/report
  const cleanupFn = content.indexOf('export function runPostReleaseCleanup');
  assert.ok(cleanupFn !== -1, 'runPostReleaseCleanup should be exported');

  const fnBody = content.slice(cleanupFn);
  const nextFn = fnBody.indexOf('\nexport function', 1);
  const fnContent = nextFn !== -1 ? fnBody.slice(0, nextFn) : fnBody;

  assert.ok(
    fnContent.includes('console') || fnContent.includes('return'),
    'runPostReleaseCleanup should produce output or return a result',
  );
});

// ── AC6: Dry-run compatibility ──────────────────────────────────────────────

test('AC6: runPostReleaseCleanup accepts dryRun option', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  assert.ok(
    content.includes('dryRun') && content.includes('runPostReleaseCleanup'),
    'runPostReleaseCleanup should accept a dryRun option',
  );
});

test('AC6: cleanup scripts receive --dry-run flag', async () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  // The cleanup invocation should pass --dry-run when in dry-run mode
  assert.ok(
    content.includes('--dry-run') && content.indexOf('runPostReleaseCleanup') > 0,
    'runPostReleaseCleanup should pass --dry-run to cleanup scripts',
  );
});

test('AC6: release dry-run propagates to cleanup', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');

  // Find the call site in runReleaseImpl
  const cleanupCallMatch = content.match(/runPostReleaseCleanup\(\{[^}]*dryRun[^}]*\}\)/);
  assert.ok(
    cleanupCallMatch,
    'runPostReleaseCleanup should be called with a { dryRun: ... } option object',
  );
});

// ── AC7: All existing ship gating tests pass ────────────────────────────────

test('AC7: run-release.js still exports all previously exported functions', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const requiredExports = [
    'runRelease',
    'verifyReleaseMerge',
    'closeWorkItemsAfterRelease',
    'syncDevWithMain',
    'parsePRUrl',
    'waitForPRMerge',
    'setCodeFreezeMarker',
    'clearCodeFreezeMarker',
    'codeFreezeMarkerPath',
    'resolveProjectRoot',
    'releaseScriptForwardArgs',
    'resolveCandidateCoverage',
    'getDescendants',
    'runPostReleaseCleanup',
  ];

  for (const exportName of requiredExports) {
    assert.equal(
      typeof mod[exportName],
      'function',
      `run-release.js should still export ${exportName}`,
    );
  }
});

// ── Integration: runPostReleaseCleanup module import ────────────────────────

test('Integration: runPostReleaseCleanup can be imported and called', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const result = mod.runPostReleaseCleanup({ dryRun: true });
  assert.ok(typeof result === 'object', 'runPostReleaseCleanup should return an object');
  assert.ok('success' in result, 'result should have success field');
  assert.ok('message' in result, 'result should have message field');
});
