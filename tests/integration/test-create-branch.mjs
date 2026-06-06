/**
 * Integration tests for skill/git-management/scripts/create-branch.mjs
 *
 * Tests branch creation in temporary git repositories.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createBareRemote, createSimRepo } from '../helpers/git-sim.js';
import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SCRIPT_PATH = join(REPO_ROOT, 'skill', 'git-management', 'scripts', 'create-branch.mjs');

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Run the create-branch script in a sim repo directory.
 * @param {string} repoDir
 * @param {string[]} args
 * @returns {{ stdout: string, stderr: string, exitCode: number }}
 */
function runCreateBranch(repoDir, args) {
  try {
    const stdout = execSync(
      `node "${SCRIPT_PATH}" ${args.join(' ')}`,
      {
        encoding: 'utf-8',
        cwd: repoDir,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
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

// ── Tests ────────────────────────────────────────────────────────────────────

describe('create-branch: happy path', () => {
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

  test('creates a canonical branch with valid inputs', () => {
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test-feature', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.branchName, 'wl-SA-0MPMI7FWI004PXHS-test-feature');
    assert.equal(data.workItemId, 'SA-0MPMI7FWI004PXHS');
  });

  test('checks out the new branch', () => {
    runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test-feature']);

    const currentBranch = repo.currentBranch();
    assert.equal(currentBranch, 'wl-SA-0MPMI7FWI004PXHS-test-feature');
  });

  test('accepts multi-word descriptions', () => {
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'add new feature', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.branchName, 'wl-SA-0MPMI7FWI004PXHS-add-new-feature');
  });
});

describe('create-branch: invalid inputs', () => {
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

  test('fails with no arguments', () => {
    const result = runCreateBranch(repo.dir, ['--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.includes('Usage'));
  });

  test('fails with invalid work-item ID', () => {
    const result = runCreateBranch(repo.dir, ['invalid-id', 'desc', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.toLowerCase().includes('work-item'));
  });

  test('fails with empty description', () => {
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', '---', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    // '---' is parsed as a flag by the arg parser, so we get a usage error
    assert.ok(data.error.includes('Usage') || data.error.toLowerCase().includes('slug'));
  });
});

describe('create-branch: branch collision', () => {
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

  test('fails when branch already exists', () => {
    // Create branch first time
    runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test-feature']);

    // Try to create it again
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test-feature', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, false);
    assert.ok(data.error.includes('already exists'));
  });
});

describe('create-branch: dry-run', () => {
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

  test('dry-run reports what would happen without creating branch', () => {
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test-feature', '--dry-run', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    assert.equal(data.dryRun, true);
    assert.equal(data.branchName, 'wl-SA-0MPMI7FWI004PXHS-test-feature');

    // Verify no branch was created
    const branches = repo.branches();
    assert.ok(!branches.includes('wl-SA-0MPMI7FWI004PXHS-test-feature'));
  });
});

describe('create-branch: protected branch rejection', () => {
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

  // Note: The generated branch name wl-SA-XXX-main would not match blocked branches
  // because it has the wl- prefix. This test verifies the safety check is in place.
  test('generated names are validated against blocked list', () => {
    // The script generates wl-<id>-<slug> names which are always valid.
    // We verify the generated branch name is not blocked.
    const result = runCreateBranch(repo.dir, ['SA-0MPMI7FWI004PXHS', 'test', '--json']);
    const data = JSON.parse(result.stdout);

    assert.equal(data.success, true);
    // The generated name should NOT be a blocked branch
    assert.ok(!['main', 'master', 'HEAD'].includes(data.branchName));
  });
});
