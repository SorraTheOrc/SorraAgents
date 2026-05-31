/**
 * git-helpers.js — Legacy re-export wrapper.
 *
 * This file is retained for backward compatibility. All implementation has
 * moved to skill/ship/scripts/git-helpers.js. New code should import directly
 * from the skill module.
 *
 * @deprecated Import from '../skill/ship/scripts/git-helpers.js' instead.
 */

export {
  makeBranchName,
  validateBranchName,
  isBranchBlocked,
  BLOCKED_BRANCHS,
  BRANCH_NAME_PATTERN,
} from '../skill/ship/scripts/git-helpers.js';
