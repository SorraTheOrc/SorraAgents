/**
 * Integration tests for skill/git-management/scripts/commit.mjs
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createBareRemote, createSimRepo } from '../helpers/git-sim.js';
import { execSync } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SCRIPT_PATH = join(REPO_ROOT, 'skill', 'git-management', 'scripts', 'commit.mjs');

function runCommit(repoDir, args) {
  // Properly quote arguments that contain spaces
  const quotedArgs = args.map(arg => {
    if (arg.includes(' ') && !arg.startsWith('--')) {
      return JSON.stringify(arg);
    }
    return arg;
  });
  try {
    const stdout = execSync(
      `node "${SCRIPT_PATH}" ${quotedArgs.join(' ')}`,
      { encoding: 'utf-8', cwd: repoDir, stdio: ['pipe', 'pipe', 'pipe'] },
    );
    return { stdout, stderr: '', exitCode: 0 };
  } catch (err) {
    return {
      stdout: (err.stdout ?? '').toString(),
      stderr: (err.stderr ?? '').toString(),
      exitCode: err.status ?? 1,
    };
  }
}

describe('commit: happy path', () => {
  let remote, repo;

  beforeEach(() => {
    remote = createBareRemote();
    repo = createSimRepo(remote.url);
    repo.configureIdentity('Test Agent', 'agent@test.local');
    repo.commitFile('README.md', '# Test', 'Initial commit');
  });

  afterEach(() => {
    repo?.cleanup();
    remote?.cleanup();
  });

  test('commits with conventional message and work-item ref', () => {
    // Create a file without committing (staged changes for the commit script)
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, [
      '--message', 'feat: add feature', '--work-item', 'SA-0MPMI7FWI004PXHS', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.ok(data.commitMessage.includes('SA-0MPMI7FWI004PXHS'));
    assert.ok(data.commitHash);
  });

  test('auto-wraps non-conventional message as chore', () => {
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, [
      '--message', 'add some stuff', '--work-item', 'SA-0MPMI7FWI004PXHS', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.ok(data.commitMessage.startsWith('chore:'));
    assert.ok(data.commitMessage.includes('SA-0MPMI7FWI004PXHS'));
  });

  test('does not duplicate work-item ref if already present', () => {
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, [
      '--message', 'feat: add feature (SA-0MPMI7FWI004PXHS)',
      '--work-item', 'SA-0MPMI7FWI004PXHS', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    // Should appear exactly once
    const count = (data.commitMessage.match(/SA-0MPMI7FWI004PXHS/g) || []).length;
    assert.equal(count, 1);
  });
});

describe('commit: validation', () => {
  let remote, repo;

  beforeEach(() => {
    remote = createBareRemote();
    repo = createSimRepo(remote.url);
    repo.configureIdentity('Test Agent', 'agent@test.local');
    repo.commitFile('README.md', '# Test', 'Initial commit');
  });

  afterEach(() => {
    repo?.cleanup();
    remote?.cleanup();
  });

  test('fails with missing work-item reference', () => {
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, ['--message', 'feat: something', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.toLowerCase().includes('work-item'));
  });

  test('fails with empty worktree and no --all', () => {
    const result = runCommit(repo.dir, [
      '--message', 'feat: nothing', '--work-item', 'SA-0MPMI7FWI004PXHS', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.toLowerCase().includes('staged') || data.error.toLowerCase().includes('no'));
  });

  test('fails with invalid work-item ID', () => {
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, [
      '--message', 'feat: something', '--work-item', 'bad-id', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
  });
});

describe('commit: dry-run', () => {
  let remote, repo;

  beforeEach(() => {
    remote = createBareRemote();
    repo = createSimRepo(remote.url);
    repo.configureIdentity('Test Agent', 'agent@test.local');
    repo.commitFile('README.md', '# Test', 'Initial commit');
  });

  afterEach(() => {
    repo?.cleanup();
    remote?.cleanup();
  });

  test('dry-run reports what would happen without committing', () => {
    writeFileSync(join(repo.dir, 'feature.js'), 'export const x = 1;');
    repo.exec('add feature.js');

    const result = runCommit(repo.dir, [
      '--message', 'feat: add feature', '--work-item', 'SA-0MPMI7FWI004PXHS',
      '--dry-run', '--json',
    ]);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.dryRun, true);
    assert.ok(data.commitMessage);

    // Verify no new commit was made (only the initial commit)
    const logResult = repo.exec('log --oneline');
    const commitCount = logResult.stdout.split('\n').filter(Boolean).length;
    assert.equal(commitCount, 1);
  });
});
