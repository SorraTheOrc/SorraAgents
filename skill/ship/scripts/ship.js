/**
 * ship.js — Agent push-to-dev behaviour module.
 *
 * Provides the canonical helper functions agents use to push completed
 * feature branch work into the `dev` integration branch. Agents must
 * never push directly to `main`.
 *
 * Key behaviours:
 *   1. Agents create feature branches using the canonical naming pattern
 *      via `makeBranchName()` from `agent/git-helpers.js`.
 *   2. Agents push completed work into `dev` using `pushToDev()`.
 *   3. Push to `main` is always rejected.
 *   4. Force-push / history-rewrite is always rejected.
 *   5. On push failure (e.g., non-fast-forward due to conflicts), the
 *      agent returns a non-zero status and does NOT rewrite history.
 *
 * Usage:
 *
 *   import { pushToDev, validatePushTarget, validateForcePush } from './agent/ship.js';
 *
 *   // Push completed work into dev
 *   const result = pushToDev('origin');
 *   if (!result.success) {
 *     // handle failure — e.g., create a merge-conflict work item
 *   }
 */

import { execSync } from 'node:child_process';
import { isBranchBlocked, validateBranchName, makeBranchName } from './git-helpers.js';
import { checkUnmergedBranches } from './check-unmerged-branches.js';
import { checkAuditReadyToClose } from './check-audit-gate.js';

// ── Constants ────────────────────────────────────────────────────────────────

/** The integration branch that agents push into. */
export const DEV_BRANCH = 'dev';

/** Branches that agents must never push to directly. */
export const PROTECTED_BRANCHES = Object.freeze(['main', 'master', 'HEAD']);

// ── validatePushTarget ───────────────────────────────────────────────────────

/**
 * Determine whether a push target branch is allowed for agents.
 * Agents may push feature branches to origin and merge into dev,
 * but must NEVER push directly to protected branches.
 *
 * @param {string} targetBranch - The target branch name.
 * @returns {{ allowed: boolean, reason?: string }}
 */
export function validatePushTarget(targetBranch) {
  if (!targetBranch || typeof targetBranch !== 'string') {
    return {
      allowed: false,
      reason: 'Push target must be a non-empty string',
    };
  }

  if (PROTECTED_BRANCHES.includes(targetBranch)) {
    return {
      allowed: false,
      reason: `Agents must not push directly to '${targetBranch}'. Push to '${DEV_BRANCH}' instead.`,
    };
  }

  return { allowed: true };
}

// ── validateForcePush ────────────────────────────────────────────────────────

/**
 * Determine whether a force-push is allowed.
 * Agents must never force-push or rewrite history.
 *
 * @returns {{ allowed: false, reason: string }}
 */
export function validateForcePush() {
  return {
    allowed: false,
    reason: 'Force-push / history-rewrite is not permitted for agents.',
  };
}

// ── pushToDev ────────────────────────────────────────────────────────────────

/**
 * Push the current feature branch into the `dev` integration branch.
 *
 * This is the canonical push routine that agents call instead of running
 * `git push` directly. It performs all required validations before executing
 * the push.
 *
 * Push command executed: `git push <remote> HEAD:refs/heads/dev`
 *
 * @param {object} [opts] - Options.
 * @param {string} [opts.remote='origin'] - The remote name.
 * @param {boolean} [opts.force=false] - Whether to force-push (always rejected).
 * @returns {{ success: boolean, error?: string, command?: string }}
 */
export function pushToDev(opts = {}) {
  const { remote = 'origin', force = false } = opts;

  // Step 1: validate force-push (always rejected)
  if (force) {
    const forceValidation = validateForcePush();
    return { success: false, error: forceValidation.reason };
  }

  // Step 2: validate push target
  const targetValidation = validatePushTarget(DEV_BRANCH);
  if (!targetValidation.allowed) {
    return { success: false, error: targetValidation.reason };
  }

  // Step 3: validate current branch name
  let currentBranch;
  try {
    currentBranch = execSync('git rev-parse --abbrev-ref HEAD', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return { success: false, error: 'Unable to determine current git branch' };
  }

  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    return {
      success: false,
      error: `Current branch "${currentBranch}" is not a valid agent branch: ${branchValidation.reason}`,
    };
  }

  // Step 4: check for unmerged branches (gating step)
  const unmergedCheck = checkUnmergedBranches();
  if (unmergedCheck.hasUnmergedBranches) {
    return {
      success: false,
      error: `Cannot push to '${DEV_BRANCH}' — there are unmerged branches that should be resolved first:\n\n${unmergedCheck.message}`,
    };
  }

  // Step 5: execute the push
  const command = `git push ${remote} HEAD:refs/heads/${DEV_BRANCH}`;
  try {
    execSync(command, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return { success: true, command };
  } catch (err) {
    const stderr = err.stderr?.toString() || '';
    // Detect non-fast-forward (conflict scenario)
    if (stderr.includes('non-fast-forward') || stderr.includes('[rejected]')) {
      return {
        success: false,
        error: `Push to '${DEV_BRANCH}' was rejected (non-fast-forward / conflict). Record the conflict details in a comment on the owning work item and resolve manually.`,
        command,
      };
    }
    return {
      success: false,
      error: `Push to '${DEV_BRANCH}' failed: ${stderr.trim()}`,
      command,
    };
  }
}

// ── pushToBranch ─────────────────────────────────────────────────────────────

/**
 * Push the current branch to a specific remote branch, with full validation.
 * Use this for pushing feature branches to origin (not for pushing to dev).
 *
 * @param {string} targetBranch - The target branch name.
 * @param {object} [opts] - Options.
 * @param {string} [opts.remote='origin'] - The remote name.
 * @param {boolean} [opts.force=false] - Whether to force-push (always rejected).
 * @returns {{ success: boolean, error?: string, command?: string }}
 */
export function pushToBranch(targetBranch, opts = {}) {
  const { remote = 'origin', force = false } = opts;

  // Step 1: validate force-push (always rejected)
  if (force) {
    const forceValidation = validateForcePush();
    return { success: false, error: forceValidation.reason };
  }

  // Step 2: validate push target
  const targetValidation = validatePushTarget(targetBranch);
  if (!targetValidation.allowed) {
    return { success: false, error: targetValidation.reason };
  }

  // Step 3: validate current branch name
  let currentBranch;
  try {
    currentBranch = execSync('git rev-parse --abbrev-ref HEAD', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return { success: false, error: 'Unable to determine current git branch' };
  }

  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    return {
      success: false,
      error: `Current branch "${currentBranch}" is not a valid agent branch: ${branchValidation.reason}`,
    };
  }

  // Step 4: check for unmerged branches (gating step) — only when pushing to dev
  if (targetBranch === DEV_BRANCH) {
    const unmergedCheck = checkUnmergedBranches();
    if (unmergedCheck.hasUnmergedBranches) {
      return {
        success: false,
        error: `Cannot push to '${DEV_BRANCH}' — there are unmerged branches that should be resolved first:\n\n${unmergedCheck.message}`,
      };
    }
  }

  // Step 5: execute the push
  const command = `git push ${remote} HEAD:refs/heads/${targetBranch}`;
  try {
    execSync(command, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return { success: true, command };
  } catch (err) {
    const stderr = err.stderr?.toString() || '';
    if (stderr.includes('non-fast-forward') || stderr.includes('[rejected]')) {
      return {
        success: false,
        error: `Push to '${targetBranch}' was rejected (non-fast-forward / conflict). Record the conflict details in a comment on the owning work item and resolve manually.`,
        command,
      };
    }
    return {
      success: false,
      error: `Push to '${targetBranch}' failed: ${stderr.trim()}`,
      command,
    };
  }
}

// ── Exports ──────────────────────────────────────────────────────────────────

export { makeBranchName, validateBranchName, isBranchBlocked };
export { checkUnmergedBranches, getUnmergedBranchNames, extractWorkItemId, getWorkItemStatus, getCurrentBranch } from './check-unmerged-branches.js';
export { checkAuditReadyToClose, getAuditStatus, getCandidateItems } from './check-audit-gate.js';
