#!/usr/bin/env node

/**
 * regenerate-version.js — Regenerate derived version files for the ship
 * release process (SA-0MV7WAUHZ009UWY2).
 *
 * After `bump-version.js` increments `package.json` and the release branch is
 * merged with `dev`, consumer repositories that derive a CLI/runtime version
 * from `package.json` via a build step (e.g. ContextHub's auto-generated
 * `src/version.ts`, written by `scripts/generate-version.cjs` during
 * `npm run build`) are left with a stale derived version until the next build
 * runs. This helper runs the repository's build — but only when
 * `package.json` declares a `scripts.build` command — and commits the
 * regenerated **tracked** files so version-parity tests pass immediately after
 * a release.
 *
 * Behaviour:
 *   - No `scripts.build` → no-op (repos without a build are unaffected).
 *   - Build success → `git add -u` (tracked modifications only, so build
 *     droppings such as `dist/` are never staged) and a commit
 *     `Regenerate derived version files for v<version>` when anything changed.
 *   - Submodule pointers and their pins excluded → after staging, every
 *     staged gitlink (index mode `160000`) is restored to its recorded entry,
 *     and any staged tracked file the build rewrote to record the advanced
 *     submodule SHA (a "pin", e.g. `configs/engine-pin.json`) is restored to
 *     its recorded content. A build that advances a submodule checkout can
 *     therefore never smuggle a pointer bump — or a pin that disagrees with the
 *     recorded pointer — into the derived-files commit (generic — derived from
 *     the recorded/advanced SHA pair only, no per-repo configuration).
 *   - Build failure → a `Warning: … (continuing with release)` line is logged
 *     and the process exits 0: regeneration is **non-fatal**, mirroring the
 *     post-bump CHANGELOG step.
 *
 * Usage:
 *   node regenerate-version.js [<version>]
 *
 * Arguments:
 *   <version>   Version being released (e.g. "0.2.0"). Used only in the commit
 *               subject; optional.
 *
 * Options:
 *   -h, --help  Show this help message
 *
 * Exit codes:
 *   0  Success, no-op, or a non-fatal build failure
 *   1  Invalid usage
 */

import { readFileSync, existsSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { execSync, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { gitEnvForRepo } from '../../../shared/git_env.mjs';

// ── Pure helpers ────────────────────────────────────────────────────────────

/**
 * Read the `scripts.build` command from a package.json.
 *
 * @param {string} pkgPath - Path to package.json.
 * @returns {string|null} The build command, or null when absent/unreadable.
 */
export function readBuildScript(pkgPath) {
  if (!existsSync(pkgPath)) return null;
  try {
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8'));
    const build = pkg?.scripts?.build;
    return typeof build === 'string' && build.trim() ? build : null;
  } catch {
    return null;
  }
}

/** Run the repository's `npm run build` in *repoRoot* (default build runner). */
function runNpmBuild(repoRoot) {
  execSync('npm run build', {
    cwd: repoRoot,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
}

/**
 * Default minimum SHA-hex prefix length used when matching a pin reference.
 *
 * A pin file may record a submodule commit as a full 40-char SHA or as an
 * abbreviation (commonly 7 chars). Comparing on at least this many hex
 * characters keeps the match meaningful without hard-coding a pin format.
 */
export const SHA_MIN_PREFIX = 7;

/**
 * Does *content* reference *sha*?
 *
 * Scans the content's hex runs and returns true when one is a prefix of *sha*
 * at least *minPrefix* characters long (so a full SHA and a >=7-char
 * abbreviation both match, case-insensitively). A pin records one submodule
 * commit this way; requiring the minimum prefix avoids a coincidental
 * two-character match while tolerating abbreviations.
 *
 * @param {string} content - Text to search.
 * @param {string} sha     - Full commit SHA the content should reference.
 * @param {number} [minPrefix] - Minimum hex-prefix length (default 7).
 * @returns {boolean}
 */
export function contentReferencesSha(content, sha, minPrefix = SHA_MIN_PREFIX) {
  if (typeof content !== 'string' || typeof sha !== 'string') return false;
  const needle = sha.trim().toLowerCase();
  if (needle.length < minPrefix) return false;
  const hexRun = /[0-9a-f]+/gi;
  let match;
  while ((match = hexRun.exec(content)) !== null) {
    const run = match[0].toLowerCase();
    if (run.length < minPrefix) continue;
    if (needle.startsWith(run) || run.startsWith(needle)) return true;
  }
  return false;
}

/** Run a git command in *repoRoot* and return its stdout (UTF-8). */
function gitOutput(repoRoot, args) {
  return execFileSync('git', args, {
    cwd: repoRoot,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    env: gitEnvForRepo(repoRoot),
  });
}

/**
 * Read a git blob (`HEAD:<path>` or `:<path>`) as UTF-8, or null when absent.
 *
 * Used to compare a tracked file's recorded (HEAD) content with its staged
 * content without touching the working tree. A path absent from the given
 * revision (new or deleted file) yields null and is never restored.
 *
 * @param {string} repoRoot
 * @param {string} revPath - e.g. `HEAD:configs/engine-pin.json` or `:path`.
 * @returns {string|null}
 */
function readBlob(repoRoot, revPath) {
  try {
    return gitOutput(repoRoot, ['show', revPath]);
  } catch {
    return null;
  }
}

/**
 * Staged submodule pointers (gitlinks) and their recorded/advanced SHAs.
 *
 * `git add -u` stages a submodule's gitlink at its *advanced* commit (the
 * checkout's new HEAD). The *recorded* commit is the one still in `HEAD`'s
 * tree. Both halves let the caller decide which tracked files the build
 * rewrote from the recorded commit to the advanced one.
 *
 * @param {string} repoRoot
 * @returns {{path:string, recordedSha:string|null, advancedSha:string}[]}
 */
function stagedGitlinks(repoRoot) {
  // `--stage -z`: NUL-separated ``<mode> <object> <stage>\t<path>`` records.
  return gitOutput(repoRoot, ['ls-files', '--stage', '-z'])
    .split('\0')
    .filter(Boolean)
    .map((record) => {
      const tab = record.indexOf('\t');
      if (tab === -1) return null;
      const [mode, advancedSha] = record.slice(0, tab).split(' ');
      if (mode !== '160000') return null;
      const path = record.slice(tab + 1);
      const recordedSha = recordedGitlinkSha(repoRoot, path);
      return { path, recordedSha, advancedSha };
    })
    .filter(Boolean);
}

/**
 * The commit SHA recorded for a gitlink *path* in `HEAD`'s tree, or null.
 *
 * `git rev-parse HEAD:<path>` reads the tree entry directly — correct even
 * though a submodule's commit object lives in the submodule's own object
 * database, not the superproject's.
 */
function recordedGitlinkSha(repoRoot, path) {
  try {
    return gitOutput(repoRoot, ['rev-parse', `HEAD:${path}`]).trim();
  } catch {
    return null;
  }
}

/**
 * Staged tracked files the build rewrote from a recorded submodule SHA to the
 * advanced one — the "pins" that must travel with their gitlink.
 *
 * A file qualifies only when **both** halves hold: its `HEAD` content carries
 * the recorded gitlink SHA and its staged content carries the advanced SHA.
 * Requiring both makes the rule generic (no pin path list) and avoids
 * reverting an unrelated tracked file that merely mentions the advanced SHA.
 * A file the release deliberately changed before the build does not carry the
 * recorded SHA in `HEAD`, so it is left alone (SA-0MW6LOXBO0008706 AC3).
 *
 * @param {string} repoRoot
 * @param {{path:string, recordedSha:string, advancedSha:string}[]} changedGitlinks
 * @param {number} minPrefix
 * @returns {string[]} Pin paths to restore, in staged order.
 */
function findRewrittenPins(repoRoot, changedGitlinks, minPrefix) {
  if (changedGitlinks.length === 0) return [];
  const gitlinkPaths = new Set(changedGitlinks.map((g) => g.path));
  const stagedPaths = gitOutput(repoRoot, ['diff', '--cached', '--name-only', '-z'])
    .split('\0')
    .filter(Boolean)
    .filter((path) => !gitlinkPaths.has(path));

  const pins = [];
  for (const path of stagedPaths) {
    const headContent = readBlob(repoRoot, `HEAD:${path}`);
    const stagedContent = readBlob(repoRoot, `:${path}`);
    if (headContent === null || stagedContent === null) continue;
    const rewritten = changedGitlinks.some(
      (g) =>
        contentReferencesSha(headContent, g.recordedSha, minPrefix) &&
        contentReferencesSha(stagedContent, g.advancedSha, minPrefix),
    );
    if (rewritten) pins.push(path);
  }
  return pins;
}

/**
 * Unstage every staged submodule pointer (gitlink) **and restore the pins the
 * build rewrote to follow it**.
 *
 * `git add -u` stages a submodule's gitlink whenever the submodule checkout
 * HEAD differs from the recorded index entry. Consumer-repo builds may advance
 * a submodule checkout (e.g. main-street's `engine:sync`), so a derived-files
 * commit would otherwise silently bump the submodule pointer — breaking pin
 * contracts that expect the pointer to match a committed pin.
 *
 * Restoring the pointer alone is not enough when the same build also rewrote a
 * tracked pin file that records the submodule SHA: the commit would then carry
 * a pin pointing at the advanced commit while the gitlink stayed recorded. For
 * every gitlink whose staging was reversed, restore any staged tracked file
 * whose recorded content carries the recorded SHA and whose staged content
 * carries the advanced SHA — both the index entry and the working-tree file —
 * so the pair stays coherent. This is generic (any submodule, any pin, no
 * per-repo configuration) and scoped strictly to gitlink-related paths: an
 * unrelated staged change is untouched.
 *
 * @param {string} repoRoot - Repository root.
 * @param {{minPrefix?:number}} [opts]
 * @returns {{gitlinks:string[], pins:string[]}} Reversed gitlink paths and
 *   restored pin paths.
 */
function unstageSubmodulePointers(repoRoot, { minPrefix = SHA_MIN_PREFIX } = {}) {
  const gitlinks = stagedGitlinks(repoRoot);
  if (gitlinks.length === 0) return { gitlinks: [], pins: [] };

  const changed = gitlinks.filter(
    (g) => g.recordedSha && g.advancedSha && g.recordedSha !== g.advancedSha,
  );

  // Read the pins before restoring anything so the advanced SHA is still in
  // the index for the staged-content comparison.
  const pins = findRewrittenPins(repoRoot, changed, minPrefix);

  // Restore every staged gitlink to its recorded entry (index only — the
  // submodule working tree is deliberately not reset). Unchanged gitlinks are
  // a harmless no-op.
  gitOutput(repoRoot, ['restore', '--source=HEAD', '--staged', '--', ...gitlinks.map((g) => g.path)]);

  // Restore the pins (index + working tree) so neither the gitlink nor its pin
  // reaches the derived-files commit.
  if (pins.length > 0) {
    gitOutput(repoRoot, [
      'restore',
      '--source=HEAD',
      '--staged',
      '--worktree',
      '--',
      ...pins,
    ]);
  }

  return { gitlinks: changed.map((g) => g.path), pins };
}

// ── Core step ───────────────────────────────────────────────────────────────

/**
 * Regenerate and commit tracked derived version files for a release.
 *
 * @param {object} opts
 * @param {string} opts.repoRoot   Repository root (working directory for the
 *                                 build and all git operations).
 * @param {string|null} [opts.version]  Released version, for the commit subject.
 * @param {() => void} [opts.runBuild]  Injectable build boundary. Defaults to
 *                                 `npm run build`. May throw on failure.
 * @param {Console} [opts.logger]  Logger (defaults to the global console).
 * @param {number} [opts.shaMinPrefix]  Minimum SHA-hex prefix length used when
 *                                 matching a pin reference (default 7).
 * @returns {{ran:boolean, success:boolean, committed:boolean, reason:string, files?:string[], restoredGitlinks?:string[], restoredPins?:string[]}}
 *   A structured result; never throws for a build failure (non-fatal).
 *   On a commit, `restoredGitlinks`/`restoredPins` name the submodule pointers
 *   and pin files the build tried to change that were restored to `HEAD`.
 */
export function regenerateDerivedFiles({
  repoRoot,
  version = null,
  runBuild = null,
  logger = console,
  shaMinPrefix = SHA_MIN_PREFIX,
} = {}) {
  const pkgPath = resolve(repoRoot, 'package.json');
  const buildScript = readBuildScript(pkgPath);

  if (!buildScript) {
    return { ran: false, success: true, committed: false, reason: 'no build script' };
  }

  // Build — non-fatal: a failure logs a warning and returns a failed result.
  try {
    (runBuild || (() => runNpmBuild(repoRoot)))();
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    logger.warn(
      `Warning: derived version file regeneration failed (continuing with release): ${message}`,
    );
    return { ran: true, success: false, committed: false, reason: message };
  }

  // Stage tracked modifications only (`git add -u`): untracked build
  // droppings such as `dist/` are deliberately not staged.
  execFileSync('git', ['add', '-u'], {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: gitEnvForRepo(repoRoot),
  });

  // The build may also have advanced a submodule checkout (e.g. main-street's
  // `engine:sync`), which `git add -u` stages as a gitlink change — and the
  // same build may have rewritten a tracked pin file that records the
  // submodule SHA. Restore the staged pointers and their build-rewritten pins
  // to their recorded entries: the derived-files commit contains regenerated
  // tracked files only and can never bump a pointer or ship a pin that
  // disagrees with it. Generic — derived from the recorded/advanced SHA pair,
  // no per-repo configuration (SA-0MW6LOXBO0008706).
  const { gitlinks: restoredGitlinks, pins: restoredPins } = unstageSubmodulePointers(
    repoRoot,
    { minPrefix: shaMinPrefix },
  );

  const staged = execFileSync('git', ['diff', '--cached', '--name-only'], {
    cwd: repoRoot,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    env: gitEnvForRepo(repoRoot),
  }).trim();

  if (!staged) {
    return { ran: true, success: true, committed: false, reason: 'no changes' };
  }

  const files = staged.split('\n').filter(Boolean);
  const subject = version
    ? `Regenerate derived version files for v${version}`
    : 'Regenerate derived version files';
  execFileSync('git', ['commit', '-m', subject], {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: gitEnvForRepo(repoRoot),
  });

  return {
    ran: true,
    success: true,
    committed: true,
    reason: 'committed',
    files,
    restoredGitlinks,
    restoredPins,
  };
}

// ── CLI Entry Point ─────────────────────────────────────────────────────────

function printUsage() {
  console.error(`
Usage: node regenerate-version.js [<version>]

Arguments:
  <version>   Version being released (e.g. "0.2.0"); optional.

Options:
  -h, --help  Show this help message

Regenerates tracked derived version files by running the repository's
\`npm run build\` (only when package.json declares a \`build\` script) and
commits the result. A failing build logs a warning and never aborts a release.
`);
}

function main() {
  const args = process.argv.slice(2);

  if (args[0] === '-h' || args[0] === '--help') {
    printUsage();
    process.exit(0);
  }

  const unknown = args.filter((a) => a.startsWith('-'));
  if (unknown.length > 0) {
    console.error(`Unknown option: ${unknown[0]}`);
    printUsage();
    process.exit(1);
  }

  const version = args[0] || null;
  const repoRoot = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();

  const result = regenerateDerivedFiles({ repoRoot, version });

  if (!result.ran) {
    console.error('No build script found in package.json; skipping derived version file regeneration.');
  } else if (result.success && result.committed) {
    const parts = [
      `Regenerated derived version files for v${version ?? '(unversioned)'} (${result.files.length} file(s))`,
    ];
    if (result.restoredGitlinks?.length) {
      parts.push(`restored submodule pointer(s): ${result.restoredGitlinks.join(', ')}`);
    }
    if (result.restoredPins?.length) {
      parts.push(`restored pin file(s): ${result.restoredPins.join(', ')}`);
    }
    console.error(`${parts.join('; ')}.`);
  } else if (result.success) {
    console.error('Derived version files already up to date.');
  }
  // Build failure already logged a warning; the step is non-fatal.

  process.exit(0);
}

// Allow both ESM import and direct CLI execution.
// Use realpathSync on both sides to handle symlinked install paths.
const isMainModule = process.argv[1] &&
  (realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1])));

if (isMainModule) {
  main();
}
