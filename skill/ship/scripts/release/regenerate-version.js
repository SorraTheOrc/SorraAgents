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
 *   - Submodule pointers excluded → after staging, every staged gitlink
 *     (index mode `160000`) is restored to its recorded entry, so a build
 *     that advances a submodule checkout can never smuggle a pointer bump
 *     into the derived-files commit (generic — no per-repo configuration).
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
 * Unstage every staged submodule pointer (gitlink) in *repoRoot*.
 *
 * `git add -u` stages a submodule's gitlink whenever the submodule checkout
 * HEAD differs from the recorded index entry. Consumer-repo builds may advance
 * a submodule checkout (e.g. main-street's `engine:sync`), so a derived-files
 * commit would otherwise silently bump the submodule pointer — breaking pin
 * contracts that expect the pointer to match a committed pin.
 *
 * Restore all staged gitlinks (index mode `160000`) to their recorded HEAD
 * entries. This is generic (any submodule, any repo) and scoped strictly to
 * gitlink entries: unrelated staged changes are untouched. A gitlink that was
 * not staged is a harmless no-op.
 *
 * @param {string} repoRoot - Repository root.
 * @returns {string[]} Submodule paths whose gitlink staging was reversed.
 */
function unstageSubmodulePointers(repoRoot) {
  // `--stage -z`: NUL-separated ``<mode> <object> <stage>\t<path>`` records.
  const index = execFileSync('git', ['ls-files', '--stage', '-z'], {
    cwd: repoRoot,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const gitlinks = index
    .split('\0')
    .filter(Boolean)
    .map((record) => {
      const tab = record.indexOf('\t');
      if (tab === -1) return null;
      const mode = record.slice(0, tab).split(' ')[0];
      return mode === '160000' ? record.slice(tab + 1) : null;
    })
    .filter(Boolean);

  if (gitlinks.length === 0) return [];

  execFileSync('git', ['restore', '--staged', '--', ...gitlinks], {
    cwd: repoRoot,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  return gitlinks;
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
 * @returns {{ran:boolean, success:boolean, committed:boolean, reason:string, files?:string[]}}
 *   A structured result; never throws for a build failure (non-fatal).
 */
export function regenerateDerivedFiles({
  repoRoot,
  version = null,
  runBuild = null,
  logger = console,
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
  execFileSync('git', ['add', '-u'], { cwd: repoRoot, stdio: ['pipe', 'pipe', 'pipe'] });

  // The build may also have advanced a submodule checkout (e.g. main-street's
  // `engine:sync`), which `git add -u` stages as a gitlink change. Restore
  // staged submodule pointers to their recorded entries: the derived-files
  // commit contains regenerated tracked files only and can never bump a
  // submodule pointer. Generic — applies to every submodule, no per-repo
  // configuration.
  unstageSubmodulePointers(repoRoot);

  const staged = execFileSync('git', ['diff', '--cached', '--name-only'], {
    cwd: repoRoot,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
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
  });

  return { ran: true, success: true, committed: true, reason: 'committed', files };
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
    console.error(`Regenerated derived version files for v${version ?? '(unversioned)'} (${result.files.length} file(s)).`);
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
