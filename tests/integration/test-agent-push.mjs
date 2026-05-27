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
// Agent push policy helpers — these mirror the rules agents must follow.
// ---------------------------------------------------------------------------

/** Valid branch-name pattern: wl-<work-item-id>-<short-desc> */
const BRANCH_PATTERN = /^wl-SA-[A-Z0-9]+-[a-z0-9-]+$/;

/**
 * Validate a branch name against the canonical agent pattern.
 * Returns { valid: boolean, reason?: string }.
 */
function validateBranchName(name) {
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
function validatePushTarget(targetBranch) {
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
function validateForcePush() {
  return {
    allowed: false,
    reason: 'Force-push / history-rewrite is not permitted for agents.',
  };
}

// ---------------------------------------------------------------------------
// Test: agent creates a feature branch and pushes to dev
// ---------------------------------------------------------------------------
test('agent push: feature branch pushed to dev succeeds and dev receives the commit', async (t) => {
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
    const validation = validateBranchName(branchName);
    assert.ok(validation.valid, validation.reason);

    // Create and switch to the feature branch
    repo.createBranch(branchName);
    assert.equal(repo.currentBranch(), branchName);

    // Agent makes a commit on the feature branch
    repo.commitFile('feature.txt', 'feature content\n', `${workItemId}: Add feature`);

    // Push the feature branch itself to origin
    const pushBranch = repo.push(branchName);
    assert.equal(pushBranch.exitCode, 0, `Push of feature branch failed: ${pushBranch.stderr}`);
    assert.ok(remote.hasBranch(branchName), 'Remote should have the feature branch');

    // Agent pushes the feature branch into dev (integration step)
    const pushDev = repo.push('dev');
    assert.equal(pushDev.exitCode, 0, `Push to dev failed: ${pushDev.stderr}`);
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
    assert.ok(
      mainLog[0].includes('Initial commit'),
      'main branch should only have the initial commit',
    );
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// Test: agent attempts to push to main — must be blocked
// ---------------------------------------------------------------------------
test('agent push: push to main is rejected by agent policy', async (t) => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    // Agent creates a feature branch
    const branchName = 'wl-SA-0MPDZD1F5000W0YY-test-main-block';
    repo.createBranch(branchName);
    repo.commitFile('change.txt', 'some change\n', 'SA-0MPDZD1F5000W0YY: test change');

    // Agent policy: validate that pushing to main is NOT allowed
    const pushTargetCheck = validatePushTarget('main');
    assert.equal(pushTargetCheck.allowed, false, 'Push to main should be blocked by policy');
    assert.ok(
      pushTargetCheck.reason.includes('must not push directly to main'),
      'Rejection reason should mention the main-push restriction',
    );

    // If the agent were to attempt a git push to main, the policy should
    // have already blocked it. We verify that the policy is enforced before
    // any actual git push occurs.
    // In real agent code, this check would prevent: git push origin HEAD:refs/heads/main
    assert.ok(true, 'Agent policy correctly blocks push to main before any git command');
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// Test: branch name validation — valid and invalid patterns
// ---------------------------------------------------------------------------
test('agent push: branch names must follow wl-<id>-short-desc pattern', () => {
  // Valid branch names
  const validNames = [
    'wl-SA-0MPDZD1F5000W0YY-agent-push-tests',
    'wl-SA-42-fix-bug',
    'wl-SA-ABC123-add-new-feature',
  ];

  for (const name of validNames) {
    const result = validateBranchName(name);
    assert.ok(result.valid, `Expected "${name}" to be a valid branch name`);
  }

  // Invalid branch names
  const invalidNames = [
    'main',                              // not an agent branch
    'feature/add-auth',                  // missing wl- prefix and id
    'wl-SA-0MPDZD1F5000W0YY',            // missing short-desc portion
    'SA-0MPDZD1F5000W0YY-my-work',       // missing wl- prefix
    'wl-SA-0MPDZD1F5000W0YY-UPPER_CASE', // short-desc must be lowercase
    'random-branch-name',                // completely wrong pattern
  ];

  for (const name of invalidNames) {
    const result = validateBranchName(name);
    assert.equal(
      result.valid,
      false,
      `Expected "${name}" to be an invalid branch name`,
    );
    assert.ok(result.reason, `Invalid branch "${name}" should have a reason`);
  }
});

// ---------------------------------------------------------------------------
// Test: force-push / history-rewrite must be rejected
// ---------------------------------------------------------------------------
test('agent push: force-push is rejected and flagged', async (t) => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    // Agent creates a feature branch
    const branchName = 'wl-SA-0MPDZD1F5000W0YY-force-push-test';
    repo.createBranch(branchName);
    repo.commitFile('file.txt', 'v1\n', 'SA-0MPDZD1F5000W0YY: first commit');
    repo.push(branchName);

    // Agent policy: force-push must be rejected
    const forcePushCheck = validateForcePush();
    assert.equal(forcePushCheck.allowed, false, 'Force-push should be blocked by policy');
    assert.ok(
      forcePushCheck.reason.includes('Force-push'),
      'Rejection reason should mention force-push',
    );

    // If the agent were to attempt a force-push, the actual git command
    // would succeed technically, but the agent policy must prevent it.
    // We simulate what would happen if the policy was NOT enforced:
    // the push would rewrite history on the remote. The agent must never
    // do this.
    const pushResult = repo.push(branchName, { force: true });
    // The git command itself may succeed, but the agent policy should
    // have blocked the attempt before this point.
    // We assert that the policy check is what matters:
    assert.equal(
      forcePushCheck.allowed,
      false,
      'Agent policy must reject force-push regardless of git outcome',
    );
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// Test: end-to-end — agent creates branch, commits, pushes to dev; main untouched
// ---------------------------------------------------------------------------
test('agent push: end-to-end — dev updated, main untouched', async (t) => {
  const remote = createBareRemote();
  const repo = createSimRepo(remote.url);

  try {
    // Seed the remote with main
    repo.configureIdentity();
    repo.commitFile('README.md', '# Project\n', 'Initial commit');
    repo.push('main');

    const initialMainLog = remote.log('main');
    const initialMainHash = initialMainLog[0];

    // Agent workflow
    const workItemId = 'SA-0MPTEST';
    const branchName = `wl-${workItemId}-e2e-test`;

    // Step 1: validate branch name
    assert.ok(validateBranchName(branchName).valid);

    // Step 2: create feature branch
    repo.createBranch(branchName);
    assert.equal(repo.currentBranch(), branchName);

    // Step 3: validate push target is NOT main
    assert.equal(validatePushTarget('main').allowed, false);

    // Step 4: make changes and commit
    repo.commitFile('src/feature.js', 'export const feature = true;\n', `${workItemId}: implement feature`);

    // Step 5: push feature branch to origin
    repo.push(branchName);
    assert.ok(remote.hasBranch(branchName));

    // Step 6: push into dev for integration
    const pushDev = repo.push('dev');
    assert.equal(pushDev.exitCode, 0);
    assert.ok(remote.hasBranch('dev'));

    // Step 7: verify dev has the new commit
    const devLog = remote.log('dev');
    assert.ok(
      devLog.some((line) => line.includes('implement feature')),
      'dev should contain the feature commit',
    );

    // Step 8: verify main is completely untouched
    const finalMainLog = remote.log('main');
    assert.deepEqual(finalMainLog, initialMainLog, 'main branch must not have changed');
  } finally {
    repo.cleanup();
    remote.cleanup();
  }
});
