/**
 * git-sim — lightweight helpers for creating local, isolated git repositories
 * that simulate agent push behaviour in integration tests.
 *
 * Usage:
 *
 *   import { createSimRepo, createBareRemote } from './git-sim.js';
 *
 *   const remote = createBareRemote();
 *   const repo = createSimRepo(remote.url);
 *   // … run git operations via repo.exec() …
 *   repo.cleanup();
 *   remote.cleanup();
 */
import { execSync } from 'node:child_process';
import { mkdtempSync, rmSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// ---------------------------------------------------------------------------
// Internal: run a command, return { stdout, stderr, exitCode }
// ---------------------------------------------------------------------------
function run(cmd, opts = {}) {
  try {
    const stdout = execSync(cmd, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      ...opts,
    });
    return { stdout, stderr: '', exitCode: 0 };
  } catch (err) {
    return {
      stdout: err.stdout ?? '',
      stderr: err.stderr ?? '',
      exitCode: err.status ?? 1,
    };
  }
}

// ---------------------------------------------------------------------------
// createBareRemote — create a temporary bare repository to act as "origin"
// ---------------------------------------------------------------------------
export function createBareRemote() {
  const dir = mkdtempSync(join(tmpdir(), 'git-sim-remote-'));
  run(`git init --bare "${dir}"`);

  return {
    url: dir,
    /** Return the list of branches (one per line) on the remote. */
    branches() {
      const r = run(`git --git-dir="${dir}" branch`);
      return r.stdout
        .split('\n')
        .map((b) => b.replace(/^\*\s+/, '').trim())
        .filter(Boolean);
    },
    /** Return the list of commits on a given branch (newest first). */
    log(branch = 'main') {
      const r = run(`git --git-dir="${dir}" log --oneline "${branch}"`);
      return r.stdout
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean);
    },
    /** Check whether a specific branch exists on the remote. */
    hasBranch(name) {
      return this.branches().includes(name);
    },
    /** Clean up the temporary directory. */
    cleanup() {
      if (existsSync(dir)) rmSync(dir, { recursive: true, force: true });
    },
  };
}

// ---------------------------------------------------------------------------
// createSimRepo — create a temporary working repository cloned from a remote
// ---------------------------------------------------------------------------
export function createSimRepo(remoteUrl, opts = {}) {
  const { initialBranch = 'main' } = opts;
  const dir = mkdtempSync(join(tmpdir(), 'git-sim-repo-'));

  run(`git clone "${remoteUrl}" "${dir}"`);

  return {
    dir,
    /** Execute a git command inside the working repo. */
    exec(cmd) {
      return run(`git -C "${dir}" ${cmd}`);
    },
    /** Configure author identity (required for commits). */
    configureIdentity(name = 'Test Agent', email = 'agent@test.local') {
      this.exec(`config user.name "${name}"`);
      this.exec(`config user.email "${email}"`);
    },
    /** Create a file and commit it. */
    commitFile(path, content, message) {
      const fullPath = join(dir, path);
      const parentDir = fullPath.substring(0, fullPath.lastIndexOf('/'));
      if (parentDir && !existsSync(parentDir)) {
        mkdirSync(parentDir, { recursive: true });
      }
      writeFileSync(fullPath, content);
      this.exec(`add "${path}"`);
      return this.exec(`commit -m "${message}"`);
    },
    /** Create a branch (optionally check it out). */
    createBranch(name, checkout = true) {
      if (checkout) {
        return this.exec(`checkout -b "${name}"`);
      }
      return this.exec(`branch "${name}"`);
    },
    /** Push current HEAD to a remote branch. */
    push(remoteBranch, opts = {}) {
      const { force = false, remoteName = 'origin' } = opts;
      const flag = force ? '--force' : '';
      return this.exec(`push ${flag} ${remoteName} HEAD:refs/heads/${remoteBranch}`);
    },
    /** Fetch from the remote. */
    fetch() {
      return this.exec('fetch origin');
    },
    /** Return the list of local branches. */
    branches() {
      const r = this.exec('branch');
      return r.stdout
        .split('\n')
        .map((b) => b.replace(/^\*\s+/, '').trim())
        .filter(Boolean);
    },
    /** Return the current branch name. */
    currentBranch() {
      const r = this.exec('rev-parse --abbrev-ref HEAD');
      return r.stdout.trim();
    },
    /** Clean up the temporary directory. */
    cleanup() {
      if (existsSync(dir)) rmSync(dir, { recursive: true, force: true });
    },
  };
}
