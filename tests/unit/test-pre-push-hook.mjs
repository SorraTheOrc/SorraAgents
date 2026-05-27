/**
 * Unit tests for .githooks/pre-push branch policy enforcement.
 *
 * Validates that the pre-push hook correctly blocks pushes to protected
 * branches (main, master, HEAD) and allows pushes to feature branches.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const REPO_ROOT = path.resolve(import.meta.dirname, '../..');
const HOOK_PATH = path.join(REPO_ROOT, '.githooks', 'pre-push');

/**
 * Run the pre-push hook script with a simulated git branch context.
 * Returns { exitCode, stdout, stderr }.
 */
function runPrePushHook(envOverrides = {}) {
  // Create a temp directory with a fake git repo to test the hook
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-test-'));
  try {
    // Initialize a minimal git repo
    execSync('git init', { cwd: tmpDir, stdio: 'pipe' });

    // Copy the hook into the temp repo's .git/hooks
    const hooksDir = path.join(tmpDir, '.git', 'hooks');
    fs.mkdirSync(hooksDir, { recursive: true });
    const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
    const targetHook = path.join(hooksDir, 'pre-push');
    fs.writeFileSync(targetHook, hookContent, { mode: 0o755 });

    // Configure the repo to use hooks
    execSync('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });

    // Create an initial commit so we have a valid repo state
    execSync('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
    execSync('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
    fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
    execSync('git add .', { cwd: tmpDir, stdio: 'pipe' });
    execSync('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

    // Add a fake remote so push doesn't fail on network
    const fakeRemote = path.join(tmpDir, 'fake-remote.git');
    execSync(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
    execSync('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

    // Build the environment
    const baseEnv = {
      ...process.env,
      WORKLOG_SKIP_PRE_PUSH: '1',
      PATH: process.env.PATH,
      HOME: process.env.HOME,
    };
    const env = { ...baseEnv, ...envOverrides };

    // Attempt push (the hook runs before the push)
    try {
      execSync('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
      return { exitCode: 0, stdout: '', stderr: '' };
    } catch (err) {
      return {
        exitCode: err.status,
        stdout: err.stdout?.toString() || '',
        stderr: err.stderr?.toString() || '',
      };
    }
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

// ── Pre-push hook: branch policy enforcement ────────────────────────────────

describe('pre-push hook: branch policy', () => {
  test('blocks push when on main branch', () => {
    const result = runPrePushHook({ BRANCH_POLICY_SKIP: '' });
    // The hook should reject because we're on main
    assert.notEqual(result.exitCode, 0, 'Push to main should be blocked');
    assert.ok(
      result.stderr.includes('blocked by branch policy') ||
        result.stdout.includes('blocked by branch policy'),
      `Expected error message about branch policy, got: ${result.stderr || result.stdout}`
    );
  });

  test('blocks push when on master branch', () => {
    const result = runPrePushHook({ BRANCH_POLICY_SKIP: '' });
    // We're on 'main' by default, but test master explicitly
    // by checking the hook logic directly
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-master-'));
    try {
      execSync('git init', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git checkout -b master', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execSync('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execSync('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execSync(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execSync('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
        BRANCH_POLICY_SKIP: '',
      };
      try {
        execSync('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
        assert.fail('Push to master should be blocked');
      } catch (err) {
        const output = err.stderr?.toString() || err.stdout?.toString() || '';
        assert.ok(
          output.includes('blocked by branch policy'),
          `Expected error about branch policy, got: ${output}`
        );
      }
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('allows push from a feature branch', () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-feature-'));
    try {
      execSync('git init', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git checkout -b wl-SA-001-test-feature', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execSync('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execSync('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execSync(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execSync('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
      };
      // This should succeed (exit 0)
      execSync('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
      // If we get here, the push succeeded
      assert.ok(true, 'Push from feature branch should succeed');
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test('BRANCH_POLICY_SKIP=1 bypasses the policy check', () => {
    // Even on main, setting BRANCH_POLICY_SKIP=1 should allow the push
    // (though it may fail on wl sync, that's OK — we only care the policy is skipped)
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-bypass-'));
    try {
      execSync('git init', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execSync('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execSync('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execSync('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execSync(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execSync('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
        BRANCH_POLICY_SKIP: '1',
      };
      // With both skips enabled, push should succeed
      execSync('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
      assert.ok(true, 'Push with BRANCH_POLICY_SKIP=1 should succeed');
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

// ── Hook file integrity ─────────────────────────────────────────────────────

describe('pre-push hook file', () => {
  test('hook file exists and is executable', () => {
    assert.ok(fs.existsSync(HOOK_PATH), `Hook file should exist at ${HOOK_PATH}`);
    const stat = fs.statSync(HOOK_PATH);
    assert.ok(stat.mode & 0o111, 'Hook file should be executable');
  });

  test('hook file contains branch policy check', () => {
    const content = fs.readFileSync(HOOK_PATH, 'utf8');
    assert.ok(content.includes('blocked_branches'), 'Hook should define blocked_branches');
    assert.ok(content.includes('main'), 'Hook should block main');
    assert.ok(content.includes('master'), 'Hook should block master');
    assert.ok(content.includes('HEAD'), 'Hook should block HEAD');
    assert.ok(
      content.includes('blocked by branch policy'),
      'Hook should output policy error message'
    );
  });

  test('hook file references BRANCH_POLICY_SKIP env var', () => {
    const content = fs.readFileSync(HOOK_PATH, 'utf8');
    assert.ok(
      content.includes('BRANCH_POLICY_SKIP'),
      'Hook should support BRANCH_POLICY_SKIP bypass'
    );
  });
});
