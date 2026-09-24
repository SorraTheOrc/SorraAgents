/**
 * Regression tests for the Step 8.5 Discord release notification wiring
 * (AH-0MTXLWCWO005OSCZ).
 *
 * The release pipeline previously raised
 *   "Discord notification step failed: projectRoot is not defined
 *    (non-blocking)."
 * because `runReleaseImpl()` called `sendReleaseNotification({ version,
 * prUrl, projectRoot })` without `projectRoot` in scope.
 *
 * These tests run the REAL run-release.js (copied into a temp skill layout)
 * with mocked git/wl/gh binaries on PATH and a mock discord-notify.js that
 * records the arguments it receives, then assert:
 *
 *   1. (AC1/AC2) the notification is invoked with the resolved project root,
 *      the released version, and the PR URL;
 *   2. (AC3) a throwing notifier never changes the release exit code (the
 *      failure is logged as a warning only).
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const CHECK_FINAL_VALIDATION_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-final-validation.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');
const TIMING_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'timing.js');
const PR_URL = 'https://github.com/example/repo/pull/42';

// ── Mock discord-notify.js -----------------------------------------------------------------
//
// Replaces the real notification module inside the temp skill layout. Records
// the arguments it receives (as JSON lines) to $DISCORD_NOTIFY_LOG, or throws
// when $DISCORD_MODE=throw so callers can verify the non-blocking contract.

const MOCK_DISCORD_NOTIFY = `export async function sendReleaseNotification({ version, prUrl, projectRoot } = {}) {
  const logPath = process.env.DISCORD_NOTIFY_LOG;
  if (logPath) {
    const { appendFileSync } = await import('node:fs');
    appendFileSync(logPath, JSON.stringify({
      version,
      prUrl,
      projectRoot,
      projectRootDefined: typeof projectRoot !== 'undefined' && projectRoot !== null && projectRoot !== '',
    }) + '\\n');
  }
  if (process.env.DISCORD_MODE === 'throw') {
    throw new Error('mock discord failure');
  }
  return { success: true, notified: true };
}
`;

// ── Harness: temp skill layout + mocked git/wl/gh + mock discord-notify -------

/**
 * Build a temp skill layout with the REAL run-release.js, mock the
 * discord-notify.js module, and run `node run-release.js --skip-checks`
 * with mocked git/wl/gh binaries on PATH (same fixture as the Step 8
 * merge-guard tests, so the pipeline reaches Step 8.5 in "success" mode).
 *
 * @param {object} [opts]
 * @param {'record'|'throw'} [opts.discordMode] - mock notifier behaviour.
 * @returns {object} { res, notifyLog, tmpDir }
 */
function runReleaseWithDiscordMock(discordMode = 'record') {
  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-discord-test-'));
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  // Copy the real scripts (same layout run-release.js expects).
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-final-validation.js'), readFileSync(CHECK_FINAL_VALIDATION_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'timing.js'), readFileSync(TIMING_SRC, 'utf8'));

  // Substitute the mock notifier for the real discord-notify.js.
  writeFileSync(join(skillScriptDir, 'discord-notify.js'), MOCK_DISCORD_NOTIFY);

  // Mock release script: succeeds and prints a GitHub PR URL. `--force` is
  // passed to runRelease below so the PR-merge wait is skipped while the URL
  // is still parsed and forwarded to the notification step.
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'), `#!/bin/bash\necho "mock release script completed"\necho "${PR_URL}"\n`, { mode: 0o755 });

  // Mock binaries.
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });

  const gitMock = join(binDir, 'git');
  writeFileSync(gitMock, `#!/usr/bin/env bash
case "$1" in
  rev-parse)
    case "$2" in
      --show-toplevel) echo "$MOCK_TOPLVL" ;;
      --verify) echo "abcd1234abcd1234abcd1234abcd1234abcd1234" ;;
    esac
    ;;
  fetch) exit 0 ;;
  checkout) exit 0 ;;
  merge) exit 0 ;;
  push) exit 0 ;;
  describe) echo "v9.9.9" ;;
  ls-remote) echo "abcd1234abcd1234abcd1234abcd1234abcd1234\trefs/tags/v9.9.9" ;;
  merge-base) exit 0 ;;
esac
exit 0
`, { mode: 0o755 });

  const wlMock = join(binDir, 'wl');
  writeFileSync(wlMock, `#!/usr/bin/env bash
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      echo '{"success":true,"workItems":[{"id":"SA-PARENT1","title":"Parent One","needsProducerReview":false}]}'
    else
      echo '{"success":true,"workItems":[]}'
    fi
    ;;
  close)
    echo "$@" >> "$WL_CLOSE_LOG"
    echo '{"success":true}'
    ;;
  *)
    echo '{"success":true}'
    ;;
esac
`, { mode: 0o755 });

  writeFileSync(join(binDir, 'gh'), '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });

  const notifyLog = join(tmpDir, 'discord-notify.log');
  const closeLog = join(tmpDir, 'close.log');
  const runReleasePath = join(skillScriptDir, 'run-release.js');

  const res = spawnSync(process.execPath, [runReleasePath, '--skip-checks', '--force'], {
    cwd: tmpDir,
    encoding: 'utf-8',
    timeout: 30000,
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH}`,
      MOCK_TOPLVL: tmpDir,
      DISCORD_MODE: discordMode,
      DISCORD_NOTIFY_LOG: notifyLog,
      WL_CLOSE_LOG: closeLog,
    },
  });

  return { res, notifyLog, closeLog, tmpDir };
}

/** Parse the captured notification calls from the mock's JSON-lines log. */
function readNotifyCalls(notifyLog) {
  try {
    const raw = readFileSync(notifyLog, 'utf-8');
    return raw.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
  } catch {
    return [];
  }
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('run-release Step 8.5 Discord notification wiring', () => {
  test('AC1/AC2: sendReleaseNotification receives the resolved projectRoot and version', () => {
    const { res, notifyLog, tmpDir } = runReleaseWithDiscordMock('record');
    assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${res.stdout}\n${res.stderr}`);

    const calls = readNotifyCalls(notifyLog);
    assert.equal(calls.length, 1, `expected exactly one notification call, got ${calls.length}`);
    const call = calls[0];

    // AC1: projectRoot must be defined (not the ReferenceError bug) and be
    // the repo root resolved by `git rev-parse --show-toplevel`.
    assert.equal(call.projectRootDefined, true, `projectRoot must be defined, got ${JSON.stringify(call)}`);
    assert.equal(call.projectRoot, tmpDir, `projectRoot should be the resolved repo root, got ${JSON.stringify(call)}`);

    // AC2: the embed payload data — released version + PR URL — is forwarded.
    assert.equal(call.version, '9.9.9', `released version should be forwarded, got ${JSON.stringify(call)}`);
    assert.equal(call.prUrl, PR_URL, `PR URL should be forwarded, got ${JSON.stringify(call)}`);
  });

  test('AC3: a throwing notifier logs a warning but keeps the release exit code', () => {
    const { res, closeLog } = runReleaseWithDiscordMock('throw');
    const out = `${res.stdout}\n${res.stderr}`;

    // The thrown error is caught: exit code unchanged.
    assert.equal(res.status, 0, `expected exit 0 despite notifier failure, got ${res.status}\n${out}`);
    assert.ok(
      out.includes('Discord notification step failed') && out.includes('(non-blocking)'),
      `should log the non-blocking warning, got:\n${out}`,
    );

    // The pipeline continued past the failed notification: Step 9 (close
    // work items) still ran and the release completed.
    const closeLines = readFileSync(closeLog, 'utf-8').trim().split('\n').filter(Boolean);
    assert.equal(closeLines.length, 1, `expected Step 9 to run after the notifier failure, got: ${closeLines}`);
  });
});