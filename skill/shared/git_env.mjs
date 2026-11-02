/**
 * Git subprocess environments — hermetic fixtures and ownership-aware repo git.
 *
 * JS counterpart of `skill/shared/git_sandbox.py`
 * (`fixture_git_env()` / `git_env_for_repo()`).
 *
 * Why (SA-0MVQHVGFR006W8LG, SA-0MVQHV9Y3001RNNW, SA-0MVYS34QF000AWQX):
 * `implement.py finish` exports a temporary `GIT_INDEX_FILE` holding the
 * *would-be commit* of the tree under test. The test-skill scrub deliberately
 * preserves that variable (F3) so finisher tests can observe the staged tree.
 * It is therefore inherited by every `git` subprocess a test — or a script the
 * test drives — spawns. git applies an inherited index to whatever repository
 * `cwd` names, so:
 *
 *   - a **fixture** repository's `git init`/`add`/`commit` write into the
 *     gate's index, failing the test and invalidating the gate's evidence; and
 *   - a **script operating on a fixture repo** (e.g. the release helpers)
 *     reports another repository's staged files as its own changes.
 *
 * `hermeticGitEnv()` is for fixtures: it drops the index and every
 * repository-override variable. `gitEnvForRepo()` is for production scripts:
 * it keeps the ambient index only when that index belongs to the repository
 * being operated on, and drops it otherwise.
 */

import { execFileSync } from 'node:child_process';
import { realpathSync } from 'node:fs';
import { isAbsolute, resolve, sep } from 'node:path';

/** Variables that retarget git at another repository. */
export const REPOSITORY_OVERRIDE_VARS = [
  'GIT_DIR',
  'GIT_WORK_TREE',
  'GIT_CONFIG',
  'GIT_CONFIG_GLOBAL',
  'GIT_CONFIG_SYSTEM',
  'GIT_CONFIG_NOSYSTEM',
  'GIT_OBJECT_DIRECTORY',
  'GIT_ALTERNATE_OBJECT_DIRECTORIES',
];

/** The variable the finish gate exports (see module docs). */
export const INDEX_ENV_VAR = 'GIT_INDEX_FILE';

/**
 * Environment for **fixture** git commands: hermetic, never inherits an index.
 *
 * @param {Record<string, string|undefined>} [base] - Base environment
 *   (default `process.env`); never mutated.
 * @returns {Record<string, string|undefined>} A scrubbed copy.
 */
export function hermeticGitEnv(base = process.env) {
  const env = { ...base };
  for (const name of REPOSITORY_OVERRIDE_VARS) delete env[name];
  delete env[INDEX_ENV_VAR];
  return env;
}

/** Directory of the repository *repo* names, or null when it is not one. */
function absoluteGitDir(repo) {
  try {
    const out = execFileSync('git', ['rev-parse', '--absolute-git-dir'], {
      cwd: repo,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
      env: hermeticGitEnv(),
    }).trim();
    return out ? realpathSync(out) : null;
  } catch {
    return null;
  }
}

/**
 * Environment for a git command that **inspects or mutates** *repo*.
 *
 * Repository overrides are always scrubbed. An ambient `GIT_INDEX_FILE` is
 * kept **only when it lives inside *repo*'s git directory** — the case where
 * the gate is validating exactly that repository — and dropped otherwise, so a
 * script driving a fixture (or any other repository) falls back to git's
 * default index for that repository instead of writing into a foreign one.
 *
 * @param {string} repo - Repository the git command operates on.
 * @param {Record<string, string|undefined>} [base] - Base environment
 *   (default `process.env`); never mutated.
 * @returns {Record<string, string|undefined>} A child environment mapping.
 */
export function gitEnvForRepo(repo, base = process.env) {
  const env = hermeticGitEnv(base);
  const indexFile = base[INDEX_ENV_VAR];
  if (!indexFile) return env;
  const gitDir = absoluteGitDir(repo);
  if (!gitDir || !isAbsolute(indexFile)) return env;
  let resolvedIndex;
  try {
    resolvedIndex = realpathSync(indexFile);
  } catch {
    return env; // an index that does not exist yet cannot be attributed
  }
  const prefix = gitDir.endsWith(sep) ? gitDir : gitDir + sep;
  if (resolvedIndex.startsWith(prefix) || resolve(resolvedIndex) === gitDir) {
    env[INDEX_ENV_VAR] = indexFile;
  }
  return env;
}
