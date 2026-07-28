#!/usr/bin/env node
// run-release.js — safe wrapper to invoke repository-level release script
// Usage: node run-release.js [--dry-run] [--work-item-id <id>] [--force] [--skip-checks] [--bump patch|minor|major]
//
// The --bump flag is passed through to the canonical release script
// (merge-dev-to-main.sh) and controls which part of the semver is
// incremented before the merge. Default is 'patch'.

import { existsSync, realpathSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { checkUnmergedBranches } from './check-unmerged-branches.js';
import { checkAuditReadyToClose, getCandidateItems } from './check-audit-gate.js';
import { checkCriticalItems } from './check-critical-items.js';
import { checkWorklogRefs } from './check-worklog-refs.js';

// Canonical release script path relative to repository root
const REPO_RELEASE_SCRIPT = 'scripts/release/merge-dev-to-main.sh';

// Also accept a skill-level release script (e.g., installed under the skill directory)
// Skill layout: <skill-dir>/scripts/release/merge-dev-to-main.sh
const skillDir = dirname(dirname(fileURLToPath(import.meta.url)));
const SKILL_RELEASE_SCRIPT = join(skillDir, 'scripts', 'release', 'merge-dev-to-main.sh');

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

// ── closeWorkItemsAfterRelease ──────────────────────────────────────────────

/**
 * Close all candidate work items after a successful release.
 *
 * Uses `getCandidateItems()` from check-audit-gate.js to find items in
 * `in_review` stage or `completed` status (excluding `stage: done`). For
 * each candidate item, runs `wl close <id> --reason "Shipped in v<version>"`.
 *
 * This is a non-blocking step: individual close failures are logged as
 * warnings and do not affect the return value. Empty candidate sets
 * are handled gracefully.
 *
 * @param {string|null} version - The released semver version (e.g., "0.2.0").
 * @returns {{ success: boolean, message: string, closedCount: number, errorCount: number }}
 */
export function closeWorkItemsAfterRelease(version) {
  if (!version) {
    return {
      success: false,
      message: 'No version provided; skipping close work items step.',
      closedCount: 0,
      errorCount: 0,
    };
  }

  console.log('\nClosing work items shipped in this release...');

  const items = getCandidateItems();

  if (items.length === 0) {
    const message = 'No work items to close — no in_review or completed items found.';
    console.log(message);
    return {
      success: true,
      message,
      closedCount: 0,
      errorCount: 0,
    };
  }

  console.log(`Found ${items.length} work item(s) to close.`);

  let closedCount = 0;
  let errorCount = 0;
  const errors = [];

  for (const item of items) {
    try {
      const reason = `Shipped in v${version}`;
      execSync(`wl close ${item.id} --reason "${reason}" --json`, {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      });
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

  console.log(`\n${summary}`);

  return {
    success: errorCount === 0,
    message: errors.length > 0
      ? `${summary}\nErrors: ${errors.map(e => `${e.id}: ${e.error}`).join('; ')}`
      : summary,
    closedCount,
    errorCount,
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
 * Wait for CI status checks to pass on a PR, then merge it.
 *
 * @param {string} prUrl - The GitHub PR URL.
 * @param {number} [timeoutSeconds=600] - Maximum time to wait for checks.
 * @returns {{ success: boolean, message: string }}
 */
export function waitForPRMerge(prUrl, timeoutSeconds = 600) {
  if (!prUrl) {
    return { success: false, message: 'No PR URL provided; cannot wait for merge.' };
  }

  console.log(`\nWaiting for CI checks to pass on ${prUrl}...`);

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
        console.log('All CI checks passed. Merging PR...');
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
      process.stdout.write(`\rWaiting for CI checks... (${elapsed}s)`);
    } catch {
      // If gh command fails temporarily, retry
    }

    execSync('sleep 10', { stdio: 'ignore' });
  }

  console.log(''); // newline after progress dots
  return {
    success: false,
    message: `Timed out waiting for CI checks on ${prUrl} after ${timeoutSeconds} seconds. Merge the PR manually.`,
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
 * 4. Find and execute the release script
 * 5. Parse PR URL from release script output
 * 6. Wait for PR merge (if not already merged with --force)
 * 7. Sync dev with main
 * 8. Close work items shipped in this release (non-blocking)
 *
 * @param {string[]} [cliArgs=[]] - Command-line arguments.
 * @returns {number} Exit code (0 = success).
 */
export async function runRelease(cliArgs = []) {
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

  const child = spawnSync('bash', [selectedScript, ...args], {
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });

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

  // ── Step 8: Close work items shipped in this release (non-blocking) ────
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
