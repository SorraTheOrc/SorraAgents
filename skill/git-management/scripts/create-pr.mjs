#!/usr/bin/env node
/**
 * create-pr.mjs — Create a GitHub Pull Request from the current branch.
 *
 * Usage:
 *   node skill/git-management/scripts/create-pr.mjs [--base <branch>] [--title <title>] [--body <body>] [--draft] [--dry-run] [--json]
 *
 * Creates a PR using the gh CLI. Validates prerequisites and branch state.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error
 *   2 — Safety violation
 *   3 — Prerequisite not met (missing gh, not authenticated)
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
  validateBranchName,
} from '../../ship/scripts/git-helpers.js';

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const draft = hasFlag(flags, 'draft');
  const base = getFlag(flags, 'base') || 'dev';
  const title = getFlag(flags, 'title');
  const body = getFlag(flags, 'body');

  // Check prerequisites
  const prereq = checkPrerequisites(['git', 'gh'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Check gh is authenticated
  const authCheck = safeExec('gh auth status', { stdio: ['pipe', 'pipe', 'pipe'] });
  if (!authCheck.success) {
    const msg = 'GitHub CLI is not authenticated. Run `gh auth login` first.';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.PREREQ_NOT_MET);
    humanError(msg, EXIT.PREREQ_NOT_MET);
  }

  // Get current branch
  const branchResult = safeExec('git rev-parse --abbrev-ref HEAD');
  if (!branchResult.success) {
    const msg = 'Unable to determine current branch';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }
  const currentBranch = branchResult.stdout;

  // Validate current branch
  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    const msg = `Current branch "${currentBranch}" is not a valid agent branch`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check current branch is not protected
  if (isBranchBlocked(currentBranch)) {
    const msg = `Cannot create a PR from protected branch "${currentBranch}"`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check base branch is not protected for source
  if (isBranchBlocked(base)) {
    const msg = `Cannot use protected branch "${base}" as PR source`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check branch has been pushed to remote
  const remoteBranchCheck = safeExec(`git rev-parse --verify origin/${currentBranch}`);
  if (!remoteBranchCheck.success) {
    const msg = `Branch "${currentBranch}" has not been pushed to remote. Push first before creating a PR.`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Build title from commit if not provided
  let prTitle = title;
  if (!prTitle) {
    const lastCommitResult = safeExec('git log -1 --format=%s');
    if (lastCommitResult.success) {
      prTitle = lastCommitResult.stdout;
    } else {
      prTitle = currentBranch;
    }
  }

  // Build body if not provided
  let prBody = body || '';
  if (!prBody) {
    // Try to get commit body as PR description
    const commitBodyResult = safeExec('git log -1 --format=%b');
    if (commitBodyResult.success && commitBodyResult.stdout.trim()) {
      prBody = commitBodyResult.stdout;
    }
  }

  // Build gh command
  let ghCmd = `gh pr create --base "${base}" --head "${currentBranch}" --title "${prTitle.replace(/"/g, '\\"')}"`;
  if (prBody) {
    ghCmd += ` --body "${prBody.replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`;
  }
  if (draft) {
    ghCmd += ' --draft';
  }

  // Dry-run
  if (dryRun) {
    const result = {
      success: true,
      dryRun: true,
      command: ghCmd,
      currentBranch,
      base,
      title: prTitle,
      message: `Would create PR: ${prTitle} (${currentBranch} → ${base})`,
    };
    if (asJson) jsonOutput(result);
    humanSuccess({
      message: `[DRY RUN] Would create PR: ${prTitle} (${currentBranch} → ${base})`,
      details: { currentBranch, base, title: prTitle, draft },
    });
  }

  // Create PR
  const prResult = safeExec(ghCmd);
  if (!prResult.success) {
    const msg = `PR creation failed: ${prResult.stderr}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Extract PR URL and number from output
  const urlMatch = prResult.stdout.match(/(https:\/\/github\.com\/[^\/]+\/[^\/]+\/pull\/\d+)/);
  const numberMatch = prResult.stdout.match(/#(\d+)/);
  const prUrl = urlMatch ? urlMatch[1] : '';
  const prNumber = numberMatch ? numberMatch[1] : '';

  const result = {
    success: true,
    prUrl,
    prNumber,
    currentBranch,
    base,
    title: prTitle,
    message: `Created PR #${prNumber}: ${prTitle}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Created PR #${prNumber}: ${prTitle}`,
    details: { prUrl, currentBranch, base },
  });
}

main();
