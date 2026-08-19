#!/usr/bin/env node
// run-release.js — safe wrapper to invoke repository-level release script
// Usage: node run-release.js [--dry-run] [--work-item-id <id>] [--force] [--skip-checks] [--bump patch|minor|major]
//
// The --bump flag is passed through to the canonical release script
// (merge-dev-to-main.sh) and controls which part of the semver is
// incremented before the merge. Default is 'patch'.

import { existsSync, realpathSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { checkUnmergedBranches } from './check-unmerged-branches.js';
import { checkAuditReadyToClose, getCandidateItems, getTopLevelCandidateItems, checkProducerReviewStatus } from './check-audit-gate.js';
import { checkCriticalItems } from './check-critical-items.js';
import { checkWorklogRefs } from './check-worklog-refs.js';

// Canonical release script path relative to repository root
const REPO_RELEASE_SCRIPT = 'scripts/release/merge-dev-to-main.sh';

// Also accept a skill-level release script (e.g., installed under the skill directory)
// Skill layout: <skill-dir>/scripts/release/merge-dev-to-main.sh
const skillDir = dirname(dirname(fileURLToPath(import.meta.url)));
const SKILL_RELEASE_SCRIPT = join(skillDir, 'scripts', 'release', 'merge-dev-to-main.sh');

// Timeout (ms) for the release-script subprocess. A hung git/gh operation
// must fail the release loudly after a bounded time instead of blocking
// indefinitely — the Code Freeze marker stays set for the whole run, so an
// unbounded spawn means an unbounded project-wide freeze (SA-0MSDX3KTV0092B7N).
// Overridable via SHIP_RELEASE_TIMEOUT_MS (operators/tests).
const RELEASE_SCRIPT_TIMEOUT_MS = Number(process.env.SHIP_RELEASE_TIMEOUT_MS) || 600000;

// ── Code Freeze marker (contract: WL-0MSBU4KMA004PKSR) ──────────────────────

/**
 * Resolve the project root directory (where .worklog/ lives).
 *
 * Resolution order:
 *   1. The git top-level (``git rev-parse --show-toplevel``) when inside a
 *      git worktree/checkout.
 *   2. ``process.cwd()`` as a fallback.
 *
 * @returns {string} Absolute project root path.
 */
export function resolveProjectRoot() {
  try {
    const out = execSync('git rev-parse --show-toplevel', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const root = (out || '').trim();
    if (root) return root;
  } catch {
    // not a git checkout — fall through to cwd
  }
  return process.cwd();
}

/**
 * Absolute path to the Code Freeze marker file for a project root.
 *
 * @param {string} [projectRoot] - Project root (default: resolveProjectRoot()).
 * @returns {string} Absolute path to <root>/.worklog/code-freeze.json.
 */
export function codeFreezeMarkerPath(projectRoot = resolveProjectRoot()) {
  return join(projectRoot, '.worklog', 'code-freeze.json');
}

/**
 * Write the Code Freeze marker file.
 *
 * Contract (WL-0MSBU4KMA004PKSR):
 *   `{ "active": true, "reason": "ship release in progress",
 *      "startedAt": "<ISO>", "pid": <pid> }`
 *
 * @param {string} [projectRoot] - Project root (default: resolveProjectRoot()).
 * @returns {string} Absolute path to the marker file written.
 */
export function setCodeFreezeMarker(projectRoot = resolveProjectRoot()) {
  const markerPath = codeFreezeMarkerPath(projectRoot);
  mkdirSync(dirname(markerPath), { recursive: true });
  const marker = {
    active: true,
    reason: 'ship release in progress',
    startedAt: new Date().toISOString(),
    pid: process.pid,
  };
  writeFileSync(markerPath, JSON.stringify(marker, null, 2));
  return markerPath;
}

/**
 * Remove the Code Freeze marker file (idempotent; missing file is a no-op).
 *
 * @param {string} [projectRoot] - Project root (default: resolveProjectRoot()).
 * @returns {void}
 */
export function clearCodeFreezeMarker(projectRoot = resolveProjectRoot()) {
  const markerPath = codeFreezeMarkerPath(projectRoot);
  try {
    rmSync(markerPath, { force: true });
  } catch {
    // ignore — removal is best-effort
  }
}

// ── releaseScriptForwardArgs ─────────────────────────────────────────────────

// Flags consumed by run-release.js itself (e.g. gate bypass) and therefore
// NEVER forwarded to the canonical merge script, which rejects unknown flags
// with exit 2 ("Unknown arg: ..."). See SA-0MSKYGAWJ0009M3P.
const WRAPPER_ONLY_FLAGS = new Set(['--skip-checks']);

/**
 * Compute the argument list to forward to the canonical merge script.
 *
 * Strips wrapper-only flags (currently just `--skip-checks`) so the merge
 * script never sees arguments it does not understand. All other flags
 * (`--dry-run`, `--force`, `--work-item-id`, `--bump`) pass through unchanged.
 *
 * @param {string[]} [cliArgs] - Full CLI arguments given to run-release.js.
 * @returns {string[]} Arguments safe to forward to the merge script.
 */
export function releaseScriptForwardArgs(cliArgs) {
  return (cliArgs || []).filter((arg) => !WRAPPER_ONLY_FLAGS.has(arg));
}

// ── parsePRUrl ───────────────────────────────────────────────────────────────

/**
 * Extract a GitHub PR URL from the output of the release script.
 * Looks for lines matching `https://github.com/.../pull/<number>`.
 *
 * @param {string} output - The stdout/stderr output from the release script.
 * @returns {string|null} The PR URL, or null if not found.
 */
export function parsePRUrl(output) {
  if (!output) return null;
  const match = output.match(/https:\/\/github\.com\/[^\/]+\/[^\/]+\/pull\/\d+/);
  return match ? match[0] : null;
}

// ── verifyReleaseMerge ───────────────────────────────────────────────────────

/**
 * Verify that a release actually landed on main before work items are closed.
 *
 * Defense-in-depth guard for the close-work-items step (SA-0MSJ2XMQL006CVQS):
 * a dev→main merge must be verified before any "Shipped in v<version>" close
 * may run. Without this, invoking the close step outside a real release (or
 * after a failed merge) spuriously closes real work items.
 *
 * Both conditions must hold:
 *   1. The version tag `v<version>` exists on origin (the release script
 *      creates and pushes it on the merge commit).
 *   2. The tag commit is an ancestor of `origin/main` — i.e. the release
 *      merge actually landed on main.
 *
 * @param {string|null} version - The released semver version (e.g., "0.2.0").
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {(cmd: string) => string} [options.run] - Command runner; defaults
 *   to `execSync` returning trimmed stdout. Tests inject a fake runner to
 *   simulate git state without touching a real repository.
 * @returns {{ success: boolean, message: string }}
 */
export function verifyReleaseMerge(version, options = {}) {
  const run = options.run || ((cmd) => execSync(cmd, {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    maxBuffer: 10 * 1024 * 1024,
  }).toString().trim());

  if (!version) {
    return {
      success: false,
      message: 'No version provided; cannot verify the release merge.',
    };
  }

  // Refresh remote refs so `origin/main` and the version tag reflect the
  // just-completed release. A fetch failure means we cannot prove the merge
  // landed — fail closed and refuse to close work items.
  try {
    run('git fetch origin --prune');
  } catch {
    return {
      success: false,
      message: 'Failed to fetch origin; cannot verify the release merge.',
    };
  }

  // 1. The released version tag must exist on origin.
  let lsRemote = '';
  try {
    lsRemote = run(`git ls-remote origin refs/tags/v${version}`);
  } catch {
    return {
      success: false,
      message: `Failed to query origin for tag v${version}; cannot verify the release merge.`,
    };
  }
  if (!lsRemote.includes(`refs/tags/v${version}`)) {
    return {
      success: false,
      message: `Release tag v${version} was not found on origin; refusing to close work items.`,
    };
  }

  // 2. The tag commit must be an ancestor of origin/main — the dev→main
  //    merge actually landed on main.
  let tagCommit = '';
  try {
    tagCommit = run(`git rev-parse --verify v${version}^{commit}`);
  } catch {
    return {
      success: false,
      message: `Could not resolve tag v${version} to a commit; refusing to close work items.`,
    };
  }
  if (!tagCommit) {
    return {
      success: false,
      message: `Could not resolve tag v${version} to a commit; refusing to close work items.`,
    };
  }

  try {
    run(`git merge-base --is-ancestor ${tagCommit} origin/main`);
  } catch {
    return {
      success: false,
      message: `Release v${version} is not an ancestor of origin/main — the dev→main merge did not land; refusing to close work items.`,
    };
  }

  return {
    success: true,
    message: `Release merge verified: v${version} is on origin/main.`,
  };
}

// ── closeWorkItemsAfterRelease ──────────────────────────────────────────────

/**
 * Close candidate work items after a successful release, filtering by
 * `needsProducerReview`.
 *
 * Uses `getCandidateItems()` from check-audit-gate.js to find items in
 * `in_review` stage or `completed` status (excluding `stage: done`).
 * Only items with `needsProducerReview === false` are closed. Items with
 * `needsProducerReview = true`, `null`, or `undefined` are skipped and
 * logged with "Skipped (needs producer review)".
 *
 * The existing audit gate (Step 2) already validates `audit.readyToClose`,
 * so this step does not re-check `readyToClose`.
 *
 * This is a non-blocking step: individual close failures are logged as
 * warnings and do not affect the return value. Empty candidate sets
 * are handled gracefully.
 *
 * Test-isolation note (SA-0MSJ2XMQL006CVQS): callers MUST NOT invoke this
 * function with the default worklog boundary unless a real, verified release
 * is in progress — the release flow gates the call behind `verifyReleaseMerge()`
 * (Step 8). Unit tests inject `getCandidateItemsFn`/`runCloseCommand` so the
 * test suite never mutates the live worklog.
 *
 * @param {string|null} version - The released semver version (e.g., "0.2.0").
 * @param {object} [options] - Optional injection point (used by unit tests).
 * @param {() => Array<{id: string, title: string, needsProducerReview: boolean|null}>} [options.getCandidateItemsFn] -
 *   Candidate-item query; defaults to `getCandidateItems()` from
 *   check-audit-gate.js (real `wl list`).
 * @param {(itemId: string, reason: string) => void} [options.runCloseCommand] -
 *   Close-command runner; defaults to `wl close <id> --force --reason <r>`.
 * @returns {{ success: boolean, message: string, closedCount: number, errorCount: number, skippedCount: number, skippedItems: Array<{id: string, title: string, reason: string}> }}
 */
export function closeWorkItemsAfterRelease(version, options = {}) {
  const {
    getCandidateItemsFn = getCandidateItems,
    runCloseCommand = (itemId, reason) => execSync(
      `wl close ${itemId} --force --reason "${reason}" --json`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    ),
  } = options;

  if (!version) {
    return {
      success: false,
      message: 'No version provided; skipping close work items step.',
      closedCount: 0,
      errorCount: 0,
      skippedCount: 0,
      skippedItems: [],
    };
  }

  console.log('\nClosing work items shipped in this release...');

  const items = getCandidateItemsFn();

  if (items.length === 0) {
    const message = 'No work items to close — no in_review or completed items found.';
    console.log(message);
    return {
      success: true,
      message,
      closedCount: 0,
      errorCount: 0,
      skippedCount: 0,
      skippedItems: [],
    };
  }

  console.log(`Found ${items.length} work item(s).`);

  // Filter: only close items where needsProducerReview === false
  const toClose = items.filter(
    (item) => item.needsProducerReview === false,
  );
  const skippedItems = items
    .filter(
      (item) => item.needsProducerReview !== false,
    )
    .map((item) => ({
      id: item.id,
      title: item.title,
      reason: 'Skipped (needs producer review)',
    }));

  if (skippedItems.length > 0) {
    console.log(`\nSkipping ${skippedItems.length} work item(s) that need producer review:`);
    for (const skipped of skippedItems) {
      console.log(`  ○ ${skipped.title} (${skipped.id}) — ${skipped.reason}`);
    }
    console.log('');
  }

  if (toClose.length === 0) {
    const message = `No work items to close (${skippedItems.length} skipped, needs producer review).`;
    console.log(message);
    return {
      success: true,
      message,
      closedCount: 0,
      errorCount: 0,
      skippedCount: skippedItems.length,
      skippedItems,
    };
  }

  console.log(`Closing ${toClose.length} work item(s)...`);

  let closedCount = 0;
  let errorCount = 0;
  const errors = [];

  for (const item of toClose) {
    try {
      const reason = `Shipped in v${version}`;
      // --force: the audit gate (Step 2) already verified audit readiness for
      // every candidate, so the close step may bypass the per-item stage/audit
      // re-check. Without it, a parent whose descendant is stuck in a
      // non-terminal state (e.g. left at in_progress by a crashed audit) fails
      // to close recursively, leaving items dangling after the release
      // (SA-0MSAL2NQV0008HY5).
      runCloseCommand(item.id, reason);
      console.log(`  ✓ ${item.title || item.id} — closed with reason: "${reason}"`);
      closedCount++;
    } catch (err) {
      const errorMsg = err.stderr?.toString()?.trim() || err.message;
      console.warn(`  ⚠ Failed to close ${item.id} (${item.title}): ${errorMsg}`);
      errors.push({ id: item.id, title: item.title, error: errorMsg });
      errorCount++;
    }
  }

  let summary;
  if (errorCount === 0) {
    summary = `All ${closedCount} work item(s) closed successfully.`;
  } else {
    summary = `Closed ${closedCount} work item(s); ${errorCount} error(s) (non-fatal).`;
  }

  if (skippedItems.length > 0) {
    summary += ` ${skippedItems.length} item(s) skipped (needs producer review).`;
  }

  console.log(`\n${summary}`);

  return {
    success: errorCount === 0,
    message: errors.length > 0
      ? `${summary}\nErrors: ${errors.map(e => `${e.id}: ${e.error}`).join('; ')}`
      : summary,
    closedCount,
    errorCount,
    skippedCount: skippedItems.length,
    skippedItems,
  };
}

// ── syncDevWithMain ──────────────────────────────────────────────────────────

/**
 * Sync the local `dev` branch with `main` after a successful release.
 *
 * Steps:
 * 1. Fetch latest from origin
 * 2. Checkout `dev` (switches from the release branch back to dev)
 * 3. Merge `origin/main` into `dev` (fast-forward)
 * 4. Push `dev` to origin
 *
 * @returns {{ success: boolean, message: string }}
 */
export function syncDevWithMain() {
  try {
    console.log('\nSyncing dev branch with main...');

    // Step 1: Fetch latest
    execSync('git fetch origin --prune', {
      encoding: 'utf-8',
      stdio: ['pipe', 'inherit', 'pipe'],
    });

    // Step 2: Checkout dev
    execSync('git checkout dev', {
      encoding: 'utf-8',
      stdio: ['pipe', 'inherit', 'pipe'],
    });

    // Step 3: Merge main into dev
    execSync('git merge origin/main', {
      encoding: 'utf-8',
      stdio: ['pipe', 'inherit', 'pipe'],
    });

    // Step 4: Push dev to origin
    execSync('git push origin dev', {
      encoding: 'utf-8',
      stdio: ['pipe', 'inherit', 'pipe'],
    });

    const message = 'dev branch is now in sync with main and pushed to origin.';
    console.log(message);
    return { success: true, message };
  } catch (err) {
    const errorMsg = `Failed to sync dev with main: ${err.stderr?.toString()?.trim() || err.message}`;
    console.error(errorMsg);
    return { success: false, message: errorMsg };
  }
}

// ── waitForPRMerge ───────────────────────────────────────────────────────────

/**
 * Wait for PR status checks to pass, then merge the PR.
 *
 * CI is OPTIONAL for a release:
 *  - If no status checks exist on the PR (repo has no CI), the PR is merged
 *    immediately without a CI gate.
 *  - If status checks are present, they must all complete successfully;
 *    any failed or cancelled check blocks the merge.
 *
 * @param {string} prUrl - The GitHub PR URL.
 * @param {number} [timeoutSeconds=600] - Maximum time to wait for checks.
 * @returns {{ success: boolean, message: string }}
 */
export function waitForPRMerge(prUrl, timeoutSeconds = 600) {
  if (!prUrl) {
    return { success: false, message: 'No PR URL provided; cannot wait for merge.' };
  }

  console.log(`\nWaiting for status checks on ${prUrl}...`);

  const startTime = Date.now();
  const maxWait = timeoutSeconds * 1000;
  const prNumber = prUrl.split('/').pop();

  // Poll for status checks every 10 seconds
  while (Date.now() - startTime < maxWait) {
    try {
      const statusJson = execSync(
        `gh pr view ${prNumber} --json statusCheckRollup`,
        { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
      );
      const status = JSON.parse(statusJson);

      const checks = status.statusCheckRollup || [];

      // No CI configured on this repo/branch — proceed without a CI gate.
      if (checks.length === 0) {
        console.log('No CI status checks present on the PR — proceeding without a CI gate.');
        execSync(`gh pr merge ${prNumber} --merge --delete-branch`, {
          encoding: 'utf-8',
          stdio: ['pipe', 'inherit', 'pipe'],
        });
        return {
          success: true,
          message: `PR ${prUrl} merged successfully.`,
        };
      }

      const allCompleted = checks.every(
        (c) => c.status === 'COMPLETED',
      );
      const anyFailed = checks.some(
        (c) => c.conclusion === 'FAILURE' || c.conclusion === 'CANCELLED',
      );

      if (anyFailed) {
        return {
          success: false,
          message: 'Some CI checks failed on the PR. Manual intervention required.',
        };
      }

      if (allCompleted) {
        console.log('All status checks passed. Merging PR...');
        execSync(`gh pr merge ${prNumber} --merge --delete-branch`, {
          encoding: 'utf-8',
          stdio: ['pipe', 'inherit', 'pipe'],
        });
        return {
          success: true,
          message: `PR ${prUrl} merged successfully.`,
        };
      }

      // Wait 10 seconds before polling again
      const elapsed = Math.round((Date.now() - startTime) / 1000);
      process.stdout.write(`\rWaiting for status checks... (${elapsed}s)`);
    } catch {
      // If gh command fails temporarily, retry
    }

    execSync('sleep 10', { stdio: 'ignore' });
  }

  console.log(''); // newline after progress dots
  return {
    success: false,
    message: `Timed out waiting for status checks on ${prUrl} after ${timeoutSeconds} seconds. Merge the PR manually.`,
  };
}

// ── runRelease ───────────────────────────────────────────────────────────────

/**
 * Main orchestrator for the release process.
 *
 * Steps:
 * 1. Check for unmerged branches (gating, exit code 3)
 * 2. Check audit readiness (gating, exit code 6)
 * 3. Check critical-priority items (gating, exit code 7)
 * 3.5. Check worklog refs (gating, exit code 8)
 * 3.6. Check producer-review status (gating, exit code 9)
 * 4. Find and execute the release script
 * 5. Parse PR URL from release script output
 * 6. Wait for PR merge (if not already merged with --force)
 * 7. Sync dev with main
 * 8. Verify the release merge landed on main (gating, exit code 11)
 * 9. Close work items shipped in this release (non-blocking)
 *
 * @param {string[]} [cliArgs=[]] - Command-line arguments.
 * @returns {number} Exit code (0 = success).
 */
export async function runRelease(cliArgs = []) {
  const projectRoot = resolveProjectRoot();
  setCodeFreezeMarker(projectRoot);
  try {
    return await runReleaseImpl(cliArgs);
  } finally {
    // Cleared on EVERY exit path: success, failure, abort, dry-run, and
    // gating failures (trap/finally-equivalent, contract WL-0MSBU4KMA004PKSR).
    clearCodeFreezeMarker(projectRoot);
  }
}

async function runReleaseImpl(cliArgs = []) {
  const args = [...cliArgs];
  const skipChecks = args.includes('--skip-checks');
  const isDryRun = args.includes('--dry-run');
  const isForce = args.includes('--force');

  // ── Step 1: Check for unmerged branches (gating step) ──────────────────
  if (!skipChecks) {
    const report = checkUnmergedBranches();
    if (report.hasUnmergedBranches) {
      console.error(
        '⚠️  Gating check failed — there are unmerged local branches that should be resolved first:\n',
      );
      console.error(report.message);
      console.error('\nTo bypass this check, re-run with --skip-checks.');
      return 3;
    }
  }

  // ── Step 2: Check audit readiness (gating step) ────────────────────────
  if (!skipChecks) {
    const auditReport = await checkAuditReadyToClose();
    if (auditReport.hasBlockingItems) {
      console.error(
        '⚠️  Audit gate check failed — some work items are not ready to close:\n',
      );
      console.error(auditReport.message);
      console.error('\nTo bypass this check, re-run with --skip-checks.');
      return 6;
    }
  }

  // ── Step 3: Check critical-priority items (gating step) ────────────────
  if (!skipChecks) {
    const criticalReport = checkCriticalItems();
    if (criticalReport.hasBlockingItems) {
      console.error(
        '⚠️  Critical-items gate check failed — some critical items are not in a terminal state:\n',
      );
      console.error(criticalReport.message);
      console.error('\nTo bypass this check, re-run with --skip-checks.');
      return 7;
    }
  }

  // ── Step 3.5: Check worklog refs (gating step) ─────────────────────────
  if (!skipChecks) {
    const worklogReport = checkWorklogRefs();
    if (worklogReport.hasWorklogRefs) {
      console.error(
        '⚠️  Worklog-ref gate check failed — worklog refs are present and must not be merged into main:\n',
      );
      console.error(worklogReport.message);
      console.error('\nTo bypass this check, re-run with --skip-checks.');
      return 8;
    }
  }

  // ── Step 3.6: Check producer-review status (gating step) ───────────────
  if (!skipChecks) {
    // Top-level candidates only: a child's producer review is covered by its
    // parent's review, so children must not block the release
    // (SA-0MSUT8GQP004WSYN AC3).
    const items = getTopLevelCandidateItems();
    const producerReviewReport = checkProducerReviewStatus(items);
    if (producerReviewReport.hasBlockingItems) {
      console.error(
        '⚠️  Producer-review gate check failed — some work items need producer review:\n',
      );
      console.error(producerReviewReport.message);
      console.error('\nTo bypass this check, re-run with --skip-checks.');
      return 9;
    }
  }

  // ── Step 4: Find the release script ───────────────────────────────────
  let selectedScript = null;
  if (existsSync(SKILL_RELEASE_SCRIPT)) {
    selectedScript = SKILL_RELEASE_SCRIPT;
  } else if (existsSync(REPO_RELEASE_SCRIPT)) {
    selectedScript = REPO_RELEASE_SCRIPT;
  }

  if (!selectedScript) {
    const msg = [
      `Ship automated release unavailable: missing canonical release script.`,
      '',
      'Attempted locations: ',
      ` - skill: ${SKILL_RELEASE_SCRIPT}`,
      ` - repository: ${resolve(REPO_RELEASE_SCRIPT)}`,
      '',
      'Human fallback: perform the dev → main promotion manually using the Release Manager checklist:',
      '- See docs/dev/release-process.md for the manual merge workflow and checklist.',
      '- Example manual commands (from repo root):',
      '    git fetch origin',
      '    git checkout main',
      '    git merge origin/dev --no-ff',
      '    git push origin main',
      '',
      "If you want the agent to run an automated release, place the canonical script at '<skill-dir>/scripts/release/merge-dev-to-main.sh' or add it to the repository at 'scripts/release/merge-dev-to-main.sh'.",
    ].join('\n');

    console.error(msg);
    return 2;
  }

  // ── Step 5: Execute the release script ─────────────────────────────────
  console.log('Executing release script...\n');

  // Wrapper-only flags (e.g. --skip-checks) must not reach the merge script,
  // which rejects unknown arguments (SA-0MSKYGAWJ0009M3P).
  const forwardedArgs = releaseScriptForwardArgs(args);
  const child = spawnSync('bash', [selectedScript, ...forwardedArgs], {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
    timeout: RELEASE_SCRIPT_TIMEOUT_MS,
  });

  // Timeout: spawnSync reports a timed-out child with `error.code ===
  // 'ETIMEDOUT'` (status null, signal SIGTERM). Fail loudly — the Code Freeze
  // marker is cleared by the caller's finally on every exit path (exit code
  // 10, see SKILL.md).
  if (child.error && child.error.code === 'ETIMEDOUT') {
    console.error(
      `Release script timed out after ${Math.round(RELEASE_SCRIPT_TIMEOUT_MS / 1000)}s ` +
      'and was terminated. The Code Freeze marker has been cleared. ' +
      'Check for partially-created branches/PRs/refs, then re-run the release.'
    );
    return 10;
  }

  const exitCode = child.status || 0;
  const stdout = child.stdout || '';

  // Print the release script output so the user can see progress
  if (stdout) {
    process.stdout.write(stdout);
  }

  if (exitCode !== 0) {
    console.error(`Release script exited with code ${exitCode}.`);
    return exitCode;
  }

  // If dry-run, don't do post-release steps
  if (isDryRun) {
    console.log('\nDry-run complete. No post-release actions taken.');
    return 0;
  }

  // ── Step 6: Post-release - wait for PR merge and sync dev ──────────────
  const prUrl = parsePRUrl(stdout);

  if (prUrl && !isForce) {
    const mergeResult = waitForPRMerge(prUrl);
    if (!mergeResult.success) {
      console.error(`\n⚠️  ${mergeResult.message}`);
      return 4;
    }
  } else if (!prUrl) {
    console.log('\nNo PR URL detected in release output. Skipping PR merge wait.');
  }

  // ── Step 7: Sync dev with main ─────────────────────────────────────────
  const syncResult = syncDevWithMain();
  if (!syncResult.success) {
    console.error(`\n⚠️  ${syncResult.message}`);
    return 5;
  }

  // ── Step 8: Verify the release merge landed on main (gating) ───────────
  // Merge-verification guard (SA-0MSJ2XMQL006CVQS): only close work items
  // after verifying the release actually landed on main — the version tag
  // exists on origin AND the tag commit is an ancestor of origin/main.
  // This prevents spurious "Shipped in v<version>" closes when the close
  // step runs without a real dev→main merge.
  //
  // Read the released version from the git tag created by the release script
  let version = null;
  try {
    version = execSync('git describe --tags --abbrev=0', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim().replace(/^v/, '');
  } catch {
    // Fallback: read from package.json
    try {
      const pkg = JSON.parse(
        execSync('cat package.json', {
          encoding: 'utf-8',
          stdio: ['pipe', 'pipe', 'pipe'],
        })
      );
      version = pkg.version;
    } catch {
      console.warn('⚠ Unable to determine released version. Skipping close work items step.');
    }
  }

  if (version) {
    const mergeVerification = verifyReleaseMerge(version);
    if (!mergeVerification.success) {
      console.error(`\n⚠️  ${mergeVerification.message}`);
      console.error('Refusing to close work items (exit code 11).');
      return 11;
    }

    // ── Step 9: Close work items shipped in this release (non-blocking) ──
    const closeResult = closeWorkItemsAfterRelease(version);
    if (!closeResult.success && closeResult.errorCount > 0) {
      console.warn(`\n⚠ Non-critical: ${closeResult.message}`);
    }
  }

  return 0;
}

// ── CLI Entry Point ──────────────────────────────────────────────────────────

// Only run when executed directly, not when imported as a module
// Use realpathSync on both sides to handle symlinked install paths:
// import.meta.url resolves to the real path while process.argv[1]
// may retain the symlink path.
const isMainModule = process.argv[1] &&
  (realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1])));

if (isMainModule) {
  runRelease(process.argv.slice(2)).then((exitCode) => {
    process.exitCode = exitCode;
    process.exit(exitCode);
  });
}
