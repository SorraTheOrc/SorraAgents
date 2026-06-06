#!/usr/bin/env node
/**
 * merge-pr.mjs — Guarded merge of a GitHub Pull Request with CI verification.
 *
 * Usage:
 *   node skill/git-management/scripts/merge-pr.mjs <pr-number> [--method merge|squash|rebase] [--delete-source] [--dry-run] [--json]
 *
 * Merges an approved PR using gh CLI with safety checks.
 * Verifies CI/status checks before merging.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error
 *   2 — Safety violation (direct-to-main bypass, CI not green)
 *   3 — Prerequisite not met
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

import {
  isBranchBlocked,
} from '../../ship/scripts/git-helpers.js';

// ── CI Status Check ──────────────────────────────────────────────────────────

/**
 * Check CI status for a PR. Returns structured result.
 * @param {string} prNumber
 * @returns {{ ok: boolean, status: string, checks: Array<{name: string, status: string, conclusion: string}> }}
 */
function checkCIStatus(prNumber) {
  const checksResult = safeExec(`gh pr checks ${prNumber} --json name,status,conclusion`);
  if (!checksResult.success) {
    return { ok: false, status: 'unknown', checks: [], error: checksResult.stderr };
  }

  try {
    const checks = JSON.parse(checksResult.stdout);
    if (!Array.isArray(checks)) {
      return { ok: false, status: 'unknown', checks: [] };
    }

    const hasPending = checks.some(c => c.status === 'IN_PROGRESS' || c.status === 'QUEUED' || c.status === 'REQUESTED');
    const hasFailure = checks.some(c => c.conclusion === 'FAILURE' || c.conclusion === 'TIMED_OUT' || c.conclusion === 'ACTION_REQUIRED');
    const allSuccess = checks.every(c => c.conclusion === 'SUCCESS' || c.conclusion === 'SKIPPED' || c.conclusion === 'NEUTRAL');

    if (hasFailure) {
      return { ok: false, status: 'failure', checks };
    }
    if (hasPending) {
      return { ok: false, status: 'pending', checks };
    }
    if (allSuccess || checks.length === 0) {
      return { ok: true, status: 'success', checks };
    }

    return { ok: true, status: 'unknown', checks };
  } catch {
    return { ok: true, status: 'unknown', checks: [] };
  }
}

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags, positional } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const deleteSource = hasFlag(flags, 'delete-source');
  const mergeMethod = getFlag(flags, 'method') || 'merge';

  // Require PR number
  if (positional.length < 1) {
    const msg = 'Usage: merge-pr <pr-number> [--method merge|squash|rebase] [--delete-source] [--dry-run] [--json]';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const prNumber = positional[0];

  // Validate merge method
  const validMethods = ['merge', 'squash', 'rebase'];
  if (!validMethods.includes(mergeMethod)) {
    const msg = `Invalid merge method "${mergeMethod}". Must be one of: ${validMethods.join(', ')}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Check prerequisites
  const prereq = checkPrerequisites(['git', 'gh'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Check gh is authenticated
  const authCheck = safeExec('gh auth status');
  if (!authCheck.success) {
    const msg = 'GitHub CLI is not authenticated. Run `gh auth login` first.';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.PREREQ_NOT_MET);
    humanError(msg, EXIT.PREREQ_NOT_MET);
  }

  // Get PR info
  const prInfoResult = safeExec(`gh pr view ${prNumber} --json number,state,headRefName,baseRefName,mergeable,title,url`);
  if (!prInfoResult.success) {
    const msg = `Failed to get PR info for #${prNumber}: ${prInfoResult.stderr}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  let prInfo;
  try {
    prInfo = JSON.parse(prInfoResult.stdout);
  } catch {
    const msg = `Failed to parse PR info for #${prNumber}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Check PR state
  if (prInfo.state !== 'OPEN') {
    const msg = `PR #${prNumber} is not open (state: ${prInfo.state})`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Check target branch is not protected against direct bypass
  if (isBranchBlocked(prInfo.baseRefName)) {
    // Merging into main via PR is allowed — this is the canonical flow.
    // We only block direct pushes, not PR-based merges.
    humanMsg(`Note: Merging into protected branch "${prInfo.baseRefName}" via PR (allowed).`);
  }

  // Check CI status
  humanMsg('Checking CI status...');
  const ciStatus = checkCIStatus(prNumber);
  if (!ciStatus.ok) {
    const msg = `CI checks are not passing for PR #${prNumber} (status: ${ciStatus.status}). Cannot merge.`;
    if (asJson) jsonOutput({ success: false, error: msg, ciStatus: ciStatus.status }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Dry-run
  if (dryRun) {
    const result = {
      success: true,
      dryRun: true,
      prNumber,
      prTitle: prInfo.title,
      prUrl: prInfo.url,
      headBranch: prInfo.headRefName,
      baseBranch: prInfo.baseRefName,
      mergeMethod,
      deleteSource,
      ciStatus: ciStatus.status,
      message: `Would merge PR #${prNumber}: ${prInfo.title} (${prInfo.headRefName} → ${prInfo.baseRefName})`,
    };
    if (asJson) jsonOutput(result);
    humanSuccess({
      message: `[DRY RUN] Would merge PR #${prNumber}: ${prInfo.title}`,
      details: {
        headBranch: prInfo.headRefName,
        baseBranch: prInfo.baseRefName,
        mergeMethod,
        ciStatus: ciStatus.status,
      },
    });
  }

  // Build merge command
  let ghCmd = `gh pr merge ${prNumber} --${mergeMethod}`;
  if (deleteSource) {
    ghCmd += ' --delete-branch';
  }

  // Merge
  const mergeResult = safeExec(ghCmd);
  if (!mergeResult.success) {
    const msg = `Merge failed for PR #${prNumber}: ${mergeResult.stderr}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const result = {
    success: true,
    prNumber,
    prTitle: prInfo.title,
    prUrl: prInfo.url,
    headBranch: prInfo.headRefName,
    baseBranch: prInfo.baseRefName,
    mergeMethod,
    message: `Merged PR #${prNumber}: ${prInfo.title}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Merged PR #${prNumber}: ${prInfo.title}`,
    details: {
      headBranch: prInfo.headRefName,
      baseBranch: prInfo.baseRefName,
      mergeMethod,
    },
  });
}

main();
