/**
 * Hermetic environment for test fixtures that drive git/bash subprocesses.
 *
 * Same-module re-export of the canonical JS implementation in
 * `skill/shared/git_env.mjs` (SA-0MVYS34QF000AWQX) so tests and production
 * code share one contract. See that module for the rationale.
 */

export {
  hermeticGitEnv,
  gitEnvForRepo,
  REPOSITORY_OVERRIDE_VARS,
  INDEX_ENV_VAR,
} from '../../skill/shared/git_env.mjs';
