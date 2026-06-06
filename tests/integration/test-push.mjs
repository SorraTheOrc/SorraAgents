/**
 * Integration tests for skill/git-management/scripts/push.mjs
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createBareRemote, createSimRepo } from '../helpers/git-sim.js';
import { execSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SCRIPT_PATH = join(REPO_ROOT, 'skill', 'git-management', 'scripts', 'push.mjs');

function runPush(repoDir, args) {
  try {
    const stdout = execSync(
      `node "${SCRIPT_PATH}" ${args.join(' ')}`,
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

describe('push: happy path', () => {
  let remote, repo;

  beforeEach(() => {
    remote = createBareRemote();
    repo = createSimRepo(remote.url);
    repo.configureIdentity('Test Agent', 'agent@test.local');
    repo.commitFile('README.md', '# Test', 'Initial commit');
    // Create a canonical branch
    repo.createBranch('wl-SA-0MPMI7FWI004PXHS-test-feature');
  });

  afterEach(() => {
    repo?.cleanup();
    remote?.cleanup();
  });

  test('pushes feature branch to origin', () => {
    const result = runPush(repo.dir, ['--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.ok(data.command);
  });

  test('pushes into dev with --into-dev', () => {
    const result = runPush(repo.dir, ['--into-dev', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.ok(data.command.includes('dev'));
  });
});

describe('push: safety checks', () => {
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

  test('fails from non-canonical branch', () => {
    // Stay on master (non-canonical branch name)
    const result = runPush(repo.dir, ['--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.toLowerCase().includes('not a valid') || data.error.toLowerCase().includes('branch'));
  });

  test('fails with missing remote', () => {
    repo.createBranch('wl-SA-0MPMI7FWI004PXHS-test-feature');
    // Remove the origin remote
    repo.exec('remote remove origin');

    const result = runPush(repo.dir, ['--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
  });
});

describe('push: dry-run', () => {
  let remote, repo;

  beforeEach(() => {
    remote = createBareRemote();
    repo = createSimRepo(remote.url);
    repo.configureIdentity('Test Agent', 'agent@test.local');
    repo.commitFile('README.md', '# Test', 'Initial commit');
    repo.createBranch('wl-SA-0MPMI7FWI004PXHS-test-feature');
  });

  afterEach(() => {
    repo?.cleanup();
    remote?.cleanup();
  });

  test('dry-run reports command without pushing', () => {
    const result = runPush(repo.dir, ['--dry-run', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.dryRun, true);
    assert.ok(data.command);

    // Verify nothing was pushed
    assert.equal(remote.hasBranch('wl-SA-0MPMI7FWI004PXHS-test-feature'), false);
  });

  test('dry-run with --into-dev reports dev push', () => {
    const result = runPush(repo.dir, ['--into-dev', '--dry-run', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.dryRun, true);
    assert.ok(data.command.includes('dev'));
  });
});
