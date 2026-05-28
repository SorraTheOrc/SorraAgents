/**
 * test-agent-push.mjs — Integration tests for agent-side push behaviour.
 *
 * Verifies that agents:
 *   1. Create feature branches using the canonical pattern `wl-<id>-short-desc`.
 *   2. Push completed work into `dev` as the integration step.
 *   3. Never push directly to `main`.
 *   4. Reject force-push / history-rewrite attempts.
 *   5. Return non-zero status on push failure (non-fast-forward / conflicts).
 *
 * Run locally from the repository root:
 *
 *   node --test tests/integration/test-agent-push.mjs
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import {
  validatePushTarget,
  validateForcePush,
  DEV_BRANCH,
  PROTECTED_BRANCHES,
} from '../../agent/ship.js';
import { validateBranchName } from '../../agent/git-helpers.js';
import { createBareRemote, createSimRepo } from '../helpers/git-sim.js';

// ---------------------------------------------------------------------------
// Helper: simulate the agent push routine that validates before executing.
// This mirrors what an agent would do — call the validation functions from
// ship.js before actually pushing via the sim repo.
// ---------------------------------------------------------------------------
function simulatedAgentPush(repo, remoteBranch, opts = {}) {
  const { force = false } = opts;

  // Step 1: validate force-push
  if (force) {
    const forceValidation = validateForcePush();
    return { success: false, error: forceValidation.reason };
  }

  // Step 2: validate push target
  const targetValidation = validatePushTarget(remoteBranch);
  if (!targetValidation.allowed) {
    return { success: false, error: targetValidation.reason };
  }

  // Step 3: validate branch name
  const currentBranch = repo.currentBranch();
  const branchValidation = validateBranchName(currentBranch);
  if (!branchValidation.valid) {
    return { success: false, error: branchValidation.reason };
  }

  // All checks passed — execute the push via sim repo
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
    const workItemId = 'SA-0MPDZDVG00013CQJ';
    const branchName = `wl-${workItemId}-agent-push-tests`;
    repo.createBranch(branchName);
    assert.equal(repo.currentBranch(), branchName, 'Should be on the feature branch');

    // Agent makes a commit on the feature branch
    repo.commitFile('feature.txt', 'feature content\n', `${workItemId}: Add feature`);

    // Agent uses the validated push routine to push into dev
    const result = simulatedAgentPush(repo, 'dev');
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
    const branchName = 'wl-SA-0MPDZDVG00013CQJ-test-main-block';
    repo.createBranch(branchName);
    repo.commitFile('change.txt', 'some change\n', 'SA-0MPDZDVG00013CQJ: test change');

    // Agent attempts to push to main using the validated push routine
    const result = simulatedAgentPush(repo, 'main');

    // The agent logic must reject the push BEFORE any git command runs
    assert.equal(result.success, false, 'Push to main must be rejected');
    assert.ok(
      result.error.includes('must not push directly') || result.error.includes('main'),
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
test('AC3 — branch names must follow wl-<id>-short-desc pattern', async () => {
  // Valid branch names — these should be accepted
  const validNames = [
    'wl-SA-0MPDZDVG00013CQJ-agent-push-tests',
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
    'wl-SA-0MPDZDVG00013CQJ',            // missing short-desc portion
    'SA-0MPDZDVG00013CQJ-my-work',       // missing wl- prefix
    'wl-SA-0MPDZDVG00013CQJ-UPPER_CASE', // short-desc must be lowercase
    'random-branch-name',                // completely wrong pattern
    'wl-SA-0MPDZDVG00013CQJ-',           // trailing dash with no desc
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
    const branchName = 'wl-SA-0MPDZDVG00013CQJ-force-push-test';
    repo.createBranch(branchName);
    repo.commitFile('file.txt', 'v1\n', 'SA-0MPDZDVG00013CQJ: first commit');

    // Normal push succeeds
    const normalResult = simulatedAgentPush(repo, branchName);
    assert.ok(normalResult.success, 'Normal push should succeed');
    assert.ok(remote.hasBranch(branchName), 'Remote should have the feature branch');

    // Agent amends the commit (simulating a history rewrite scenario)
    repo.commitFile('file.txt', 'v2 - amended\n', 'SA-0MPDZDVG00013CQJ: amend commit');

    // Agent attempts a force-push to update the remote
    const forceResult = simulatedAgentPush(repo, branchName, { force: true });

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
// AC5: Push failure (non-fast-forward) returns non-zero status, does not
//      rewrite history.
// ---------------------------------------------------------------------------
test('AC5 — non-fast-forward push failure returns error, does not rewrite history', async () => {
  const remote = createBareRemote();
  const repo1 = createSimRepo(remote.url);
  const repo2 = createSimRepo(remote.url);

  try {
    // Set up the initial main branch on the remote
    repo1.configureIdentity();
    repo1.commitFile('README.md', '# Project\n', 'Initial commit');
    repo1.push('main');

    // Both repos create the same feature branch
    const branchName = 'wl-SA-0MPDZDVG00013CQJ-conflict-test';

    // Repo 1: push first
    repo1.createBranch(branchName);
    repo1.commitFile('shared.txt', 'repo1 content\n', 'SA: repo1 commit');
    const result1 = simulatedAgentPush(repo1, 'dev');
    assert.ok(result1.success, 'First push to dev should succeed');

    // Repo 2: has diverged state — push will conflict
    repo2.configureIdentity();
    repo2.createBranch(branchName);
    // Create a different commit that will conflict on push
    repo2.commitFile('shared.txt', 'repo2 content\n', 'SA: repo2 commit');

    // This push should fail (non-fast-forward)
    const result2 = simulatedAgentPush(repo2, 'dev');
    assert.equal(result2.success, false, 'Second push to dev should fail due to conflict');
    assert.ok(
      result2.error.includes('rejected') || result2.error.includes('non-fast-forward') || result2.error.includes('failed'),
      `Error should indicate push rejection, got: ${result2.error}`,
    );
  } finally {
    repo1.cleanup();
    repo2.cleanup();
    remote.cleanup();
  }
});

// ---------------------------------------------------------------------------
// validatePushTarget unit tests
// ---------------------------------------------------------------------------
describe('validatePushTarget', () => {
  test('blocks push to main', () => {
    const result = validatePushTarget('main');
    assert.equal(result.allowed, false);
    assert.ok(result.reason.includes('main'));
  });

  test('blocks push to master', () => {
    const result = validatePushTarget('master');
    assert.equal(result.allowed, false);
  });

  test('blocks push to HEAD', () => {
    const result = validatePushTarget('HEAD');
    assert.equal(result.allowed, false);
  });

  test('allows push to dev', () => {
    const result = validatePushTarget('dev');
    assert.equal(result.allowed, true);
  });

  test('allows push to feature branches', () => {
    const result = validatePushTarget('wl-SA-001-feature');
    assert.equal(result.allowed, true);
  });

  test('rejects empty string', () => {
    const result = validatePushTarget('');
    assert.equal(result.allowed, false);
  });

  test('rejects undefined', () => {
    const result = validatePushTarget(undefined);
    assert.equal(result.allowed, false);
  });
});

// ---------------------------------------------------------------------------
// validateForcePush unit tests
// ---------------------------------------------------------------------------
describe('validateForcePush', () => {
  test('always returns not allowed', () => {
    const result = validateForcePush();
    assert.equal(result.allowed, false);
    assert.ok(result.reason.includes('Force-push') || result.reason.includes('history-rewrite'));
  });
});

// ---------------------------------------------------------------------------
// PROTECTED_BRANCHES constant
// ---------------------------------------------------------------------------
describe('PROTECTED_BRANCHES', () => {
  test('contains main', () => {
    assert.ok(PROTECTED_BRANCHES.includes('main'));
  });

  test('contains master', () => {
    assert.ok(PROTECTED_BRANCHES.includes('master'));
  });

  test('contains HEAD', () => {
    assert.ok(PROTECTED_BRANCHES.includes('HEAD'));
  });

  test('is frozen', () => {
    assert.throws(() => PROTECTED_BRANCHES.push('dev'));
  });
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

    // Step 5: push into dev for integration (using simulated agent push)
    const pushResult = simulatedAgentPush(repo, 'dev');
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
