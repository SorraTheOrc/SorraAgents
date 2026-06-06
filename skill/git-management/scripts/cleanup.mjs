#!/usr/bin/env node
/**
 * cleanup.mjs — Post-merge branch pruning, delegates to existing cleanup infrastructure.
 *
 * Usage:
 *   node skill/git-management/scripts/cleanup.mjs [--days <n>] [--dry-run] [--json] [--report <path>]
 *
 * Delegates to skill/cleanup/scripts/ for branch pruning rather than
 * reimplementing cleanup logic.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error
 *   2 — Safety violation (dirty worktree)
 *   3 — Prerequisite not met (missing cleanup scripts)
 */

import {
  parseArgs,
  hasFlag,
  getFlag,
  jsonOutput,
  humanMsg,
  humanError,
  humanSuccess,
  checkPrerequisites,
  safeExec,
  EXIT,
} from './git-mgmt-helpers.mjs';

import { existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SKILL_DIR = join(__dirname, '..');
const REPO_ROOT = join(SKILL_DIR, '..', '..');

// ── Locate cleanup scripts ──────────────────────────────────────────────────

const CLEANUP_SCRIPTS_DIR = join(REPO_ROOT, 'skill', 'cleanup', 'scripts');

/**
 * Find available cleanup scripts.
 * @returns {string[]}
 */
function findCleanupScripts() {
  if (!existsSync(CLEANUP_SCRIPTS_DIR)) {
    return [];
  }

  const scripts = [];
  try {
    for (const f of readdirSync(CLEANUP_SCRIPTS_DIR)) {
      if (f.endsWith('.py') && !f.startsWith('__')) {
        scripts.push(join(CLEANUP_SCRIPTS_DIR, f));
      }
    }
  } catch {
    // ignore
  }
  return scripts;
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const days = getFlag(flags, 'days') || '14';
  const reportPath = getFlag(flags, 'report') || '/tmp/git-mgmt-cleanup.json';

  // Check prerequisites
  const prereq = checkPrerequisites(['git', 'python3'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Check cleanup infrastructure exists
  const availableScripts = findCleanupScripts();
  if (availableScripts.length === 0) {
    const msg = `Cleanup infrastructure not found at ${CLEANUP_SCRIPTS_DIR}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.PREREQ_NOT_MET);
    humanError(msg, EXIT.PREREQ_NOT_MET);
  }

  // Check worktree is clean (safety — don't clean up branches with uncommitted work)
  const statusResult = safeExec('git status --porcelain');
  if (statusResult.success && statusResult.stdout.trim() !== '') {
    const msg = 'Working tree has uncommitted changes. Commit or stash before cleanup.';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Determine which branches to clean up
  // Use the summarize_branches script to get candidate branches
  const summarizeScript = join(CLEANUP_SCRIPTS_DIR, 'summarize_branches.py');
  const pruneScript = join(CLEANUP_SCRIPTS_DIR, 'prune_local_branches.py');
  const deleteRemoteScript = join(CLEANUP_SCRIPTS_DIR, 'delete_remote_branches.py');

  // Dry-run mode
  if (dryRun) {
    // Run summarize in dry-run to show what would be cleaned
    let summarizeOutput = '';
    if (existsSync(summarizeScript)) {
      const sumResult = safeExec(`python3 "${summarizeScript}" --dry-run --report "${reportPath}"`);
      summarizeOutput = sumResult.success ? sumResult.stdout : sumResult.stderr;
    }

    const result = {
      success: true,
      dryRun: true,
      reportPath,
      days: parseInt(days, 10),
      availableScripts: availableScripts.map(s => s.replace(REPO_ROOT + '/', '')),
      message: 'Dry-run cleanup — no branches were deleted',
      summary: summarizeOutput,
    };

    if (asJson) jsonOutput(result);
    humanSuccess({
      message: `[DRY RUN] Cleanup would prune merged branches older than ${days} days`,
      details: {
        reportPath,
        availableScripts: availableScripts.map(s => s.replace(REPO_ROOT + '/', '')).join(', '),
      },
    });
  }

  // Actual cleanup — delegate to existing scripts
  const results = [];

  // Step 1: Summarize branches
  if (existsSync(summarizeScript)) {
    humanMsg('Summarizing branches...');
    const sumResult = safeExec(`python3 "${summarizeScript}" --report "${reportPath}"`);
    results.push({
      step: 'summarize',
      script: 'summarize_branches.py',
      success: sumResult.success,
      output: sumResult.stdout || sumResult.stderr,
    });
    if (!sumResult.success) {
      humanMsg(`Warning: summarize_branches.py returned: ${sumResult.stderr}`);
    }
  }

  // Step 2: Prune local merged branches
  if (existsSync(pruneScript)) {
    humanMsg('Pruning local merged branches...');
    const pruneResult = safeExec(`python3 "${pruneScript}" --report "${reportPath}"`);
    results.push({
      step: 'prune_local',
      script: 'prune_local_branches.py',
      success: pruneResult.success,
      output: pruneResult.stdout || pruneResult.stderr,
    });
    if (!pruneResult.success) {
      humanMsg(`Warning: prune_local_branches.py returned: ${pruneResult.stderr}`);
    }
  }

  // Step 3: Delete remote merged branches older than N days
  if (existsSync(deleteRemoteScript)) {
    humanMsg(`Deleting remote merged branches older than ${days} days...`);
    const remoteResult = safeExec(`python3 "${deleteRemoteScript}" --days ${days} --report "${reportPath}"`);
    results.push({
      step: 'delete_remote',
      script: 'delete_remote_branches.py',
      success: remoteResult.success,
      output: remoteResult.stdout || remoteResult.stderr,
    });
    if (!remoteResult.success) {
      humanMsg(`Warning: delete_remote_branches.py returned: ${remoteResult.stderr}`);
    }
  }

  const allSuccess = results.every(r => r.success);
  const result = {
    success: allSuccess,
    reportPath,
    days: parseInt(days, 10),
    steps: results,
    message: allSuccess ? 'Cleanup completed successfully' : 'Cleanup completed with warnings',
  };

  if (asJson) jsonOutput(result, allSuccess ? EXIT.SUCCESS : EXIT.GENERAL_ERROR);
  humanSuccess({
    message: allSuccess ? 'Cleanup completed successfully' : 'Cleanup completed with warnings',
    details: {
      reportPath,
      stepsRun: results.length,
      stepsSucceeded: results.filter(r => r.success).length,
    },
  });
}

main().catch(err => {
  humanError(`Unexpected error: ${err.message}`, EXIT.GENERAL_ERROR);
});
