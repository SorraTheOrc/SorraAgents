/**
 * test-agent-push.mjs — Integration tests for agent-side push behaviour.
 *
 * Verifies that agents:
 *   1. Create feature branches using the canonical pattern `wl-<id>-short-desc`.
 *   2. Push completed work into `dev` as the integration step.
 *   3. Never push directly to `main`.
 *   4. Reject force-push / history-rewrite attempts.
 *
 * Run locally from the repository root:
 *
 *   node --test tests/integration/test-agent-push.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBareRemote, createSimRepo } from '../helpers/git-sim.js';

// ---------------------------------------------------------------------------
// Agent push policy — mirrors the rules agents must follow in production.
// These functions simulate the agent-side validation that occurs BEFORE any
// git push command is executed.
// ---------------------------------------------------------------------------

/** Valid branch-name pattern: wl-<work-item-id>-<short-desc> */
const BRANCH_PATTERN = /^wl-SA-[A-Z0-9]+-[a-z0-9-]+$/;

/**
 * Validate a branch name against the canonical agent pattern.
 * Returns { valid: boolean, reason?: string }.
 */
export function validateBranchName(name) {
  if (!BRANCH_PATTERN.test(name)) {
    return {
      valid: false,
      reason: `Branch name "${name}" does not match the required pattern wl-<work-item-id>-short-desc`,
    };
  }
  return { valid: true };
}

/**
 * Determine whether a push target is allowed for agents.
 * Agents may push feature branches to origin and merge into dev,
 * but must NEVER push directly to main.
 */
export function validatePushTarget(targetBranch) {
  if (targetBranch === 'main') {
    return {
      allowed: false,
      reason: 'Agents must not push directly to main. Push to dev instead.',
    };
  }
  return { allowed: true };
}

/**
 * Determine whether a force-push is allowed.
 * Agents must never force-push or rewrite history.
 */
export function validateForcePush() {
  return {
    allowed: false,
    reason: 'Force-push / history-rewrite is not permitted for agents.',
  };
}

/**
 * Simulate the agent-side push routine that validates before executing.
 * Returns { success: boolean, error?: string }.
 * This is the function an agent would call instead of running git push directly.
 */
export function agentPush(repo, remoteBranch, opts = {}) {
  const { force = false } = opts;

  // Step 1: validate branch name
  const currentBranch = repo.currentBranch();
  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    return { success: false, error: branchValidation.reason };
  }

  // Step 2: validate push target
  const targetValidation = validatePushTarget(remoteBranch);
  if (!targetValidation.allowed) {
    return { success: false, error: targetValidation.reason };
  }

  // Step 3: validate force-push
  if (force) {
    const forceValidation = validateForcePush();
    if (!forceValidation.allowed) {
      return { success: false, error: forceValidation.reason };
    }
  }

  // All checks passed — execute the push
  const result = repo.push(remoteBranch, { force });
  if (result.exitCode !== 0) {
    return { success: false, error: `git push failed: ${result.stderr}` };
  }
  return { success: true };
}

// ---------------------------------------------------------------------------
// AC1: Integration tests simulate an agent creating a feature branch and
//      pushing to dev, asserting the push succeeds and dev receives commits.
// ---------------------------------------------------------------------------
test('AC1 — agent creates feature branch and pushes to dev; dev receives the commit', async () => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    // Agent creates a feature branch following the naming pattern
    const workItemId = 'SA-0MPDZD1F5000W0YY';
    const branchName = `wl-${workItemId}-agent-push-tests`;
    repo.createBranch(branchName);
    assert.equal(repo.currentBranch(), branchName, 'Should be on the feature branch');

    // Agent makes a commit on the feature branch
    repo.commitFile('feature.txt', 'feature content\n', `${workItemId}: Add feature`);

    // Agent uses the validated push routine to push into dev
    const result = agentPush(repo, 'dev');
    assert.ok(result.success, `Push to dev should succeed: ${result.error || ''}`);
    assert.ok(remote.hasBranch('dev'), 'Remote should have the dev branch');

    // Verify dev received the commit
    const devLog = remote.log('dev');
    assert.ok(devLog.length >= 2, 'dev branch should have at least 2 commits');
    assert.ok(
      devLog.some((line) => line.includes('Add feature')),
      'dev branch should contain the agent commit',
    );

    // Verify main was NOT modified
    const mainLog = remote.log('main');
    assert.equal(mainLog.length, 1, 'main branch should still have only the initial commit');
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// AC2: Tests assert that agent attempts to push to main are rejected/blocked
//      by the agent logic (no direct push performed).
// ---------------------------------------------------------------------------
test('AC2 — agent push to main is rejected by agent logic; no push performed', async () => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    // Agent creates a feature branch and makes a commit
    const branchName = 'wl-SA-0MPDZD1F5000W0YY-test-main-block';
    repo.createBranch(branchName);
    repo.commitFile('change.txt', 'some change\n', 'SA-0MPDZD1F5000W0YY: test change');

    // Agent attempts to push to main using the validated push routine
    const result = agentPush(repo, 'main');

    // The agent logic must reject the push BEFORE any git command runs
    assert.equal(result.success, false, 'Push to main must be rejected');
    assert.ok(
      result.error.includes('must not push directly to main'),
      `Rejection reason should mention main restriction, got: ${result.error}`,
    );

    // Verify that main was NOT modified on the remote
    const mainLog = remote.log('main');
    assert.equal(mainLog.length, 1, 'main must still have only the initial commit');
    assert.ok(
      mainLog[0].includes('Initial commit'),
      'main branch should only have the initial commit',
    );

    // Verify dev was not created either (nothing was pushed)
    assert.equal(remote.hasBranch('dev'), false, 'No branch should have been created on remote');
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// AC3: Tests verify that branch names follow the pattern wl-<id>-short-desc.
// ---------------------------------------------------------------------------
test('AC3 — branch names must follow wl-<id>-short-desc pattern', () => {
  // Valid branch names — these should be accepted
  const validNames = [
    'wl-SA-0MPDZD1F5000W0YY-agent-push-tests',
    'wl-SA-42-fix-bug',
    'wl-SA-ABC123-add-new-feature',
  ];

  for (const name of validNames) {
    const result = validateBranchName(name);
    assert.ok(result.valid, `Expected "${name}" to be a valid branch name`);
  }

  // Invalid branch names — these should be rejected with a reason
  const invalidNames = [
    'main',                              // not an agent branch
    'feature/add-auth',                  // missing wl- prefix and id
    'wl-SA-0MPDZD1F5000W0YY',            // missing short-desc portion
    'SA-0MPDZD1F5000W0YY-my-work',       // missing wl- prefix
    'wl-SA-0MPDZD1F5000W0YY-UPPER_CASE', // short-desc must be lowercase
    'random-branch-name',                // completely wrong pattern
    'wl-SA-0MPDZD1F5000W0YY-',           // trailing dash with no desc
    '',                                  // empty string
  ];

  for (const name of invalidNames) {
    const result = validateBranchName(name);
    assert.equal(result.valid, false, `Expected "${name}" to be invalid`);
    assert.ok(result.reason, `Invalid branch "${name}" should have a reason`);
  }
});

// ---------------------------------------------------------------------------
// AC4: Tests include a failure case: attempted force-push or history-rewrite
//      is rejected and flagged.
// ---------------------------------------------------------------------------
test('AC4 — force-push is rejected and flagged by agent logic', async () => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    // Agent creates a feature branch and pushes it normally first
    const branchName = 'wl-SA-0MPDZD1F5000W0YY-force-push-test';
    repo.createBranch(branchName);
    repo.commitFile('file.txt', 'v1\n', 'SA-0MPDZD1F5000W0YY: first commit');

    // Normal push succeeds
    const normalResult = agentPush(repo, branchName);
    assert.ok(normalResult.success, 'Normal push should succeed');
    assert.ok(remote.hasBranch(branchName), 'Remote should have the feature branch');

    // Agent amends the commit (simulating a history rewrite scenario)
    repo.commitFile('file.txt', 'v2 - amended\n', 'SA-0MPDZD1F5000W0YY: amend commit');

    // Agent attempts a force-push to update the remote
    const forceResult = agentPush(repo, branchName, { force: true });

    // The agent logic must reject the force-push BEFORE any git command runs
    assert.equal(forceResult.success, false, 'Force-push must be rejected by agent logic');
    assert.ok(
      forceResult.error.includes('Force-push') || forceResult.error.includes('history-rewrite'),
      `Rejection reason should mention force-push, got: ${forceResult.error}`,
    );

    // Verify the remote still has the original commit (no history was rewritten)
    const remoteLog = remote.log(branchName);
    assert.ok(
      remoteLog.some((line) => line.includes('first commit')),
      'Remote should have the original first commit',
    );
    assert.ok(
      !remoteLog.some((line) => line.includes('amend commit')),
      'Remote must NOT have the amended commit (force-push was rejected)',
    );
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// End-to-end: full agent workflow — branch, commit, push to dev; main untouched
// ---------------------------------------------------------------------------
test('E2E — full agent workflow: dev updated, main completely untouched', async () => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Seed the remote with main
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    const initialMainLog = remote.log('main');

    // Full agent workflow
    const workItemId = 'SA-0MPTEST';
    const branchName = `wl-${workItemId}-e2e-test`;

    // Step 1: validate branch name
    assert.ok(validateBranchName(branchName).valid, 'Branch name should be valid');

    // Step 2: create feature branch
    repo.createBranch(branchName);
    assert.equal(repo.currentBranch(), branchName);

    // Step 3: validate push target is NOT main
    assert.equal(validatePushTarget('main').allowed, false, 'Push to main must be blocked');

    // Step 4: make changes and commit
    repo.commitFile('src/feature.js', 'export const feature = true;\n', `${workItemId}: implement feature`);

    // Step 5: push into dev for integration (using validated agentPush)
    const pushResult = agentPush(repo, 'dev');
    assert.ok(pushResult.success, `Push to dev should succeed: ${pushResult.error || ''}`);
    assert.ok(remote.hasBranch('dev'), 'Remote should have dev branch');

    // Step 6: verify dev has the new commit
    const devLog = remote.log('dev');
    assert.ok(
      devLog.some((line) => line.includes('implement feature')),
      'dev should contain the feature commit',
    );

    // Step 7: verify main is completely untouched
    const finalMainLog = remote.log('main');
    assert.deepEqual(finalMainLog, initialMainLog, 'main branch must not have changed');
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});
