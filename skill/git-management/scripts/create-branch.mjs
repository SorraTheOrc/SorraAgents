#!/usr/bin/env node
/**
 * create-branch.mjs — Create and check out a canonical feature branch.
 *
 * Usage:
 *   node skill/git-management/scripts/create-branch.mjs <work-item-id> <short-desc> [--dry-run] [--json]
 *
 * Creates a branch following the wl-<work-item-id>-<short-desc> pattern,
 * validates the name, and checks it out.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error (missing args, invalid inputs)
 *   2 — Safety violation (protected branch, existing branch collision)
 *   3 — Prerequisite not met (not in a git repo)
 */

import {
  parseArgs,
  hasFlag,
  jsonOutput,
  humanMsg,
  humanError,
  humanSuccess,
  checkPrerequisites,
  safeExec,
  validateWorkItemId,
  makeSlug,
  EXIT,
} from './git-mgmt-helpers.mjs';

import {
  makeBranchName,
  validateBranchName,
  isBranchBlocked,
} from '../../ship/scripts/git-helpers.js';

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags, positional } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');

  // Need work-item ID and short description
  if (positional.length < 2) {
    const msg = 'Usage: create-branch <work-item-id> <short-desc> [--dry-run] [--json]';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const workItemId = positional[0];
  const shortDesc = positional.slice(1).join('-');

  // Validate work-item ID
  const idValidation = validateWorkItemId(workItemId);
  if (!idValidation.valid) {
    if (asJson) jsonOutput({ success: false, error: idValidation.reason }, EXIT.GENERAL_ERROR);
    humanError(idValidation.reason, EXIT.GENERAL_ERROR);
  }

  // Validate short description produces a non-empty slug
  const slug = makeSlug(shortDesc);
  if (!slug) {
    const msg = 'Short description must produce a non-empty slug after sanitization';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Check prerequisites
  const prereq = checkPrerequisites(['git'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Generate and validate branch name
  const branchName = makeBranchName(workItemId, slug);
  const nameValidation = validateBranchName(branchName);
  if (!nameValidation.valid) {
    const msg = `Generated branch name is invalid: ${nameValidation.reason}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check the branch name is not a protected branch
  if (isBranchBlocked(branchName)) {
    const msg = `Branch name "${branchName}" matches a protected branch`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Check if branch already exists
  const existingCheck = safeExec(`git branch --list "${branchName}"`);
  if (existingCheck.success && existingCheck.stdout.trim() !== '') {
    const msg = `Branch "${branchName}" already exists`;
    if (asJson) jsonOutput({ success: false, error: msg, code: 'BRANCH_EXISTS' }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Dry-run: report what would happen
  if (dryRun) {
    const result = {
      success: true,
      dryRun: true,
      branchName,
      workItemId,
      message: `Would create and check out branch: ${branchName}`,
    };
    if (asJson) jsonOutput(result);
    humanSuccess({
      message: `[DRY RUN] Would create and check out branch: ${branchName}`,
      details: { workItemId, branchName },
    });
  }

  // Create and check out the branch
  const createResult = safeExec(`git checkout -b "${branchName}"`);
  if (!createResult.success) {
    const msg = `Failed to create branch: ${createResult.stderr}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const result = {
    success: true,
    branchName,
    workItemId,
    message: `Created and checked out branch: ${branchName}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Created and checked out branch: ${branchName}`,
    details: { workItemId, branchName },
  });
}

main();
