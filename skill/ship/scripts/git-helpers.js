/**
 * Git helper utilities for agent branch naming and push-policy enforcement.
 *
 * Provides:
 * - `makeBranchName(workItemId, shortDesc)` — generates a canonical branch name
 *   following the pattern `wl-<work-item-id>-<short-desc>`.
 * - `validateBranchName(name)` — validates a branch name against the canonical
 *   pattern, returning `{ valid: true }` or `{ valid: false, reason: string }`.
 * - `isBranchBlocked(branch)` — returns true if the branch is protected and
 *   agents must not push to it directly.
 *
 * Branch naming convention
 * ────────────────────────
 * All agent-created branches MUST follow the pattern:
 *
 *     wl-<work-item-id>-<short-description>
 *
 * where:
 * - `wl-` is a literal prefix
 * - `<work-item-id>` is the Worklog identifier (e.g. SA-0MPDZDPZB00121IE)
 * - `<short-description>` is a lowercase, hyphen-separated slug
 *
 * Examples:
 * - wl-SA-0MPDZDPZB00121IE-branch-naming-policy
 * - wl-SA-001-fix-login-bug
 *
 * Push policy
 * ───────────
 * Agents MUST NOT push directly to protected branches (main, master, HEAD).
 * Use `isBranchBlocked(branch)` to check before any push operation.
 */

// ── Constants ────────────────────────────────────────────────────────────────

/** Maximum safe length for a git branch name. */
const MAX_BRANCH_LENGTH = 250;

/** Protected branches that agents must never push to. */
export const BLOCKED_BRANCHS = Object.freeze(['main', 'master', 'HEAD']);

/** Regex pattern for valid agent branch names: wl-<id>-<slug>
 *  Work-item IDs may contain hyphens (e.g. SA-0MPDZDPZB00121IE), so the id
 *  segment allows hyphens between alphanumeric groups.
 */
export const BRANCH_NAME_PATTERN = /^wl-[A-Z0-9]+(-[A-Z0-9]+)*-[a-z0-9]+(-[a-z0-9]+)*$/;

// ── makeBranchName ───────────────────────────────────────────────────────────

/**
 * Generate a canonical branch name for a work item.
 *
 * @param {string} workItemId - The Worklog work-item identifier (e.g. SA-0MPDZDPZB00121IE).
 * @param {string} shortDesc  - A short human-readable description of the work.
 * @returns {string} A branch name following the `wl-<id>-<slug>` pattern.
 * @throws {Error} If workItemId or shortDesc is missing/empty.
 */
export function makeBranchName(workItemId, shortDesc) {
  if (!workItemId || typeof workItemId !== 'string' || workItemId.trim() === '') {
    throw new Error('workItemId is required and must be a non-empty string');
  }
  if (!shortDesc || typeof shortDesc !== 'string' || shortDesc.trim() === '') {
    throw new Error('shortDesc is required and must be a non-empty string');
  }

  // Build slug: trim, lowercase, replace non-alphanumeric chars with hyphens,
  // collapse multiple hyphens, strip leading/trailing hyphens.
  let slug = shortDesc
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');

  let branchName = `wl-${workItemId}-${slug}`;

  // Truncate if too long, but keep at least enough to identify the work item.
  if (branchName.length > MAX_BRANCH_LENGTH) {
    const prefix = `wl-${workItemId}-`;
    const maxSlugLen = MAX_BRANCH_LENGTH - prefix.length;
    branchName = prefix + slug.slice(0, maxSlugLen).replace(/-+$/, '');
  }

  return branchName;
}

// ── validateBranchName ───────────────────────────────────────────────────────

/**
 * Validate a branch name against the canonical agent branch naming pattern.
 *
 * @param {string} name - The branch name to validate.
 * @returns {{ valid: true } | { valid: false, reason: string }}
 */
export function validateBranchName(name) {
  if (!name || typeof name !== 'string' || name.trim() === '') {
    return { valid: false, reason: 'Branch name is empty or not a string' };
  }

  if (!BRANCH_NAME_PATTERN.test(name)) {
    return {
      valid: false,
      reason: `Branch name "${name}" does not match the required pattern wl-<work-item-id>-<short-desc> (lowercase, hyphens only)`,
    };
  }

  return { valid: true };
}

// ── isBranchBlocked ──────────────────────────────────────────────────────────

/**
 * Check whether a branch is protected and agents must not push to it.
 *
 * @param {string} branch - The branch name to check.
 * @returns {boolean} True if the branch is blocked for agent pushes.
 */
export function isBranchBlocked(branch) {
  return BLOCKED_BRANCHS.includes(branch);
}
