/**
 * check-unmerged-branches.js — Gating step for the ship skill.
 *
 * Checks for local branches that are not yet merged into `dev`.
 * For each branch that follows the canonical `wl-<work-item-id>-<slug>` pattern,
 * it queries Worklog (wl) to retrieve the work item title, status, and stage.
 *
 * This is intended as a gating step: before running ship skill operations
 * (push-to-dev, release, etc.), agents should call `checkUnmergedBranches()`
 * to determine whether there are unmerged branches that should be dealt with
 * first.
 *
 * Usage:
 *
 *   import { checkUnmergedBranches } from './check-unmerged-branches.js';
 *
 *   const report = await checkUnmergedBranches();
 *   if (report.hasUnmergedBranches) {
 *     console.log(report.message);
 *     // Ask operator: should we merge these branches first?
 *   }
 */

import { execSync } from 'node:child_process';

// ── Constants ────────────────────────────────────────────────────────────────

/** The integration branch that agents push into. */
const DEV_BRANCH = 'dev';

/** Branches that are protected and should never be merged into dev. */
const PROTECTED_BRANCHES = Object.freeze(['main', 'master', 'HEAD']);

/**
 * Regex to extract work item ID from canonical agent branch names.
 *
 * Canonical pattern: wl-<WORK_ITEM_ID>-<slug>
 *   - WORK_ITEM_ID: uppercase letters, digits, and hyphen-separated groups
 *     (e.g. SA-0MPDZDPZB00121IE)
 *   - slug: lowercase letters, digits, and hyphen-separated groups
 *
 * Capture group 1 = work item ID
 * Capture group 2 = slug (description)
 */
const BRANCH_PATTERN = /^wl-([A-Z0-9]+(?:-[A-Z0-9]+)*)-([a-z0-9]+(?:-[a-z0-9]+)*)$/;

// ── getUnmergedBranchNames ──────────────────────────────────────────────────

/**
 * Run `git branch --no-merged <branch>` to list local branches that have not
 * been merged into the given target branch.
 *
 * @param {string} [targetBranch='dev'] - The branch to check merge status against.
 * @returns {string[]} Array of local branch names not merged into targetBranch.
 */
export function getUnmergedBranchNames(targetBranch = DEV_BRANCH) {
  try {
    const output = execSync(`git branch --no-merged ${targetBranch}`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    // Parse lines, trim whitespace and asterisk (current branch marker)
    return output
      .split('\n')
      .map((line) => line.trim().replace(/^\*\s*/, '').trim())
      .filter((line) => line.length > 0);
  } catch (err) {
    // If target branch doesn't exist yet, there are no unmerged branches
    const stderr = err.stderr?.toString() || '';
    if (
      stderr.includes('unknown revision') ||
      stderr.includes('not a valid object name') ||
      stderr.includes('not a git repository')
    ) {
      return [];
    }
    // Re-throw unexpected errors
    throw err;
  }
}

// ── extractWorkItemId ────────────────────────────────────────────────────────

/**
 * Extract the work item ID from a canonical agent branch name.
 *
 * The branch must match the pattern: wl-<work-item-id>-<slug>
 * where <work-item-id> is uppercase letters/digits with optional hyphens
 * (e.g. SA-0MPDZDPZB00121IE).
 *
 * @param {string} branchName - The branch name to parse.
 * @returns {string|null} The extracted work item ID, or null if the branch
 *   doesn't match the canonical pattern.
 */
export function extractWorkItemId(branchName) {
  if (!branchName || typeof branchName !== 'string') {
    return null;
  }
  const match = branchName.match(BRANCH_PATTERN);
  return match ? match[1] : null;
}

// ── getWorkItemStatus ────────────────────────────────────────────────────────

/**
 * Query Worklog (wl) for a work item's title, status, and stage.
 *
 * @param {string} workItemId - The work item ID to query.
 * @returns {{
 *   workItemId: string,
 *   title: string|null,
 *   status: string|null,
 *   stage: string|null,
 *   error?: string
 * }}
 */
export function getWorkItemStatus(workItemId) {
  try {
    const output = execSync(`wl show ${workItemId} --json`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const data = JSON.parse(output);

    if (!data.workItem) {
      return {
        workItemId,
        title: null,
        status: null,
        stage: null,
        error: 'Work item not found',
      };
    }

    return {
      workItemId,
      title: data.workItem.title || null,
      status: data.workItem.status || null,
      stage: data.workItem.stage || null,
    };
  } catch (err) {
    return {
      workItemId,
      title: null,
      status: null,
      stage: null,
      error: err.stderr?.toString()?.trim() || err.message,
    };
  }
}

// ── checkUnmergedBranches ────────────────────────────────────────────────────

/**
 * Get the current git branch name.
 *
 * @returns {string|null} The current branch name, or null if unable to determine.
 */
export function getCurrentBranch() {
  try {
    return execSync('git rev-parse --abbrev-ref HEAD', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
  } catch {
    return null;
  }
}

/**
 * Check for local branches that are not yet merged into `dev`, and gather
 * associated work item information from Worklog.
 *
 * The function returns a structured report object:
 * - `hasUnmergedBranches`: boolean indicating whether unmerged branches exist
 * - `unmergedBranches`: array of objects, one per unmerged branch, each with:
 *   - `branch`: the branch name
 *   - `workItemId`: extracted work item ID (or null if branch doesn't match pattern)
 *   - `title`: work item title (or null)
 *   - `status`: work item status (or null)
 *   - `stage`: work item stage (or null)
 *   - `error`: any error message from querying wl
 * - `message`: a human-readable report text
 *
 * The current branch is excluded from the check, since it is the branch
 * we are working on that we intend to push/merge.
 *
 * @returns {{
 *   hasUnmergedBranches: boolean,
 *   unmergedBranches: Array<{
 *     branch: string,
 *     workItemId: string|null,
 *     title: string|null,
 *     status: string|null,
 *     stage: string|null,
 *     error?: string
 *   }>,
 *   message: string
 * }}
 */
export function checkUnmergedBranches() {
  let branchNames;
  try {
    branchNames = getUnmergedBranchNames();
  } catch (err) {
    return {
      hasUnmergedBranches: false,
      unmergedBranches: [],
      message: `Error checking for unmerged branches: ${err.message}`,
    };
  }

  // Get the current branch so we can exclude it (it's the one we're about to push)
  const currentBranch = getCurrentBranch();

  // Filter out dev, protected branches (main, master, HEAD), and the current branch
  const filteredBranches = branchNames.filter(
    (b) =>
      b !== DEV_BRANCH &&
      !PROTECTED_BRANCHES.includes(b) &&
      b !== currentBranch,
  );

  if (filteredBranches.length === 0) {
    return {
      hasUnmergedBranches: false,
      unmergedBranches: [],
      message: 'All local branches are merged into dev. No gating issues found.',
    };
  }

  const unmergedBranches = filteredBranches.map((branch) => {
    const workItemId = extractWorkItemId(branch);

    const entry = {
      branch,
      workItemId,
    };

    if (workItemId) {
      const workItemInfo = getWorkItemStatus(workItemId);
      Object.assign(entry, workItemInfo);
    }

    return entry;
  });

  // Build a human-readable report message
  const lines = [
    `Found ${unmergedBranches.length} local branch(es) not yet merged into '${DEV_BRANCH}':`,
    '',
  ];

  unmergedBranches.forEach((entry, i) => {
    lines.push(`${i + 1}. Branch: ${entry.branch}`);
    if (entry.workItemId && entry.title) {
      lines.push(`   Work Item: ${entry.title} (${entry.workItemId})`);
      lines.push(`   Status: ${entry.status || 'unknown'}`);
      lines.push(`   Stage: ${entry.stage || 'unknown'}`);
    } else if (entry.workItemId) {
      lines.push(`   Work Item: ${entry.workItemId} (title not available)`);
      if (entry.error) {
        lines.push(`   Error retrieving details: ${entry.error}`);
      }
    } else {
      lines.push(`   Work Item: Not associated with a tracked work item`);
    }
    lines.push('');
  });

  lines.push(
    'Would you like to merge these branches into dev first before proceeding?',
  );

  return {
    hasUnmergedBranches: true,
    unmergedBranches,
    message: lines.join('\n'),
  };
}
