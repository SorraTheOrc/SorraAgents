#!/usr/bin/env node

/**
 * assert-build.mjs — Fatal build/type-check gate for the dev→main release
 * (SA-0MWKQ5TXL005CLX5).
 *
 * The release's full-suite gate asserts tests, but every test runner in the
 * framework (vitest, `node --test`, pytest) transpiles or interprets without
 * type-checking, so a TypeScript tree that does not compile can still yield a
 * green suite. This helper runs the repository's build (`package.json`
 * `scripts.build`) on the merged release tree and exits **non-zero** when it
 * fails, so a tree that cannot be built can never be tagged or promoted.
 *
 * It is deliberately independent of `regenerate-version.js`, which runs the
 * same build **non-fatally** to commit regenerated derived version files.
 * Regeneration keeps its own non-fatal semantics for *its* purpose; a build
 * *failure* is always fatal here. (The two share the same build contract:
 * `scripts.build`, run with `npm run build`.)
 *
 * The `.mjs` extension is deliberate: the helper may be installed/copied
 * outside a `"type": "module"` package (hermetic test fixtures copy it
 * standalone), and `.mjs` is ESM regardless of the nearest package.json. It
 * imports only Node built-ins — no `skill/shared/` imports that would break
 * such a copy.
 *
 * Behaviour:
 *   - No `package.json`, no `scripts.build`, or an unreadable/invalid
 *     `scripts.build` → no-op (exit 0). Repos without a build are unaffected.
 *   - Build success → exit 0 with a green line.
 *   - Build failure → exit 1 with an actionable error.
 *
 * Usage:
 *   node assert-build.mjs
 *
 * Exit codes:
 *   0  Build passed, or there is no build script (no-op)
 *   1  Build failed
 */

import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

/**
 * Read the `scripts.build` command from a repository's package.json.
 *
 * @param {string} repoRoot - Repository root.
 * @returns {string|null} The build command, or null when absent/unreadable.
 */
export function readBuildScript(repoRoot) {
  const pkgPath = resolve(repoRoot, 'package.json');
  if (!existsSync(pkgPath)) return null;
  try {
    const pkg = JSON.parse(readFileSync(pkgPath, 'utf-8'));
    const build = pkg?.scripts?.build;
    return typeof build === 'string' && build.trim() ? build : null;
  } catch {
    return null;
  }
}

/**
 * Assert the repository's build on the current tree.
 *
 * Never throws for a build failure: the outcome is returned as a structured
 * result so the CLI (and tests) decide the exit code. The build's own output
 * is streamed to the parent's stdout/stderr so a red release log names the
 * compiler error directly.
 *
 * @param {object} [opts]
 * @param {string} [opts.repoRoot] - Repository root (default: process.cwd()).
 * @param {() => void} [opts.runBuild] - Injectable build boundary. Defaults to
 *   `npm run build` in *repoRoot*. May throw on failure.
 * @param {Console} [opts.logger] - Logger (defaults to the global console).
 * @returns {{ran:boolean, success:boolean, reason:string}}
 *   `ran` is false when there is no build script (a no-op).
 */
export function assertBuild({ repoRoot = process.cwd(), runBuild = null, logger = console } = {}) {
  const buildScript = readBuildScript(repoRoot);
  if (!buildScript) {
    logger.error('No build script found in package.json; the build gate is a no-op.');
    return { ran: false, success: true, reason: 'no build script' };
  }

  try {
    if (runBuild) {
      runBuild();
    } else {
      execSync('npm run build', { cwd: repoRoot, stdio: 'inherit' });
    }
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    return { ran: true, success: false, reason: message };
  }
  return { ran: true, success: true, reason: 'build passed' };
}

// ── CLI Entry Point ─────────────────────────────────────────────────────────

function main() {
  const repoRoot = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
  const result = assertBuild({ repoRoot });

  if (!result.ran) {
    console.error('Build gate: no `scripts.build` in package.json — nothing to assert (no-op).');
    process.exit(0);
  }

  if (!result.success) {
    console.error(`ERROR: Build failed on the merged tree (npm run build): ${result.reason}`);
    console.error(
      '  A tree that does not build must never be released. Fix the build (types/compile) ' +
        'on dev and re-run the release.',
    );
    process.exit(1);
  }

  console.error('Build gate: `npm run build` succeeded on the merged tree.');
  process.exit(0);
}

// Allow both ESM import and direct CLI execution. Use realpathSync on both
// sides so a symlinked install path still runs the CLI (mirrors the other
// release helpers).
const isMainModule =
  process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1]));

if (isMainModule) {
  main();
}
