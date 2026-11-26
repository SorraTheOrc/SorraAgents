/**
 * Tests for the ship wrapper's release-script timeout default and its
 * timeout-path repository recovery (SA-0MWXIXV2R008MRT3).
 *
 * Background: `run-release.js` bounds the canonical merge script with
 * `SHIP_RELEASE_TIMEOUT_MS`. The script runs the full test suite on the merged
 * dev→main tree before bumping/tagging/pushing; on this repository Step 5 takes
 * ≈ 1 982 s (~33 min), so the previous 600 s default guaranteed a kill on every
 * release. The kill left the main checkout parked on an orphan
 * `release/dev-to-main-*` branch carrying a partial version bump and CHANGELOG
 * entry.
 *
 * These tests pin:
 *
 *  1. The default timeout is ≥ 60 min and still env-overridable.
 *  2. On a spawn timeout the wrapper restores the start branch, deletes this
 *     run's orphan `release/dev-to-main-*` branch (identified by a pre/post
 *     branch snapshot), and names both in the exit-10 message.
 *  3. An orphan that was pushed to `origin` or backs an open PR is **not**
 *     deleted and is named as left in place.
 *  4. A branch that existed before the run is never touched (snapshot, not
 *     glob).
 *  5. The normal (non-timeout) path is unchanged.
 *
 * The harness drives the REAL wrapper in a temp git repository with a fake
 * hanging merge script, a stub `gh`, and a no-I/O Discord notifier stub.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import {
  mkdirSync,
  writeFileSync,
  readFileSync,
  existsSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';

// Shared helpers: no-I/O notifier stub (never POST a real release) and a
// hermetic git environment (never stage into an inherited GIT_INDEX_FILE).
import { writeStubDiscordNotify } from '../helpers/release-notify-stub.mjs';
import { hermeticGitEnv } from '../helpers/hermeticGitEnv.mjs';
import { makeTempDir } from '../helpers/temp-dir.mjs';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SKILL_SCRIPTS = join(REPO_ROOT, 'skill', 'ship', 'scripts');
const RUN_RELEASE_SRC = join(SKILL_SCRIPTS, 'run-release.js');

// Modules run-release.js imports; copied verbatim into the temp skill layout.
// `discord-notify.js` is deliberately excluded — the stub is written instead.
const DEP_FILES = [
  'check-unmerged-branches.js',
  'check-audit-gate.js',
  'audit-freshness.js',
  'audit-remediation.js',
  'refresh-audits.js',
  'check-final-validation.js',
  'check-critical-items.js',
  'check-worklog-refs.js',
  'timing.js',
  'llm.js',
];

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Run git in a fixture repo with the repository-override vars scrubbed. */
function git(args, cwd) {
  return spawnSync('git', args, {
    cwd,
    encoding: 'utf-8',
    env: hermeticGitEnv(),
  });
}

function gitOk(args, cwd) {
  const res = git(args, cwd);
  if (res.status !== 0) {
    throw new Error(`git ${args.join(' ')} failed (${res.status}): ${res.stderr}`);
  }
  return res.stdout;
}

/**
 * Build a temp repository + skill layout and return the paths a test needs.
 *
 * @param {object} [opts]
 * @param {string} [opts.releaseScript] - Body of the fake
 *   `release/dev-to-main.sh` (defaults to a fast successful exit).
 * @param {string} [opts.ghPrList] - JSON emitted by `gh pr list` (default `[]`).
 * @param {boolean} [opts.preExistingReleaseBranch] - Create
 *   `release/dev-to-main-PREEXIST` before the run.
 * @returns {{ tmpDir: string, repo: string, runReleasePath: string, binDir: string }}
 */
function buildRepoLayout(opts = {}) {
  const tmpDir = makeTempDir('run-release-timeout-');
  const repo = join(tmpDir, 'repo');
  mkdirSync(repo, { recursive: true });

  // A real git repository whose start branch is `dev`.
  gitOk(['init', '-q', '-b', 'main'], repo);
  gitOk(['config', 'user.email', 'test@example.com'], repo);
  gitOk(['config', 'user.name', 'Test'], repo);
  writeFileSync(join(repo, 'README.md'), '# test repo\n');
  gitOk(['add', '.'], repo);
  gitOk(['commit', '-q', '-m', 'init'], repo);
  gitOk(['branch', 'dev'], repo);
  gitOk(['checkout', '-q', 'dev'], repo);

  // A local bare origin so `git ls-remote` and `git push` work hermetically.
  const origin = join(tmpDir, 'origin.git');
  gitOk(['init', '--bare', '-q', origin], tmpDir);
  gitOk(['remote', 'add', 'origin', origin], repo);
  gitOk(['push', '-q', '-u', 'origin', 'main', 'dev'], repo);

  // Snapshot-vs-glob coverage: this branch exists before the run.
  if (opts.preExistingReleaseBranch) {
    gitOk(['branch', 'release/dev-to-main-PREEXIST', 'dev'], repo);
    gitOk(['push', '-q', 'origin', 'release/dev-to-main-PREEXIST'], repo);
  }

  // Skill layout inside the repo (the wrapper resolves its release script
  // relative to its own location: <skill>/ship/scripts/release/...).
  const skillScriptDir = join(repo, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  for (const dep of DEP_FILES) {
    writeFileSync(join(skillScriptDir, dep), readFileSync(join(SKILL_SCRIPTS, dep), 'utf8'));
  }
  writeStubDiscordNotify(skillScriptDir);

  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(
    join(releaseDir, 'merge-dev-to-main.sh'),
    opts.releaseScript ?? '#!/usr/bin/env bash\nexit 0\n',
  );

  // Stub `gh` so the recovery's open-PR probe is deterministic and offline.
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });
  writeFileSync(
    join(binDir, 'gh'),
    `#!/usr/bin/env bash
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then
  printf '%s\\n' '${opts.ghPrList ?? '[]'}'
  exit 0
fi
exit 0
`,
    { mode: 0o755 },
  );

  return { tmpDir, repo, runReleasePath: join(skillScriptDir, 'run-release.js'), binDir };
}

/**
 * Run the wrapper in a built layout.
 *
 * @param {{ repo: string, runReleasePath: string, binDir: string }} layout
 * @param {string[]} args
 * @param {object} [extraEnv]
 */
function runWrapper(layout, args, extraEnv = {}) {
  return spawnSync(process.execPath, [layout.runReleasePath, ...args], {
    cwd: layout.repo,
    encoding: 'utf-8',
    timeout: 60_000,
    env: {
      ...hermeticGitEnv(),
      PATH: `${layout.binDir}:${process.env.PATH}`,
      ...extraEnv,
    },
  });
}

function branches(repo) {
  return gitOk(['branch', '--format=%(refname:short)'], repo)
    .split(/\r?\n/)
    .map((b) => b.trim())
    .filter((b) => b.length > 0);
}

function currentBranch(repo) {
  return gitOk(['rev-parse', '--abbrev-ref', 'HEAD'], repo).trim();
}

// ── 1. Default timeout: generous and env-overridable ────────────────────────

describe('release-script timeout: default and override', () => {
  test('default is ≥ 60 min and sized for the observed 1 982 s Step 5 runtime', async () => {
    const { resolveReleaseScriptTimeout, RELEASE_SCRIPT_TIMEOUT_MS } =
      await import(`${RUN_RELEASE_SRC}?t=${Date.now()}`);

    const def = resolveReleaseScriptTimeout({});
    assert.ok(
      def >= 3_600_000,
      `default release-script timeout must be ≥ 3 600 000 ms (60 min), got ${def}`,
    );
    assert.equal(def, 3_600_000, 'default should be exactly 60 min');
    assert.ok(
      def > 1_982_000,
      'default must comfortably exceed the observed 1 982 s merge-script runtime',
    );
    // The module-level constant resolves the same default when no env override
    // is set in the importing process.
    if (!process.env.SHIP_RELEASE_TIMEOUT_MS) {
      assert.equal(RELEASE_SCRIPT_TIMEOUT_MS, def);
    }
  });

  test('SHIP_RELEASE_TIMEOUT_MS still wins over the default', async () => {
    const { resolveReleaseScriptTimeout } =
      await import(`${RUN_RELEASE_SRC}?t=${Date.now() + 1}`);
    assert.equal(
      resolveReleaseScriptTimeout({ SHIP_RELEASE_TIMEOUT_MS: '12345' }),
      12_345,
      'an explicit env override must be honoured',
    );
  });

  test('a non-numeric or non-positive override falls back to the default', async () => {
    const { resolveReleaseScriptTimeout } =
      await import(`${RUN_RELEASE_SRC}?t=${Date.now() + 2}`);
    assert.equal(resolveReleaseScriptTimeout({ SHIP_RELEASE_TIMEOUT_MS: 'abc' }), 3_600_000);
    assert.equal(resolveReleaseScriptTimeout({ SHIP_RELEASE_TIMEOUT_MS: '0' }), 3_600_000);
    assert.equal(resolveReleaseScriptTimeout({ SHIP_RELEASE_TIMEOUT_MS: '-5' }), 3_600_000);
  });
});

// ── 2. Timeout recovery: restore start branch + delete unpushed orphan ──────

describe('release-script timeout: recovery', () => {
  test('restores the start branch, deletes the orphan, names both, clears marker', () => {
    const layout = buildRepoLayout({
      // Creates the release branch the merge script would create, then hangs.
      releaseScript: [
        '#!/usr/bin/env bash',
        'git checkout -b release/dev-to-main-TEST origin/main',
        'exec sleep 30',
        '',
      ].join('\n'),
    });

    const res = runWrapper(layout, ['--skip-checks'], {
      SHIP_RELEASE_TIMEOUT_MS: '2000',
    });

    assert.equal(
      res.status,
      10,
      `expected exit 10 (timeout), got ${res.status}; stderr: ${res.stderr}`,
    );

    // Checkout restored to the recorded start branch.
    assert.equal(currentBranch(layout.repo), 'dev', 'checkout must be restored to dev');

    // This run's orphan branch is gone.
    const remaining = branches(layout.repo);
    assert.ok(
      !remaining.includes('release/dev-to-main-TEST'),
      `orphan release branch must be deleted; remaining: ${remaining.join(', ')}`,
    );

    // Message names what it restored and what it deleted.
    assert.match(res.stderr, /restored to the "dev" branch/i);
    assert.match(res.stderr, /deleted: .*release\/dev-to-main-TEST/);

    // Code Freeze marker is still cleared on the timeout path.
    assert.ok(
      !existsSync(join(layout.repo, '.worklog', 'code-freeze.json')),
      'Code Freeze marker must be cleared after a timed-out release',
    );
  });

  test('a release branch that existed before the run is never touched', () => {
    const layout = buildRepoLayout({
      preExistingReleaseBranch: true,
      releaseScript: [
        '#!/usr/bin/env bash',
        'git checkout -b release/dev-to-main-TEST origin/main',
        'exec sleep 30',
        '',
      ].join('\n'),
    });

    const res = runWrapper(layout, ['--skip-checks'], {
      SHIP_RELEASE_TIMEOUT_MS: '2000',
    });

    assert.equal(res.status, 10, `expected exit 10; stderr: ${res.stderr}`);
    const remaining = branches(layout.repo);
    assert.ok(
      remaining.includes('release/dev-to-main-PREEXIST'),
      'a branch present before the run must survive the snapshot diff',
    );
    assert.ok(
      !remaining.includes('release/dev-to-main-TEST'),
      'only this run\'s newly-created orphan may be deleted',
    );
    assert.doesNotMatch(
      res.stderr,
      /deleted: .*PREEXIST/,
      'the pre-existing branch must not be reported as deleted',
    );
  });

  test('an orphan pushed to origin is preserved and named, not deleted', () => {
    const layout = buildRepoLayout({
      releaseScript: [
        '#!/usr/bin/env bash',
        'git checkout -b release/dev-to-main-PUSHED origin/main',
        'git push -q origin release/dev-to-main-PUSHED',
        'exec sleep 30',
        '',
      ].join('\n'),
    });

    const res = runWrapper(layout, ['--skip-checks'], {
      SHIP_RELEASE_TIMEOUT_MS: '2000',
    });

    assert.equal(res.status, 10, `expected exit 10; stderr: ${res.stderr}`);
    assert.ok(
      branches(layout.repo).includes('release/dev-to-main-PUSHED'),
      'a branch pushed to origin must not be deleted',
    );
    assert.match(res.stderr, /"release\/dev-to-main-PUSHED" \(exists on origin\)/);
    assert.equal(currentBranch(layout.repo), 'dev', 'checkout must still be restored to dev');
  });

  test('an orphan backing an open PR is preserved and named, not deleted', () => {
    const layout = buildRepoLayout({
      ghPrList: JSON.stringify([
        { url: 'https://github.com/owner/repo/pull/999' },
      ]),
      releaseScript: [
        '#!/usr/bin/env bash',
        'git checkout -b release/dev-to-main-PR origin/main',
        'exec sleep 30',
        '',
      ].join('\n'),
    });

    const res = runWrapper(layout, ['--skip-checks'], {
      SHIP_RELEASE_TIMEOUT_MS: '2000',
    });

    assert.equal(res.status, 10, `expected exit 10; stderr: ${res.stderr}`);
    assert.ok(
      branches(layout.repo).includes('release/dev-to-main-PR'),
      'a branch backing an open PR must not be deleted',
    );
    assert.match(res.stderr, /"release\/dev-to-main-PR" \(backs an open PR\)/);
    assert.equal(currentBranch(layout.repo), 'dev', 'checkout must still be restored to dev');
  });
});

// ── 3. Normal (non-timeout) path unchanged ──────────────────────────────────

describe('release-script timeout: normal path unchanged', () => {
  test('a fast successful script exits 0 and leaves the branch untouched', () => {
    const layout = buildRepoLayout({
      releaseScript: '#!/usr/bin/env bash\nexit 0\n',
    });

    const res = runWrapper(layout, ['--dry-run', '--skip-checks']);

    assert.equal(
      res.status,
      0,
      `expected exit 0 on the normal path, got ${res.status}; stderr: ${res.stderr}`,
    );
    assert.equal(currentBranch(layout.repo), 'dev', 'a non-timeout run must not move the checkout');
    assert.doesNotMatch(
      res.stderr,
      /orphan branch|timed out/i,
      'the normal path must not emit timeout-recovery output',
    );
    assert.ok(
      !existsSync(join(layout.repo, '.worklog', 'code-freeze.json')),
      'Code Freeze marker must be cleared on the normal path',
    );
  });
});
