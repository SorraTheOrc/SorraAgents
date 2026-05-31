/**
 * ship.js — Legacy re-export wrapper.
 *
 * This file is retained for backward compatibility. All implementation has
 * moved to skill/ship/scripts/ship.js. New code should import directly from
 * the skill module.
 *
 * @deprecated Import from '../skill/ship/scripts/ship.js' instead.
 */

export {
  pushToDev,
  pushToBranch,
  validatePushTarget,
  validateForcePush,
  DEV_BRANCH,
  PROTECTED_BRANCHES,
  makeBranchName,
  validateBranchName,
  isBranchBlocked,
} from '../skill/ship/scripts/ship.js';
