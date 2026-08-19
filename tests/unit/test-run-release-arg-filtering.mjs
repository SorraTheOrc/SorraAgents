/**
 * Unit tests for skill/ship/scripts/run-release.js arg forwarding
 * (SA-0MSKYGAWJ0009M3P).
 *
 * The release wrapper consumes `--skip-checks` internally to bypass its five
 * gating checks, but it must NOT forward that flag to the canonical merge
 * script (`merge-dev-to-main.sh`), which rejects unknown flags with exit 2.
 * These tests pin the arg-filtering behaviour:
 *
 *  1. Unit: `releaseScriptForwardArgs()` strips wrapper-only flags.
 *  2. Integration: running `run-release.js --skip-checks` invokes the merge
 *     script WITHOUT `--skip-checks` in its argv.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

import { releaseScriptForwardArgs } from '../../skill/ship/scripts/run-release.js';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');
const DISCORD_NOTIFY_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'discord-notify.js');

// ── Unit tests: releaseScriptForwardArgs ────────────────────────────────────

describe('run-release: releaseScriptForwardArgs', () => {
  test('strips --skip-checks when it is the only arg', () => {
    assert.deepEqual(releaseScriptForwardArgs(['--skip-checks']), []);
  });

  test('strips --skip-checks and preserves other flags', () => {
    assert.deepEqual(
      releaseScriptForwardArgs(['--skip-checks', '--force']),
      ['--force'],
    );
  });

  test('strips --skip-checks from the middle of the arg list', () => {
    assert.deepEqual(
      releaseScriptForwardArgs(['--force', '--skip-checks', '--bump', 'patch']),
      ['--force', '--bump', 'patch'],
    );
  });

  test('keeps all merge-script flags unchanged', () => {
    const args = ['--dry-run', '--force', '--work-item-id', 'SA-000', '--bump', 'minor'];
    assert.deepEqual(releaseScriptForwardArgs(args), args);
  });

  test('returns an empty array for no args', () => {
    assert.deepEqual(releaseScriptForwardArgs([]), []);
    assert.deepEqual(releaseScriptForwardArgs(), []);
  });
});

// ── Integration test: merge script argv ─────────────────────────────────────

/**
 * Build a fake skill layout (like test-run-release.mjs) with a recording
 * merge script, then run run-release.js with the given args.
 *
 * The fake merge script writes its argv to the path in env var
 * `RELEASE_RECORD_FILE` and exits 0.
 *
 * @param {string[]} cliArgs - Args passed to run-release.js.
 * @returns {{ status: number|null, recordFile: string, recorded: string }}
 */
function runRunReleaseWithRecordingScript(cliArgs) {
  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-argfilter-'));
  const recordFile = join(tmpDir, 'merge-args.txt');

  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'discord-notify.js'), readFileSync(DISCORD_NOTIFY_SRC, 'utf8'));

  const skillReleaseDir = join(skillScriptDir, 'release');
  mkdirSync(skillReleaseDir, { recursive: true });
  writeFileSync(
    join(skillReleaseDir, 'merge-dev-to-main.sh'),
    '#!/bin/bash\necho "$*" > "${RELEASE_RECORD_FILE}"\nexit 0\n',
  );

  const previousRecordFile = process.env.RELEASE_RECORD_FILE;
  process.env.RELEASE_RECORD_FILE = recordFile;
  try {
    const res = spawnSync(
      process.execPath,
      [join(skillScriptDir, 'run-release.js'), ...cliArgs],
      { cwd: tmpDir, encoding: 'utf-8', timeout: 15_000 },
    );
    let recorded = '';
    try {
      recorded = readFileSync(recordFile, 'utf8').trim();
    } catch {
      // merge script never ran — recorded stays empty
    }
    return { status: res.status, recordFile, recorded };
  } finally {
    if (previousRecordFile === undefined) {
      delete process.env.RELEASE_RECORD_FILE;
    } else {
      process.env.RELEASE_RECORD_FILE = previousRecordFile;
    }
  }
}

describe('run-release: --skip-checks forwarding', () => {
  test('merge script is NOT invoked with --skip-checks', () => {
    const { recorded } = runRunReleaseWithRecordingScript(['--skip-checks']);
    assert.ok(
      !recorded.includes('--skip-checks'),
      `Merge script must not receive --skip-checks, got argv: "${recorded}"`,
    );
  });

  test('merge script still receives --force alongside --skip-checks', () => {
    const { recorded } = runRunReleaseWithRecordingScript(['--skip-checks', '--force']);
    assert.ok(
      recorded.includes('--force'),
      `Merge script should receive --force, got argv: "${recorded}"`,
    );
    assert.ok(
      !recorded.includes('--skip-checks'),
      `Merge script must not receive --skip-checks, got argv: "${recorded}"`,
    );
  });

  test('merge script receives flags unchanged when no wrapper-only flags present', () => {
    const { recorded } = runRunReleaseWithRecordingScript(['--dry-run']);
    assert.ok(
      recorded.includes('--dry-run'),
      `Merge script should receive --dry-run, got argv: "${recorded}"`,
    );
  });
});
