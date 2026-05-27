/**
 * Integration test: Conflict handling & work item creation.
 *
 * Verifies agent behaviour when merge conflicts occur:
 * - Agents detect conflicts when attempting to push/merge into dev.
 * - The agent fails the push and creates a merge-conflict work item.
 * - The created work item contains reproduction steps, branch references,
 *   and acceptance criteria.
 *
 * Deliverable: tests/integration/test-conflict-handling.mjs
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFile, execFileSync } from 'node:child_process';
import { promisify } from 'node:util';

import {
  createWorkItem,
  getWorkItem,
  searchWorkItems,
  listByTag,
  uniqueTestTag,
} from '../helpers/wl-test-helpers.mjs';

const exec = promisify(execFile);

// ---------------------------------------------------------------------------
// Git helpers for temporary test repositories.
// ---------------------------------------------------------------------------

/**
 * Run a git command in a specific directory.
 */
function git(cwd, args) {
  return execFileSync('git', args, { cwd, encoding: 'utf-8' });
}

/**
 * Create a temporary git repo with a conflict scenario between a feature
 * branch and the dev branch. Returns the repo path and branch metadata.
 */
function createConflictRepo() {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'conflict-test-'));

  // Initialise repo and rename default branch to main.
  git(tmpDir, ['init']);
  git(tmpDir, ['branch', '-m', 'main']);
  git(tmpDir, ['config', 'user.email', 'test@example.com']);
  git(tmpDir, ['config', 'user.name', 'Test Agent']);

  // Create initial commit on main with a shared file.
  const sharedFile = path.join(tmpDir, 'shared.txt');
  fs.writeFileSync(sharedFile, 'line 1\nline 2\nline 3\n');
  git(tmpDir, ['add', '.']);
  git(tmpDir, ['commit', '-m', 'Initial commit']);

  // Create dev branch and modify the shared file.
  git(tmpDir, ['branch', 'dev']);
  git(tmpDir, ['checkout', 'dev']);
  fs.writeFileSync(sharedFile, 'line 1\nline 2 from dev\nline 3\n');
  git(tmpDir, ['add', '.']);
  git(tmpDir, ['commit', '-m', 'dev: modify line 2']);

  // Create feature branch from main (before dev changes) and make
  // a conflicting modification.
  git(tmpDir, ['checkout', '-b', 'feature/wl-42-test-change', 'main']);
  fs.writeFileSync(sharedFile, 'line 1\nline 2 from feature\nline 3\n');
  git(tmpDir, ['add', '.']);
  git(tmpDir, ['commit', '-m', 'feature: modify line 2 differently']);

  return {
    repoPath: tmpDir,
    featureBranch: 'feature/wl-42-test-change',
    targetBranch: 'dev',
    conflictingFile: 'shared.txt',
  };
}

/**
 * Attempt to merge the feature branch into the target branch.
 * Returns { success: boolean, stdout: string, error: string|null }.
 */
function attemptMerge(repoPath, targetBranch, featureBranch) {
  try {
    git(repoPath, ['checkout', targetBranch]);
    const stdout = git(repoPath, ['merge', '--no-edit', featureBranch]);
    return { success: true, stdout, error: null };
  } catch (err) {
    // Git merge with conflicts exits non-zero.
    const output = err.stdout ?? '';
    const stderr = err.stderr ?? '';
    // Abort the merge so the repo is clean for the next test.
    try {
      git(repoPath, ['merge', '--abort']);
    } catch {
      // Merge may not be in progress — ignore.
    }
    return { success: false, stdout: output, error: stderr };
  }
}

/**
 * Detect whether a merge operation resulted in a conflict.
 *
 * @param {object} result - Output from attemptMerge().
 * @returns {boolean} True if a conflict was detected.
 */
function isMergeConflict(result) {
  if (result.success) return false;
  const combined = (result.stdout || '') + (result.error || '');
  return (
    combined.includes('CONFLICT') ||
    combined.includes('Automatic merge failed') ||
    combined.includes('could not apply') ||
    combined.includes('merge conflict')
  );
}

// ---------------------------------------------------------------------------
// Conflict-handler: the function an agent would call when it detects a
// merge conflict during push/merge to dev.
// ---------------------------------------------------------------------------

/**
 * Create a merge-conflict work item via the `wl` CLI.
 *
 * This is the helper that an agent would invoke when it detects a merge
 * conflict. It creates a work item with a standardised title, description,
 * reproduction steps, and acceptance criteria.
 *
 * @param {object} opts
 * @param {string} opts.featureBranch - The feature branch that conflicted.
 * @param {string} opts.targetBranch - The target branch (e.g. dev).
 * @param {string} opts.conflictingFile - File path where the conflict occurred.
 * @param {string} opts.mergeOutput - Output from the failed merge attempt.
 * @param {string} [opts.tag] - Tag for test isolation.
 * @returns {Promise<object>} The created work item.
 */
export async function createMergeConflictWorkItem({
  featureBranch,
  targetBranch,
  conflictingFile,
  mergeOutput,
  tag,
}) {
  const tags = tag ? `merge-conflict,${tag}` : 'merge-conflict';
  const title = `Merge conflict: ${featureBranch}`;

  const description = `## Summary

A merge conflict was detected when attempting to integrate \`${featureBranch}\` into \`${targetBranch}\`.

## Reproduction Steps

1. Check out the \`${targetBranch}\` branch.
2. Attempt to merge \`${featureBranch}\`:
   \`\`\`bash
   git checkout ${targetBranch}
   git merge --no-edit ${featureBranch}
   \`\`\`
3. The merge fails with a conflict in \`${conflictingFile}\`.

## Conflicting Branches

- Feature branch: \`${featureBranch}\`
- Target branch: \`${targetBranch}\`
- Conflicting file: \`${conflictingFile}\`

## Merge Output

\`\`\`
${mergeOutput || '(no output captured)'}
\`\`\`

## Acceptance Criteria

- Resolve the conflict in \`${conflictingFile}\` between \`${featureBranch}\` and \`${targetBranch}\`.
- Ensure the merged result passes all tests on \`${targetBranch}\`.
- Push the resolved merge to \`${targetBranch}\`.
- Close this work item once the merge is successful.
`;

  return createWorkItem({ title, description, priority: 'high', tags });
}

// ---------------------------------------------------------------------------
// Tests.
// ---------------------------------------------------------------------------

describe('Merge conflict detection', () => {
  test('attemptMerge returns success: false for conflicting branches', () => {
    const { repoPath, featureBranch, targetBranch } = createConflictRepo();
    try {
      const result = attemptMerge(repoPath, targetBranch, featureBranch);
      assert.equal(result.success, false, 'merge should fail due to conflict');
      assert.ok(isMergeConflict(result), 'should detect merge conflict');
    } finally {
      fs.rmSync(repoPath, { recursive: true, force: true });
    }
  });

  test('attemptMerge returns success: true for non-conflicting branches', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'no-conflict-test-'));
    try {
      git(tmpDir, ['init']);
      git(tmpDir, ['branch', '-m', 'main']);
      git(tmpDir, ['config', 'user.email', 'test@example.com']);
      git(tmpDir, ['config', 'user.name', 'Test Agent']);

      // Initial commit.
      fs.writeFileSync(path.join(tmpDir, 'a.txt'), 'hello\n');
      git(tmpDir, ['add', '.']);
      git(tmpDir, ['commit', '-m', 'init']);

      // Create dev with a different file.
      git(tmpDir, ['branch', 'dev']);
      git(tmpDir, ['checkout', 'dev']);
      fs.writeFileSync(path.join(tmpDir, 'b.txt'), 'world\n');
      git(tmpDir, ['add', '.']);
      git(tmpDir, ['commit', '-m', 'dev: add b.txt']);

      // Feature branch from main with yet another different file.
      git(tmpDir, ['checkout', '-b', 'feature/no-conflict', 'main']);
      fs.writeFileSync(path.join(tmpDir, 'c.txt'), 'foo\n');
      git(tmpDir, ['add', '.']);
      git(tmpDir, ['commit', '-m', 'feature: add c.txt']);

      const result = attemptMerge(tmpDir, 'dev', 'feature/no-conflict');
      assert.equal(result.success, true, 'merge should succeed without conflicts');
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('isMergeConflict correctly identifies conflict output', () => {
    assert.ok(
      isMergeConflict({
        success: false,
        stdout: '',
        error: 'CONFLICT (content): Merge conflict in shared.txt',
      }),
    );
    assert.ok(
      isMergeConflict({
        success: false,
        stdout: 'Automatic merge failed; fix conflicts and then commit the result.',
        error: '',
      }),
    );
    assert.equal(
      isMergeConflict({
        success: false,
        stdout: '',
        error: 'Some unrelated error',
      }),
      false,
    );
    assert.equal(
      isMergeConflict({ success: true, stdout: 'Already up to date.', error: null }),
      false,
    );
  });
});

describe('Merge conflict work item creation', () => {
  test('createMergeConflictWorkItem creates a properly formatted work item', async () => {
    const tag = uniqueTestTag('conflict-test');

    const result = await createMergeConflictWorkItem({
      featureBranch: 'feature/wl-42-test-change',
      targetBranch: 'dev',
      conflictingFile: 'shared.txt',
      mergeOutput: 'CONFLICT (content): Merge conflict in shared.txt',
      tag,
    });

    assert.ok(result.id, 'work item should have an id');

    // Retrieve the full work item to verify its content.
    const item = await getWorkItem(result.id);

    // Verify title format.
    assert.ok(
      item.title.includes('Merge conflict:'),
      `title should include "Merge conflict:", got: ${item.title}`,
    );
    assert.ok(
      item.title.includes('feature/wl-42-test-change'),
      `title should include the feature branch name, got: ${item.title}`,
    );

    // Verify description contains acceptance criteria section.
    assert.ok(
      item.description.includes('## Acceptance Criteria'),
      'description should contain Acceptance Criteria section',
    );

    // Verify description contains both branch names.
    assert.ok(
      item.description.includes('feature/wl-42-test-change'),
      'description should include the feature branch',
    );
    assert.ok(
      item.description.includes('dev'),
      'description should include the target branch',
    );

    // Verify description contains the conflicting file.
    assert.ok(
      item.description.includes('shared.txt'),
      'description should include the conflicting file',
    );

    // Verify tags.
    assert.ok(
      item.tags && item.tags.includes('merge-conflict'),
      'work item should be tagged merge-conflict',
    );
    assert.ok(
      item.tags && item.tags.some((t) => t.startsWith('conflict-test-')),
      'work item should include the test isolation tag',
    );
  });

  test('created work item can be found via tag lookup', async () => {
    const tag = uniqueTestTag('conflict-tag-lookup');

    const result = await createMergeConflictWorkItem({
      featureBranch: 'feature/wl-99-tag-lookup',
      targetBranch: 'dev',
      conflictingFile: 'config.yml',
      mergeOutput: 'CONFLICT (content): Merge conflict in config.yml',
      tag,
    });

    // Look up by the unique test tag.
    const found = await listByTag(tag);
    const items = Array.isArray(found) ? found : found.workItems ?? found.items ?? [];

    const match = items.find((item) => item.id === result.id);
    assert.ok(match, 'listByTag should find the created work item');
  });

  test('end-to-end: conflict detection triggers work item creation', async () => {
    // Full end-to-end flow: create a conflict repo, detect the conflict,
    // create a work item, and verify the work item.
    const { repoPath, featureBranch, targetBranch, conflictingFile } =
      createConflictRepo();

    try {
      // Step 1: Attempt merge (will fail).
      const mergeResult = attemptMerge(repoPath, targetBranch, featureBranch);
      assert.equal(mergeResult.success, false, 'merge should fail');
      assert.ok(isMergeConflict(mergeResult), 'should detect conflict');

      // Step 2: Create merge-conflict work item (as agent would).
      const tag = uniqueTestTag('conflict-e2e');
      const mergeOutput = (mergeResult.error || '') + (mergeResult.stdout || '');
      const workItem = await createMergeConflictWorkItem({
        featureBranch,
        targetBranch,
        conflictingFile,
        mergeOutput,
        tag,
      });

      // Step 3: Verify the work item.
      const item = await getWorkItem(workItem.id);

      assert.ok(
        item.title.includes('Merge conflict:'),
        `title should indicate merge conflict, got: ${item.title}`,
      );
      assert.ok(
        item.title.includes(featureBranch),
        `title should include feature branch "${featureBranch}", got: ${item.title}`,
      );

      assert.ok(
        item.description.includes('## Acceptance Criteria'),
        'description must contain Acceptance Criteria heading',
      );

      assert.ok(
        item.description.includes(featureBranch),
        'description must reference the feature branch',
      );
      assert.ok(
        item.description.includes(targetBranch),
        'description must reference the target branch',
      );
      assert.ok(
        item.description.includes(conflictingFile),
        'description must reference the conflicting file',
      );
      assert.ok(
        item.description.includes('Reproduction'),
        'description must include reproduction steps',
      );
    } finally {
      fs.rmSync(repoPath, { recursive: true, force: true });
    }
  });
});
