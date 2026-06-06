#!/usr/bin/env node
/**
 * push.mjs — Push to remote with safety checks (no force-push, no direct-to-main).
 *
 * Usage:
 *   node skill/git-management/scripts/push.mjs [--remote <name>] [--into-dev] [--dry-run] [--json]
 *
 * Pushes the current branch to origin with safety validation.
 * Delegates push policy to skill/ship/scripts/ship.js.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error
 *   2 — Safety violation (protected branch, force-push request)
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
  pushToDev,
  pushToBranch,
  validatePushTarget,
  validateForcePush,
  validateBranchName,
  isBranchBlocked,
} from '../../ship/scripts/ship.js';

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const intoDev = hasFlag(flags, 'into-dev');
  const remote = getFlag(flags, 'remote') || 'origin';

  // Check prerequisites
  const prereq = checkPrerequisites(['git'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Get current branch
  const branchResult = safeExec('git rev-parse --abbrev-ref HEAD');
  if (!branchResult.success) {
    const msg = 'Unable to determine current branch';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }
  const currentBranch = branchResult.stdout;

  // Validate current branch name
  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    const msg = `Current branch "${currentBranch}" is not a valid agent branch: ${branchValidation.reason}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check current branch is not protected
  if (isBranchBlocked(currentBranch)) {
    const msg = `Cannot push from protected branch "${currentBranch}"`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check remote exists
  const remoteCheck = safeExec(`git remote get-url ${remote}`);
  if (!remoteCheck.success) {
    const msg = `Remote "${remote}" is not configured`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.PREREQ_NOT_MET);
    humanError(msg, EXIT.PREREQ_NOT_MET);
  }

  // Dry-run
  if (dryRun) {
    if (intoDev) {
      const targetValidation = validatePushTarget('dev');
      if (!targetValidation.allowed) {
        if (asJson) jsonOutput({ success: false, error: targetValidation.reason }, EXIT.SAFETY_VIOLATION);
        humanError(targetValidation.reason, EXIT.SAFETY_VIOLATION);
      }
      const result = {
        success: true,
        dryRun: true,
        command: `git push ${remote} HEAD:refs/heads/dev`,
        currentBranch,
        message: `Would push ${currentBranch} into dev via ${remote}`,
      };
      if (asJson) jsonOutput(result);
      humanSuccess({
        message: `[DRY RUN] Would push ${currentBranch} into dev via ${remote}`,
        details: { currentBranch, target: 'dev', remote },
      });
    } else {
      const result = {
        success: true,
        dryRun: true,
        command: `git push ${remote} HEAD:refs/heads/${currentBranch}`,
        currentBranch,
        message: `Would push ${currentBranch} to ${remote}`,
      };
      if (asJson) jsonOutput(result);
      humanSuccess({
        message: `[DRY RUN] Would push ${currentBranch} to ${remote}`,
        details: { currentBranch, remote },
      });
    }
  }

  // Perform push
  let pushResult;
  if (intoDev) {
    pushResult = pushToDev({ remote });
  } else {
    pushResult = pushToBranch(currentBranch, { remote });
  }

  if (!pushResult.success) {
    if (asJson) jsonOutput({ success: false, error: pushResult.error }, EXIT.GENERAL_ERROR);
    humanError(pushResult.error, EXIT.GENERAL_ERROR);
  }

  const result = {
    success: true,
    currentBranch,
    remote,
    targetBranch: intoDev ? 'dev' : currentBranch,
    command: pushResult.command,
    message: `Pushed ${currentBranch} to ${remote}/${intoDev ? 'dev' : currentBranch}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Pushed ${currentBranch} to ${remote}/${intoDev ? 'dev' : currentBranch}`,
    details: { currentBranch, remote, targetBranch: intoDev ? 'dev' : currentBranch },
  });
}

main();
