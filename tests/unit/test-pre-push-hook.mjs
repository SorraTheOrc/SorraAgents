/**
 * Unit tests for .githooks/pre-push branch policy enforcement.
 *
 * Validates that the pre-push hook correctly blocks pushes to protected
 * branches (main, master, HEAD) and allows pushes to feature branches.
 */

import { test, describe, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
// Hermetic git environment: a fixture git command must never stage into an
// inherited GIT_INDEX_FILE (SA-0MW0V9PEP004EJYV / SA-0MVYS34QF000AWQX).
import { hermeticGitEnv } from '../helpers/hermeticGitEnv.mjs';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const REPO_ROOT = path.resolve(import.meta.dirname, '../..');
const HOOK_PATH = path.join(REPO_ROOT, '.githooks', 'pre-push');

/** Run git with the repository-override vars (incl. GIT_INDEX_FILE) scrubbed. */
function execGit(cmd, opts = {}) {
  return execSync(cmd, { ...opts, env: hermeticGitEnv(opts.env) });
}

/**
 * Run the pre-push hook script with a simulated git branch context.
 * Returns { exitCode, stdout, stderr }.
 */
function runPrePushHook(envOverrides = {}) {
  // Create a temp directory with a fake git repo to test the hook
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-test-'));
  try {
    // Initialize a minimal git repo
    execGit('git init', { cwd: tmpDir, stdio: 'pipe' });

    // Copy the hook into the temp repo's .git/hooks
    const hooksDir = path.join(tmpDir, '.git', 'hooks');
    fs.mkdirSync(hooksDir, { recursive: true });
    const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
    const targetHook = path.join(hooksDir, 'pre-push');
    fs.writeFileSync(targetHook, hookContent, { mode: 0o755 });

    // Configure the repo to use hooks
    execGit('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });

    // Create an initial commit so we have a valid repo state
    execGit('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
    fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
    execGit('git add .', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

    // Add a fake remote so push doesn't fail on network
    const fakeRemote = path.join(tmpDir, 'fake-remote.git');
    execGit(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
    execGit('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

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
      execGit('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
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
      execGit('git init', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git checkout -b master', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execGit('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execGit('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execGit(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execGit('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
        BRANCH_POLICY_SKIP: '',
      };
      try {
        execGit('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
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
      execGit('git init', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git checkout -b wl-SA-001-test-feature', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execGit('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execGit('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execGit(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execGit('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
      };
      // This should succeed (exit 0)
      execGit('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
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
      execGit('git init', { cwd: tmpDir, stdio: 'pipe' });
      const hooksDir = path.join(tmpDir, '.git', 'hooks');
      fs.mkdirSync(hooksDir, { recursive: true });
      const hookContent = fs.readFileSync(HOOK_PATH, 'utf8');
      fs.writeFileSync(path.join(hooksDir, 'pre-push'), hookContent, { mode: 0o755 });
      execGit('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
      fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
      execGit('git add .', { cwd: tmpDir, stdio: 'pipe' });
      execGit('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

      const fakeRemote = path.join(tmpDir, 'fake-remote.git');
      execGit(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
      execGit('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

      const env = {
        ...process.env,
        WORKLOG_SKIP_PRE_PUSH: '1',
        BRANCH_POLICY_SKIP: '1',
        // The temp repo has no skill/test/scripts/run_tests.py, and its
        // default branch is 'main' on machines with init.defaultBranch=main
        // — the hook's full-suite gate would then fail with "runner not
        // found". Skip the test/context gates so this test isolates the
        // branch-policy bypass it actually exercises.
        TEST_SCOPE_SKIP: '1',
        CONTEXT_BUDGET_SKIP: '1',
      };
      // With all skips enabled, push should succeed
      execGit('git push -u origin HEAD 2>&1', { cwd: tmpDir, env, stdio: 'pipe' });
      assert.ok(true, 'Push with BRANCH_POLICY_SKIP=1 should succeed');
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

// ── Tree-keyed gate provenance (SA-0MW7C6BMZ00931JG) ──────────────────────

/**
 * Run the pre-push hook in a temp repo pushing HEAD to `dev`, with a fake
 * runner that writes `runnerStderr` and exits with `runnerExit`.
 *
 * Returns `{ exitCode, output }` where `output` is git's combined stdout +
 * stderr (the hook's provenance echoes are forwarded to the push's stderr).
 */
function runDevGateHook({ runnerStderr, runnerExit = 0 }) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pre-push-gate-'));
  try {
    execGit('git init', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git checkout -b feature', { cwd: tmpDir, stdio: 'pipe' });
    const hooksDir = path.join(tmpDir, '.git', 'hooks');
    fs.mkdirSync(hooksDir, { recursive: true });
    fs.writeFileSync(path.join(hooksDir, 'pre-push'), fs.readFileSync(HOOK_PATH, 'utf8'), {
      mode: 0o755,
    });
    execGit('git config core.hooksPath .git/hooks', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git config user.email "test@test.com"', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git config user.name "Test"', { cwd: tmpDir, stdio: 'pipe' });
    fs.writeFileSync(path.join(tmpDir, 'README.md'), '# test');
    execGit('git add .', { cwd: tmpDir, stdio: 'pipe' });
    execGit('git commit -m "init"', { cwd: tmpDir, stdio: 'pipe' });

    const fakeRemote = path.join(tmpDir, 'fake-remote.git');
    execGit(`git init --bare ${fakeRemote}`, { cwd: tmpDir, stdio: 'pipe' });
    execGit('git remote add origin ' + fakeRemote, { cwd: tmpDir, stdio: 'pipe' });

    // Fake runner: emit the provenance lines the real runner writes, then the
    // JSON the hook may classify from. The gate only greps stderr for the
    // gate-* lines when it succeeds.
    const runner = path.join(tmpDir, 'skill', 'test', 'scripts', 'run_tests.py');
    fs.mkdirSync(path.dirname(runner), { recursive: true });
    fs.writeFileSync(
      runner,
      'import sys\n' +
        `sys.stderr.write(${JSON.stringify(runnerStderr)})\n` +
        'sys.stdout.write("{}\\n")\n' +
        `sys.exit(${runnerExit})\n`,
      'utf8'
    );

    const env = {
      ...process.env,
      WORKLOG_SKIP_PRE_PUSH: '1',
      CONTEXT_BUDGET_SKIP: '1',
      BRANCH_POLICY_SKIP: '',
    };
    try {
      const out = execGit('git push origin HEAD:refs/heads/dev 2>&1', {
        cwd: tmpDir,
        env,
        stdio: 'pipe',
      }).toString();
      return { exitCode: 0, output: out };
    } catch (err) {
      return {
        exitCode: err.status ?? 1,
        output: (err.stdout?.toString() || '') + (err.stderr?.toString() || ''),
      };
    }
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}

describe('pre-push hook: tree-keyed gate provenance', () => {
  test('echoes tree-keyed provenance and cache hit/age to the pusher', () => {
    const lines =
      'run_tests: gate-subject pushed=abc123 checkout=in-place ' +
      'fingerprint_kind=tree tree=ttt\n' +
      'run_tests: gate-root /tmp/gate\n' +
      'run_tests: gate-verdict verdict=cached sha=abc123 tree=ttt ' +
      'fingerprint_kind=tree cache_hit=true cache_age_seconds=42.0 entries=1\n';
    const result = runDevGateHook({ runnerStderr: lines });
    assert.equal(result.exitCode, 0, result.output);
    assert.ok(
      result.output.includes('run_tests: gate-subject'),
      `expected gate-subject provenance, got: ${result.output}`
    );
    assert.ok(result.output.includes('fingerprint_kind=tree'), result.output);
    assert.ok(result.output.includes('tree=ttt'), result.output);
    assert.ok(result.output.includes('cache_hit=true'), result.output);
    assert.ok(
      result.output.includes('cache_age_seconds=42.0'),
      `expected the reused verdict's age, got: ${result.output}`
    );
  });

  test('surfaces a gate refusal and blocks the push', () => {
    const result = runDevGateHook({
      runnerStderr:
        'run_tests: gate-refused pushed commit abc123 is not present in this ' +
        'repository. Set TEST_SCOPE_SKIP=1 to bypass (not recommended).\n',
      runnerExit: 2,
    });
    assert.notEqual(result.exitCode, 0, result.output);
    assert.ok(result.output.includes('run_tests: gate-refused'), result.output);
    assert.ok(result.output.includes('TEST_SCOPE_SKIP=1'), result.output);
  });

  test('invokes the runner with --gate-commit (pushed-commit guard intact)', () => {
    const content = fs.readFileSync(HOOK_PATH, 'utf8');
    assert.ok(content.includes('--gate-commit'), 'hook must forward --gate-commit');
    assert.ok(
      content.includes("grep '^run_tests: gate-'"),
      'hook must echo the runner gate-* provenance lines'
    );
    assert.ok(
      content.includes('mirror_suite_cache'),
      'hook must mirror the served entry into the main checkout cache'
    );
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

  test('hook file contains context-budget gate (F6)', () => {
    const content = fs.readFileSync(HOOK_PATH, 'utf8');
    assert.ok(
      content.includes('Context-budget regression gate'),
      'Hook should include the context-budget regression gate'
    );
    assert.ok(
      content.includes('measure_context.py'),
      'Hook should invoke measure_context.py'
    );
    assert.ok(
      content.includes('context-budget.thresholds.json'),
      'Hook should reference the thresholds file'
    );
    assert.ok(
      content.includes('CONTEXT_BUDGET_SKIP'),
      'Hook should support CONTEXT_BUDGET_SKIP bypass'
    );
  });
});
