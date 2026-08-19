/**
 * Integration tests for the release merge-verification guard at Step 8 of
 * run-release.js (SA-0MSJ2XMQL006CVQS).
 *
 * The close-work-items step must only run after a verified dev→main merge.
 * These tests run the REAL run-release.js (copied into a temp skill layout)
 * with mocked `git`/`wl`/`gh` binaries on PATH, and assert:
 *
 *   1. exit code 11 and NO work items closed when the version tag is missing
 *      on origin (no release ever landed);
 *   2. exit code 11 and NO work items closed when the merge did not land on
 *      origin/main;
 *   3. exit code 0 and work items closed only when the merge is verified.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');
const DISCORD_NOTIFY_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'discord-notify.js');

// ── Helper: build a fake skill dir with mocked git/wl/gh ─────────────────────

/**
 * Create a temp skill layout with run-release.js + deps and mocked
 * git/wl/gh on PATH. Runs `node run-release.js --skip-checks` and returns
 * the spawnSync result plus the close-command log path.
 *
 * @param {object} opts
 * @param {'missing-tag'|'not-ancestor'|'success'} [opts.gitMode] - git mock behaviour.
 * @returns {{ res: import('node:child_process').SpawnSyncReturns<string>, closeLog: string, tmpDir: string }}
 */
function runReleaseWithMocks(gitMode = 'success') {
  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-guard-test-'));
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  // Copy the real scripts (same layout run-release.js expects).
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'discord-notify.js'), readFileSync(DISCORD_NOTIFY_SRC, 'utf8'));

  // Mock release script: succeeds and prints no PR URL.
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'), '#!/bin/bash\necho "mock release script completed"\n', { mode: 0o755 });

  // Mock binaries.
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });

  const closeLog = join(tmpDir, 'close.log');

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
  ls-remote)
    if [[ "$*" == *"refs/tags/v9.9.9"* ]] && [[ "$GIT_MOCK_MODE" != "missing-tag" ]]; then
      echo "abcd1234abcd1234abcd1234abcd1234abcd1234\trefs/tags/v9.9.9"
    fi
    ;;
  merge-base)
    if [[ "$GIT_MOCK_MODE" == "not-ancestor" ]]; then
      exit 1
    fi
    exit 0
    ;;
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

  const runReleasePath = join(skillScriptDir, 'run-release.js');
  const res = spawnSync(process.execPath, [runReleasePath, '--skip-checks'], {
    cwd: tmpDir,
    encoding: 'utf-8',
    timeout: 30000,
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH}`,
      MOCK_TOPLVL: tmpDir,
      GIT_MOCK_MODE: gitMode,
      WL_CLOSE_LOG: closeLog,
    },
  });

  return { res, closeLog, tmpDir };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('run-release Step 8 merge-verification guard', () => {
  test('exits 11 and does NOT close items when the release tag is missing on origin', () => {
    const { res, closeLog } = runReleaseWithMocks('missing-tag');
    assert.equal(res.status, 11, `expected exit 11, got ${res.status}\n${res.stdout}\n${res.stderr}`);
    const out = `${res.stdout}\n${res.stderr}`;
    assert.ok(
      out.includes('not found on origin'),
      `should report the missing tag, got:\n${out}`,
    );
    assert.ok(
      !existsSync(closeLog) || readFileSync(closeLog, 'utf-8').trim() === '',
      'no work items should be closed when the release did not land',
    );
  });

  test('exits 11 and does NOT close items when the merge did not land on origin/main', () => {
    const { res, closeLog } = runReleaseWithMocks('not-ancestor');
    assert.equal(res.status, 11, `expected exit 11, got ${res.status}\n${res.stdout}\n${res.stderr}`);
    const out = `${res.stdout}\n${res.stderr}`;
    assert.ok(
      out.includes('did not land') || out.includes('ancestor'),
      `should report the unverified merge, got:\n${out}`,
    );
    assert.ok(
      !existsSync(closeLog) || readFileSync(closeLog, 'utf-8').trim() === '',
      'no work items should be closed when the merge did not land',
    );
  });

  test('closes work items ONLY after the merge is verified', () => {
    const { res, closeLog } = runReleaseWithMocks('success');
    assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${res.stdout}\n${res.stderr}`);
    const out = `${res.stdout}\n${res.stderr}`;
    assert.ok(
      !out.includes('refusing to close'),
      `guard should pass on a verified merge, got:\n${out}`,
    );
    assert.ok(existsSync(closeLog), 'close log should exist after a verified release');
    const closeLines = readFileSync(closeLog, 'utf-8').trim().split('\n').filter(Boolean);
    assert.equal(closeLines.length, 1, `expected 1 close, got: ${closeLines}`);
    assert.ok(
      closeLines[0].includes('--force') && closeLines[0].includes('Shipped in v9.9.9'),
      `close should use --force with reason "Shipped in v9.9.9", got: ${closeLines[0]}`,
    );
  });
});
