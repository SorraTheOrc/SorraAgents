/**
 * Integration tests for the post-release tail ordering and the non-fatal
 * dev-sync contract in run-release.js (SA-0MWXMTERQ005HYQ3).
 *
 * The 2026-11-26 release (v0.2.0, PR #730) landed on `main`, yet the tail
 * never ran: `/tmp/ship-release7.log` shows `Step 7: sync dev with main
 * 0.000` and no Steps 8-10, leaving shipped items `completed/in_review`.
 * The cause was that `run-release.js` called `syncDevWithMain()` at Step 7
 * and `return finish(5)` on failure — BEFORE merge verification (~Step 8),
 * the Discord notification (~Step 8.5), item closure (~Step 9) and cleanup
 * (~Step 10).
 *
 * These tests run the REAL run-release.js (copied into a temp skill layout)
 * with mocked `git`/`wl`/`gh` binaries on PATH and the shared
 * `release-notify-stub.mjs`. Every step invocation (git, wl, notification,
 * cleanup) appends to one shared event log, so the post-release order can be
 * asserted as an ordered subsequence:
 *
 *   verify release merge → Discord notification → close work items
 *   → branch cleanup → sync dev with main (last)
 *
 * The ordering and non-fatal-sync cases are `{ todo: true }` until the
 * sibling reorder item (SA-0MWXIY8D2001DGZ0 decomposition) lands — the
 * red-first analogue of `pytest.mark.xfail(strict=True)` — so the
 * changed-scope gate stays green. The gate assertions (exit 11, exit 14)
 * already hold with today's code and stay unmarked.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { makeTempDir } from '../helpers/temp-dir.mjs';
import { fileURLToPath } from 'node:url';

import {
  DISCORD_NOTIFY_LOG_ENV,
  writeStubDiscordNotify,
} from '../helpers/release-notify-stub.mjs';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const CHECK_UNMERGED_BRANCHES_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-unmerged-branches.js');
const CHECK_AUDIT_GATE_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-audit-gate.js');
const AUDIT_FRESHNESS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'audit-freshness.js');
const AUDIT_REMEDIATION_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'audit-remediation.js');
const REFRESH_AUDITS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'refresh-audits.js');
const CHECK_FINAL_VALIDATION_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-final-validation.js');
const CHECK_CRITICAL_ITEMS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-critical-items.js');
const CHECK_WORKLOG_REFS_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'check-worklog-refs.js');
const TIMING_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'timing.js');
const LLM_SRC = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'llm.js');

// ── Helper: write a recording cleanup script ────────────────────────────────

/**
 * Write a no-op python cleanup script that records its invocation into the
 * shared event log. The real cleanup step shells out to
 * `summarize_branches.py` / `prune_local_branches.py` /
 * `delete_remote_branches.py`; these stubs let the ordering assertion see the
 * cleanup step without touching a real repository.
 *
 * @param {string} dir - Directory to write the script into.
 * @param {string} name - Script file name.
 * @param {string} marker - Event-log marker appended on each invocation.
 */
function writeCleanupStub(dir, name, marker) {
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, name),
    `#!/usr/bin/env python3
import json, os, sys

log = os.environ.get('EVENT_LOG')
if log:
    with open(log, 'a') as fh:
        fh.write('cleanup ${marker}\\n')

args = sys.argv[1:]
if '--report' in args:
    idx = args.index('--report')
    if idx + 1 < len(args):
        with open(args[idx + 1], 'w') as fh:
            json.dump({'branches': []}, fh)

print(json.dumps({'summary': {'deleted': 0}}))
sys.exit(0)
`,
    { mode: 0o755 },
  );
}

// ── Helper: build a fake skill dir with mocked git/wl/gh ─────────────────────

/**
 * Create a temp skill layout with run-release.js + deps, mocked git/wl/gh on
 * PATH, a recording cleanup stub, and the shared notifier stub. Runs
 * `node run-release.js --skip-checks` and returns the spawnSync result plus
 * the paths of the shared event log.
 *
 * @param {object} opts
 * @param {'success'|'missing-tag'|'not-ancestor'|'present-tag'|'push-fail'} [opts.gitMode]
 *   - git mock behaviour (default `success`).
 * @returns {{ res: import('node:child_process').SpawnSyncReturns<string>, eventLog: string, closeLog: string, tmpDir: string }}
 */
function runReleaseWithMocks({ gitMode = 'success' } = {}) {
  const tmpDir = makeTempDir('run-release-tail-order-test-');
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  // Copy the real scripts (same layout run-release.js expects).
  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(RUN_RELEASE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-unmerged-branches.js'), readFileSync(CHECK_UNMERGED_BRANCHES_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-audit-gate.js'), readFileSync(CHECK_AUDIT_GATE_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'audit-freshness.js'), readFileSync(AUDIT_FRESHNESS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'audit-remediation.js'), readFileSync(AUDIT_REMEDIATION_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'refresh-audits.js'), readFileSync(REFRESH_AUDITS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-final-validation.js'), readFileSync(CHECK_FINAL_VALIDATION_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-critical-items.js'), readFileSync(CHECK_CRITICAL_ITEMS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'check-worklog-refs.js'), readFileSync(CHECK_WORKLOG_REFS_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'timing.js'), readFileSync(TIMING_SRC, 'utf8'));
  writeFileSync(join(skillScriptDir, 'llm.js'), readFileSync(LLM_SRC, 'utf8'));
  // Substitute the recording stub for the real discord-notify.js so this
  // harness can never reach a real Discord webhook (WL-0MUYA98Z8003HU8X /
  // SA-0MU3VIW8C0035KKL). Do NOT copy the real module here.
  writeStubDiscordNotify(skillScriptDir);

  // Mock release script: succeeds and prints no PR URL.
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(join(releaseDir, 'merge-dev-to-main.sh'), '#!/bin/bash\necho "mock release script completed"\n', { mode: 0o755 });

  // Recording cleanup stubs (the real cleanup step shells out to python).
  const cleanupDir = join(tmpDir, 'skill', 'cleanup', 'scripts');
  writeCleanupStub(cleanupDir, 'summarize_branches.py', 'summarize');
  writeCleanupStub(cleanupDir, 'prune_local_branches.py', 'prune-local');
  writeCleanupStub(cleanupDir, 'delete_remote_branches.py', 'delete-remote');

  // One shared event log: git, wl, cleanup and the notifier all append here.
  const eventLog = join(tmpDir, 'events.log');

  // Mock binaries.
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });

  const gitMock = join(binDir, 'git');
  writeFileSync(gitMock, `#!/usr/bin/env bash
echo "git $*" >> "$EVENT_LOG"
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
  push)
    if [[ "$GIT_MOCK_MODE" == "push-fail" ]]; then
      echo "Connection to github.com closed by remote host." >&2
      exit 128
    fi
    exit 0
    ;;
  describe) echo "v9.9.9" ;;
  tag)
    if [[ "$2" == "--list" ]] && [[ "$GIT_MOCK_MODE" == "present-tag" ]]; then
      echo "v9.9.9"
    fi
    exit 0
    ;;
  ls-remote)
    if [[ "$*" == *"refs/tags/v9.9.9"* ]] && [[ "$GIT_MOCK_MODE" != "missing-tag" ]]; then
      echo "abcd1234abcd1234abcd1234abcd1234abcd1234\\trefs/tags/v9.9.9"
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

  const closeLog = join(tmpDir, 'close.log');

  const wlMock = join(binDir, 'wl');
  writeFileSync(wlMock, `#!/usr/bin/env bash
echo "wl $*" >> "$EVENT_LOG"
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      echo '{"success":true,"workItems":[{"id":"SA-CLOSE1","title":"Close Me","needsProducerReview":false,"parentId":null,"updatedAt":"2026-01-01T00:00:00.000Z"}]}'
    else
      echo '{"success":true,"workItems":[]}'
    fi
    ;;
  show)
    echo '{"success":true,"workItem":{"id":"'"$2"'","status":"completed","stage":"done"},"children":[]}'
    ;;
  comment)
    echo '{"success":true,"comments":[]}'
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
      EVENT_LOG: eventLog,
      WL_CLOSE_LOG: closeLog,
      // Point the shared notifier stub at the SAME shared event log so the
      // ordering assertion sees notifications alongside git/wl/cleanup.
      [DISCORD_NOTIFY_LOG_ENV]: eventLog,
    },
  });

  return { res, eventLog, closeLog, tmpDir };
}

// ── Helper: ordered subsequence assertion ───────────────────────────────────

/**
 * Read the shared event log into a list of events (one per line). JSON lines
 * written by the notifier stub are kept as-is; plain text lines are
 * git/wl/cleanup invocations.
 *
 * @param {string} eventLog - Path to the shared event log.
 * @returns {string[]} Event lines (empty when the log does not exist).
 */
function readEvents(eventLog) {
  if (!existsSync(eventLog)) return [];
  return readFileSync(eventLog, 'utf-8').split('\n').filter(Boolean);
}

/**
 * Assert that `expected` appears as an ordered subsequence of `events` — order
 * assertions, not exact-sequence assertions, so unrelated steps stay free.
 *
 * @param {string[]} events - Recorded event lines.
 * @param {string[]} expected - Substrings to find in order.
 * @param {string} label - Assertion label for failure messages.
 */
function assertOrderedSubsequence(events, expected, label) {
  let searchFrom = 0;
  const positions = [];
  for (const want of expected) {
    let found = -1;
    for (let i = searchFrom; i < events.length; i += 1) {
      if (events[i].includes(want)) {
        found = i;
        break;
      }
    }
    assert.ok(
      found >= 0,
      `${label}: expected "${want}" at or after event index ${searchFrom}, got:\n${events.join('\n')}`,
    );
    positions.push(found);
    searchFrom = found + 1;
  }
  return positions;
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('run-release post-release tail order and non-fatal dev sync', () => {
  test(
    'runs verify merge → Discord notification → close → cleanup → sync dev last',
    { todo: true },
    () => {
      const { res, eventLog } = runReleaseWithMocks({ gitMode: 'success' });
      assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${res.stdout}\n${res.stderr}`);

      const events = readEvents(eventLog);
      // Order assertion: verify → notify → close → cleanup → sync dev last.
      assertOrderedSubsequence(
        events,
        [
          'git ls-remote origin refs/tags/v9.9.9', // Step 8: verify release merge
          '"stubbed":true',                          // Step 8.5: Discord notification
          'wl close SA-CLOSE1',                     // Step 9: close work items
          'cleanup summarize',                      // Step 10: branch cleanup
          'git push origin dev',                    // sync dev with main LAST
        ],
        'tail order',
      );
    },
  );

  test(
    'a failed `git push origin dev` is non-fatal: exit 0, notification + close recorded, recovery command warned',
    { todo: true },
    () => {
      const { res, eventLog, closeLog } = runReleaseWithMocks({ gitMode: 'push-fail' });
      assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${res.stdout}\n${res.stderr}`);

      const events = readEvents(eventLog);
      // The notification and the close must still have run despite the dev
      // sync failure at the end of the tail.
      assert.ok(
        events.some((e) => e.includes('"stubbed":true')),
        `Discord notification should still run when dev sync fails, got:\n${events.join('\n')}`,
      );
      assert.ok(
        existsSync(closeLog) && readFileSync(closeLog, 'utf-8').includes('SA-CLOSE1'),
        'work items should still be closed when dev sync fails',
      );

      const out = `${res.stdout}\n${res.stderr}`;
      assert.ok(
        out.includes('Connection to github.com closed by remote host'),
        `stderr should name the dev-sync failure, got:\n${out}`,
      );
      assert.ok(
        out.includes('⚠'),
        `stderr should carry a warning marker, got:\n${out}`,
      );
      assert.ok(
        out.includes('git push origin dev'),
        `stderr should name the exact recovery command, got:\n${out}`,
      );
    },
  );

  test('a failed verifyReleaseMerge still exits 11 with no notification and no close recorded', () => {
    const { res, eventLog, closeLog } = runReleaseWithMocks({ gitMode: 'not-ancestor' });
    assert.equal(res.status, 11, `expected exit 11, got ${res.status}\n${res.stdout}\n${res.stderr}`);

    const out = `${res.stdout}\n${res.stderr}`;
    assert.ok(
      out.includes('did not land') || out.includes('ancestor'),
      `should report the unverified merge, got:\n${out}`,
    );

    const events = readEvents(eventLog);
    assert.ok(
      !events.some((e) => e.includes('"stubbed":true')),
      'no Discord notification may be emitted when the merge did not land',
    );
    assert.ok(
      !existsSync(closeLog) || readFileSync(closeLog, 'utf-8').trim() === '',
      'no work items should be closed when the merge did not land',
    );
    assert.ok(
      !events.some((e) => e.startsWith('cleanup ')),
      'no branch cleanup may run when the merge did not land',
    );
  });

  test('an already-present release tag still exits 14 with no post-release step recorded', () => {
    const { res, eventLog, closeLog } = runReleaseWithMocks({ gitMode: 'present-tag' });
    assert.equal(res.status, 14, `expected exit 14, got ${res.status}\n${res.stdout}\n${res.stderr}`);

    const out = `${res.stdout}\n${res.stderr}`;
    assert.ok(
      out.includes('already existed') || out.includes('exit code 14'),
      `should report the stale tag, got:\n${out}`,
    );

    const events = readEvents(eventLog);
    assert.ok(
      !events.some((e) => e.includes('"stubbed":true')),
      'no Discord notification may be emitted for an already-present tag',
    );
    assert.ok(
      !existsSync(closeLog) || readFileSync(closeLog, 'utf-8').trim() === '',
      'no work items should be closed for an already-present tag',
    );
    // The tag gate refuses every verification-dependent post-release step:
    // notification, close and cleanup. The dev sync's own position is pinned
    // by the ordering test above (it must run LAST, behind verification), so
    // this gate test does not assert on `git push origin dev` — doing so would
    // couple the tag gate to a placement detail the ordering test already owns.
    assert.ok(
      !events.some((e) => e.startsWith('cleanup ')),
      'no branch cleanup may run for an already-present tag',
    );
  });
});
