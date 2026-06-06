#!/usr/bin/env node
/**
 * commit.mjs — Stage changes and commit with conventional format + work-item reference.
 *
 * Usage:
 *   node skill/git-management/scripts/commit.mjs --message <msg> --work-item <id> [--all] [--dry-run] [--json]
 *
 * Stages specified files (or all with --all), creates a conventional commit
 * message with work-item reference, and commits.
 *
 * Exit codes:
 *   0 — Success
 *   1 — General error (missing args, empty commit)
 *   2 — Safety violation (missing work-item reference)
 *   3 — Prerequisite not met (not in a git repo)
 */

import {
  parseArgs,
  hasFlag,
  getFlag,
  jsonOutput,
  humanMsg,
  humanError,
  humanSuccess,
  checkPrerequisites,
  safeExec,
  validateWorkItemId,
  EXIT,
} from './git-mgmt-helpers.mjs';

// ── Conventional commit types ────────────────────────────────────────────────

const VALID_COMMIT_TYPES = Object.freeze([
  'feat', 'fix', 'docs', 'style', 'refactor', 'test', 'chore', 'perf', 'ci', 'build', 'revert',
]);

/**
 * Parse a conventional commit message: "type(scope): description" or "type: description"
 * @param {string} msg
 * @returns {{ type: string, scope?: string, description: string } | null}
 */
function parseConventionalCommit(msg) {
  const match = msg.match(/^([a-z]+)(\([^)]+\))?:\s+(.+)$/);
  if (!match) return null;
  return {
    type: match[1],
    scope: match[2] ? match[2].slice(1, -1) : undefined,
    description: match[3],
  };
}

/**
 * Format a conventional commit message with work-item reference.
 * @param {string} message - The user-provided message (may or may not be conventional)
 * @param {string} workItemId
 * @returns {string}
 */
function formatCommitMessage(message, workItemId) {
  const parsed = parseConventionalCommit(message);

  if (parsed) {
    // Already conventional — append work-item ref if not present
    if (!message.includes(workItemId)) {
      return `${message} (${workItemId})`;
    }
    return message;
  }

  // Not conventional — wrap as chore
  if (!message.includes(workItemId)) {
    return `chore: ${message} (${workItemId})`;
  }
  return `chore: ${message}`;
}

// ── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const { flags, positional } = parseArgs(process.argv.slice(2));
  const dryRun = hasFlag(flags, 'dry-run');
  const asJson = hasFlag(flags, 'json');
  const allFlag = hasFlag(flags, 'all');

  // Require --message and --work-item
  const message = getFlag(flags, 'message') || getFlag(flags, 'm');
  const workItemId = getFlag(flags, 'work-item') || getFlag(flags, 'w');

  if (!message || typeof message !== 'string') {
    const msg = 'Required: --message <commit-message>';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  if (!workItemId || typeof workItemId !== 'string') {
    const msg = 'Required: --work-item <work-item-id>';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.SAFETY_VIOLATION);
    humanError(msg, EXIT.SAFETY_VIOLATION);
  }

  // Validate work-item ID
  const idValidation = validateWorkItemId(workItemId);
  if (!idValidation.valid) {
    if (asJson) jsonOutput({ success: false, error: idValidation.reason }, EXIT.SAFETY_VIOLATION);
    humanError(idValidation.reason, EXIT.SAFETY_VIOLATION);
  }

  // Check prerequisites
  const prereq = checkPrerequisites(['git'], { requireGitDir: true });
  if (!prereq.ok) {
    if (asJson) jsonOutput({ success: false, error: prereq.errors.join('; ') }, EXIT.PREREQ_NOT_MET);
    humanError(prereq.errors.join('; '), EXIT.PREREQ_NOT_MET);
  }

  // Stage files
  if (allFlag === true) {
    const stageResult = safeExec('git add -A');
    if (!stageResult.success) {
      const msg = `Failed to stage all changes: ${stageResult.stderr}`;
      if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
      humanError(msg, EXIT.GENERAL_ERROR);
    }
  } else if (positional.length > 0) {
    // Stage specific files
    const files = positional.join(' ');
    const stageResult = safeExec(`git add ${files}`);
    if (!stageResult.success) {
      const msg = `Failed to stage files: ${stageResult.stderr}`;
      if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
      humanError(msg, EXIT.GENERAL_ERROR);
    }
  }

  // Check if there are staged changes
  const stagedCheck = safeExec('git diff --cached --name-only');
  if (!stagedCheck.success || stagedCheck.stdout.trim() === '') {
    const msg = 'No staged changes to commit. Use --all to stage all changes, or specify file paths.';
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  const stagedFiles = stagedCheck.stdout.split('\n').filter(Boolean);

  // Format commit message
  const commitMessage = formatCommitMessage(message, workItemId);

  // Dry-run
  if (dryRun) {
    const result = {
      success: true,
      dryRun: true,
      commitMessage,
      stagedFiles,
      message: `Would commit with message: ${commitMessage}`,
    };
    if (asJson) jsonOutput(result);
    humanSuccess({
      message: `[DRY RUN] Would commit with message: ${commitMessage}`,
      details: {
        stagedFiles: stagedFiles.join(', '),
        workItemId,
      },
    });
  }

  // Commit
  // Escape single quotes in commit message for shell
  const escapedMessage = commitMessage.replace(/'/g, "'\"'\"'");
  const commitResult = safeExec(`git commit -m '${escapedMessage}'`);
  if (!commitResult.success) {
    const msg = `Commit failed: ${commitResult.stderr}`;
    if (asJson) jsonOutput({ success: false, error: msg }, EXIT.GENERAL_ERROR);
    humanError(msg, EXIT.GENERAL_ERROR);
  }

  // Extract commit hash
  const hashResult = safeExec('git rev-parse HEAD');
  const commitHash = hashResult.success ? hashResult.stdout : 'unknown';

  const result = {
    success: true,
    commitHash,
    commitMessage,
    stagedFiles,
    workItemId,
    message: `Committed: ${commitMessage}`,
  };

  if (asJson) jsonOutput(result);
  humanSuccess({
    message: `Committed: ${commitMessage}`,
    details: {
      commitHash,
      stagedFiles: stagedFiles.join(', '),
    },
  });
}

main();
