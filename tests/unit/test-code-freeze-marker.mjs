/**
 * Tests for the ship-side Code Freeze marker lifecycle (SA-0MSBU4OBU005WJNB).
 *
 * Contract (per work item ACs):
 *  1. `setCodeFreezeMarker()` writes `<project-root>/.worklog/code-freeze.json`
 *     with the cross-repo contract format (WL-0MSBU4KMA004PKSR):
 *     `{ "active": true, "reason": "ship release in progress",
 *        "startedAt": "<ISO>", "pid": <pid> }`.
 *  2. `clearCodeFreezeMarker()` removes it (idempotent).
 *  3. `runRelease()` sets the marker at the start of the release (before
 *     gating) and clears it on EVERY exit path (success, failure, dry-run,
 *     gating failure) via a try/finally.
 *  4. The marker path resolves into the project's `.worklog/` directory.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  existsSync,
  rmSync,
} from 'node:fs';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeTempSkillDir() {
  const tmpDir = mkdtempSync(join(tmpdir(), 'code-freeze-test-'));
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));
  return { tmpDir, skillScriptDir };
}

/** A release script that exits with the given code and prints marker status. */
function writeFakeReleaseScript(skillScriptDir, exitCode) {
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  const script = `#!/bin/bash
if [ -f .worklog/code-freeze.json ]; then
  echo "MARKER_PRESENT_DURING_RELEASE"
else
  echo "MARKER_ABSENT_DURING_RELEASE"
fi
echo "FAKE_RELEASE_RAN"
exit ${exitCode}
`;
  writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'), script);
}

function runRelease(args, cwd) {
  return spawnSync(process.execPath, [join(cwd, 'skill', 'ship', 'scripts', 'run-release.js'), ...args], {
    cwd,
    encoding: 'utf-8',
    timeout: 30_000,
  });
}

// ── Marker helpers (import from source) ──────────────────────────────────────

const { setCodeFreezeMarker, clearCodeFreezeMarker, codeFreezeMarkerPath } =
  await import(`${RUN_RELEASE_SRC}?t=${Date.now()}`);

describe('code-freeze marker: path resolution', () => {
  test('resolves into <projectRoot>/.worklog/code-freeze.json', () => {
    const path = codeFreezeMarkerPath('/tmp/myproj');
    assert.equal(path, join('/tmp/myproj', '.worklog', 'code-freeze.json'));
  });
});

describe('code-freeze marker: set/clear helpers', () => {
  test('setCodeFreezeMarker writes the contract marker file', () => {
    const tmpDir = mkdtempSync(join(tmpdir(), 'cf-set-'));
    const markerPath = setCodeFreezeMarker(tmpDir);
    assert.ok(existsSync(markerPath), 'marker file must exist after set');
    const raw = readFileSync(markerPath, 'utf8');
    const marker = JSON.parse(raw);
    assert.equal(marker.active, true);
    assert.equal(marker.reason, 'ship release in progress');
    assert.ok(typeof marker.startedAt === 'string' && marker.startedAt.length > 0);
    assert.ok(typeof marker.pid === 'number' && marker.pid > 0);
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('clearCodeFreezeMarker removes the marker (idempotent)', () => {
    const tmpDir = mkdtempSync(join(tmpdir(), 'cf-clear-'));
    const markerPath = setCodeFreezeMarker(tmpDir);
    assert.ok(existsSync(markerPath));
    clearCodeFreezeMarker(tmpDir);
    assert.ok(!existsSync(markerPath), 'marker must be gone after clear');
    // Second clear must not throw.
    clearCodeFreezeMarker(tmpDir);
    rmSync(tmpDir, { recursive: true, force: true });
  });
});

describe('code-freeze marker: merge-dev-to-main.sh fallback (trap)', () => {
  const MERGE_SCRIPT_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh');

  test('script sets the marker and clears it on EXIT (trap)', () => {
    const content = readFileSync(MERGE_SCRIPT_SRC, 'utf8');
    assert.ok(content.includes('write_code_freeze_marker'), 'should define write_code_freeze_marker');
    assert.ok(content.includes('clear_code_freeze_marker'), 'should define clear_code_freeze_marker');
    assert.ok(content.includes('trap clear_code_freeze_marker EXIT'), 'should trap EXIT to clear the marker');
    assert.ok(content.includes('code-freeze.json'), 'should reference the contract marker path');
  });

  test('shell script clears marker on dry-run exit', () => {
    const { tmpDir, skillScriptDir } = makeTempSkillDir();
    // Copy the real merge script into the fake skill layout.
    const releaseDir = join(skillScriptDir, 'release');
    mkdirSync(releaseDir, { recursive: true });
    writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'), readFileSync(MERGE_SCRIPT_SRC, 'utf8'));

    // Set up a minimal git repo so the script's git checks pass.
    spawnSync('git', ['init', '-b', 'dev'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['config', 'user.email', 't@t.com'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['config', 'user.name', 'T'], { cwd: tmpDir, encoding: 'utf-8' });
    writeFileSync(join(tmpDir, 'README.md'), '# repo');
    spawnSync('git', ['add', '-A'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['commit', '-m', 'init'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['branch', 'main'], { cwd: tmpDir, encoding: 'utf-8' });

    // gh is required by the script before the marker is written; stub it.
    const binDir = join(tmpDir, 'bin');
    mkdirSync(binDir, { recursive: true });
    writeFileSync(join(binDir, 'gh'), '#!/bin/bash\nexit 0\n');
    spawnSync('chmod', ['+x', join(binDir, 'gh')], { cwd: tmpDir, encoding: 'utf-8' });

    const res = spawnSync('bash', [join(releaseDir, 'merge-dev-to-main.sh'), '--dry-run'], {
      cwd: tmpDir,
      encoding: 'utf-8',
      timeout: 30_000,
      env: { ...process.env, PATH: `${binDir}:${process.env.PATH}` },
    });

    const markerPath = join(tmpDir, '.worklog', 'code-freeze.json');
    // The script may exit early with a non-zero code (e.g. worklog-ref check,
    // missing origin) — but the marker must ALWAYS be cleared by the trap.
    assert.ok(!existsSync(markerPath), 'marker must be cleared on shell-script exit');
    rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ── runRelease lifecycle (marker set/cleared around the whole run) ───────────

describe('code-freeze marker: runRelease lifecycle', () => {
  test('marker is set BEFORE the release script runs', () => {
    const { tmpDir, skillScriptDir } = makeTempSkillDir();
    writeFakeReleaseScript(skillScriptDir, 0);
    // --skip-checks avoids gating that needs a real worklog; the fake script
    // reports whether the marker is visible during the release.
    const res = runRelease(['--skip-checks', '--dry-run'], tmpDir);
    const out = (res.stdout || '') + '\n' + (res.stderr || '');
    assert.ok(
      out.includes('MARKER_PRESENT_DURING_RELEASE'),
      `Expected marker present during release, got:\n${out}`,
    );
    // Marker must be cleared after the release finishes (dry-run success).
    const markerPath = join(tmpDir, '.worklog', 'code-freeze.json');
    assert.ok(!existsSync(markerPath), 'marker must be cleared after release');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('marker is cleared on release failure (non-zero release exit)', () => {
    const { tmpDir, skillScriptDir } = makeTempSkillDir();
    writeFakeReleaseScript(skillScriptDir, 7);
    const res = runRelease(['--skip-checks'], tmpDir);
    const markerPath = join(tmpDir, '.worklog', 'code-freeze.json');
    assert.notStrictEqual(res.status, 0, 'release should fail');
    assert.ok(!existsSync(markerPath), 'marker must be cleared on failure');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('marker is cleared on missing release script (exit path)', () => {
    const { tmpDir } = makeTempSkillDir(); // no release script
    const res = runRelease(['--skip-checks'], tmpDir);
    assert.equal(res.status, 2, 'missing script must exit 2');
    const markerPath = join(tmpDir, '.worklog', 'code-freeze.json');
    assert.ok(!existsSync(markerPath), 'marker must be cleared when script missing');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('marker is cleared on gating failure (unmerged branches)', () => {
    const { tmpDir, skillScriptDir } = makeTempSkillDir();
    writeFakeReleaseScript(skillScriptDir, 0);
    // Create a git repo in tmpDir with an unmerged feature branch so the
    // unmerged-branches gate fails (exit 3) BEFORE the release script runs.
    spawnSync('git', ['init', '-b', 'dev'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['config', 'user.email', 't@t.com'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['config', 'user.name', 'T'], { cwd: tmpDir, encoding: 'utf-8' });
    writeFileSync(join(tmpDir, 'README.md'), '# repo');
    spawnSync('git', ['add', '-A'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['commit', '-m', 'init'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['checkout', '-b', 'feature-x'], { cwd: tmpDir, encoding: 'utf-8' });
    writeFileSync(join(tmpDir, 'feature.txt'), 'x');
    spawnSync('git', ['add', '-A'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['commit', '-m', 'feature'], { cwd: tmpDir, encoding: 'utf-8' });
    spawnSync('git', ['checkout', 'dev'], { cwd: tmpDir, encoding: 'utf-8' });

    const res = runRelease([], tmpDir);
    assert.equal(res.status, 3, 'unmerged-branches gate must fail with exit 3');
    const markerPath = join(tmpDir, '.worklog', 'code-freeze.json');
    assert.ok(!existsSync(markerPath), 'marker must be cleared on gating failure');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('run-release.js imports marker helpers and wraps runRelease in try/finally', () => {
    const content = readFileSync(RUN_RELEASE_SRC, 'utf-8');
    assert.ok(content.includes('setCodeFreezeMarker'), 'should import/define setCodeFreezeMarker');
    assert.ok(content.includes('clearCodeFreezeMarker'), 'should import/define clearCodeFreezeMarker');
    assert.ok(content.includes('finally'), 'runRelease must use finally to clear the marker');
  });
});

// ---------------------------------------------------------------------------
// Release-script spawn timeout (SA-0MSDX3KTV0092B7N)
// ---------------------------------------------------------------------------
// A hung git/gh operation inside the release script must fail the release
// loudly after a bounded time instead of blocking indefinitely — while the
// release runs, the Code Freeze marker blocks all implementation work, so an
// unbounded spawn = an unbounded project-wide freeze.

test('release-script timeout: spawn is bounded, exit 10, marker cleared', () => {
  const { tmpDir, skillScriptDir } = makeTempSkillDir();

  // Fake release script that hangs — must be killed by the wrapper timeout.
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(
    join(releaseDir, 'merge-dev-to-main.sh'),
    '#!/bin/bash\nsleep 30\nexit 0\n',
  );

  const res = spawnSync(process.execPath, [
    join(tmpDir, 'skill', 'ship', 'scripts', 'run-release.js'),
    '--skip-checks',
  ], {
    cwd: tmpDir,
    encoding: 'utf-8',
    timeout: 30_000,
    env: { ...process.env, SHIP_RELEASE_TIMEOUT_MS: '500' },
  });

  assert.equal(
    res.status,
    10,
    `expected exit code 10 (timeout), got ${res.status}; stderr: ${res.stderr}`,
  );
  assert.match(
    res.stderr,
    /timed out|timeout/i,
    `stderr should report the timeout, got: ${res.stderr}`,
  );
  assert.ok(
    !existsSync(join(tmpDir, '.worklog', 'code-freeze.json')),
    'Code Freeze marker must be cleared after a timed-out release',
  );
});
