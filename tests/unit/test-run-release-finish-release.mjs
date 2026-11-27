/**
 * TDD-first tests for the `--finish-release <version>` recovery entry point
 * (SA-0MWXMTKYB006IBPK; implementation lands in SA-0MWXMTSKH0085UZZ).
 *
 * Background: re-running `run-release.js` after a partially-applied release
 * trips the newly-created-tag gate (exit 14) by design
 * (SA-0MV13B5WO008CYXA) — the released tag already exists, so the wrapper
 * cannot prove *this* run created it. On 2026-11-26 that forced an operator to
 * finish the v0.2.0 tail with a hand-rolled `node -e` script. `--finish-release
 * <version>` is the supported recovery: it runs ONLY the verified post-release
 * tail (verify merge → notify → close items → cleanup → sync dev last), with
 * no merge, no PR, no tag creation, no version bump and no Code Freeze marker,
 * and proves the release via `verifyReleaseMerge` instead of the pre-run tag
 * snapshot.
 *
 * Harness: the same temp skill layout + mocked `git`/`wl`/`gh` binaries and
 * the shared `release-notify-stub.mjs` as `test-run-release-tail-order.mjs`.
 * Every step invocation (git, wl, gh, cleanup, notification) appends to one
 * shared event log, so the "only the tail ran" contract is asserted as both an
 * ordered subsequence (the tail) and an absence set (merge/PR/tag/bump).
 *
 * These cases were landed red-first as `{ todo: true }` (the analogue of
 * `pytest.mark.xfail(strict=True)`, mirroring SA-0MW3GCHEL001G8CP) so the
 * changed-scope gate stayed green before the entry point existed; the
 * implementation item (SA-0MWXMTSKH0085UZZ) removed the markers so they now
 * enforce the recovery contract.
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

// The parser is imported directly so the recovery flag contract can be pinned
// without a full harness run.
import { parseReleaseArgs } from '../../skill/ship/scripts/run-release.js';
import { makeTempDir } from '../helpers/temp-dir.mjs';
import {
  DISCORD_NOTIFY_LOG_ENV,
  writeStubDiscordNotify,
} from '../helpers/release-notify-stub.mjs';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SKILL_SCRIPTS = join(REPO_ROOT, 'skill', 'ship', 'scripts');

// run-release.js's imported modules, copied verbatim into the temp layout.
// `discord-notify.js` is deliberately excluded — the no-I/O stub is written
// instead so the harness can never POST a real release notification.
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

// The version under recovery. Deliberately the 2026-11-26 v0.2.0 release.
const RECOVERY_VERSION = '0.2.0';

// ── Helper: recording cleanup stubs ─────────────────────────────────────────

/**
 * Write a no-op python cleanup script that records its invocation into the
 * shared event log. Mirrors the stubs used by `test-run-release-tail-order.mjs`;
 * the real cleanup step shells out to these scripts, so stubbing them lets the
 * assertion observe the cleanup step without touching a real repository.
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

// ── Helper: build the temp skill layout with mocked binaries ────────────────

/**
 * Build a temp skill layout containing the real `run-release.js` (plus its
 * dependencies), a recording merge-script stub, mocked `git`/`wl`/`gh`
 * binaries, recording cleanup stubs and the shared no-I/O notifier stub.
 *
 * The returned `run(args)` spawns the wrapper in that layout and returns the
 * `spawnSync` result. The layout keeps its recorded state (event log, close
 * log, "shipped" marker state) so a test can run the wrapper more than once
 * against the SAME recorded `wl` state to prove idempotent closure.
 *
 * @param {object} [opts]
 * @param {'success'|'not-ancestor'|'missing-tag'} [opts.gitMode='success']
 *   - `success`: the release tag exists on origin and is an ancestor of
 *     `origin/main` (a verified release).
 *   - `not-ancestor`: the tag exists but is NOT on `origin/main` (a failed
 *     verification — exit 11).
 *   - `missing-tag`: origin does not expose the tag (a failed verification).
 * @returns {{
 *   tmpDir: string,
 *   skillScriptDir: string,
 *   runReleasePath: string,
 *   eventLog: string,
 *   closeLog: string,
 *   shippedState: string,
 *   packageJsonPath: string,
 *   run: (args: string[]) => import('node:child_process').SpawnSyncReturns<string>,
 * }}
 */
function buildLayout({ gitMode = 'success' } = {}) {
  const tmpDir = makeTempDir('run-release-finish-release-');
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  // Copy the real scripts (same layout run-release.js expects).
  writeFileSync(
    join(skillScriptDir, 'run-release.js'),
    readFileSync(join(SKILL_SCRIPTS, 'run-release.js'), 'utf8'),
  );
  for (const dep of DEP_FILES) {
    writeFileSync(
      join(skillScriptDir, dep),
      readFileSync(join(SKILL_SCRIPTS, dep), 'utf8'),
    );
  }
  // Substitute the no-I/O recording stub for the real discord-notify.js so
  // this harness can never reach a real Discord webhook
  // (WL-0MUYA98Z8003HU8X / SA-0MU3VIW8C0035KKL).
  writeStubDiscordNotify(skillScriptDir);

  // Recording merge script — the recovery path must NEVER invoke it (it is the
  // step that bumps the version, makes the PR, merges and tags).
  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(
    join(releaseDir, 'merge-dev-to-main.sh'),
    '#!/bin/bash\necho "merge-script invoked" >> "$EVENT_LOG"\necho "mock release script completed"\n',
    { mode: 0o755 },
  );

  // Recording cleanup stubs (the real cleanup step shells out to python).
  const cleanupDir = join(tmpDir, 'skill', 'cleanup', 'scripts');
  writeCleanupStub(cleanupDir, 'summarize_branches.py', 'summarize');
  writeCleanupStub(cleanupDir, 'prune_local_branches.py', 'prune-local');
  writeCleanupStub(cleanupDir, 'delete_remote_branches.py', 'delete-remote');

  // The project root package.json carries a sentinel version so the test can
  // prove the recovery path performs NO version bump. `"type": "module"` also
  // makes the temp `.js` scripts unambiguously ESM.
  const packageJsonPath = join(tmpDir, 'package.json');
  writeFileSync(
    packageJsonPath,
    JSON.stringify({ name: 'finish-release-mock', version: '0.1.0', type: 'module' }, null, 2),
  );

  // One shared event log: git, wl, gh, cleanup and the notifier all append here.
  const eventLog = join(tmpDir, 'events.log');
  const closeLog = join(tmpDir, 'close.log');
  const shippedState = join(tmpDir, 'shipped.state');

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
      --abbrev-ref) echo "dev" ;;
    esac
    ;;
  fetch) exit 0 ;;
  checkout) exit 0 ;;
  merge) exit 0 ;;
  push) exit 0 ;;
  describe) echo "v${RECOVERY_VERSION}" ;;
  tag)
    # The recovery target tag ALREADY exists before this run (that is exactly
    # why --finish-release exists), so --list reports it.
    if [[ "$2" == "--list" ]]; then
      echo "v${RECOVERY_VERSION}"
    fi
    exit 0
    ;;
  ls-remote)
    if [[ "$*" == *"refs/tags/v${RECOVERY_VERSION}"* ]] && [[ "$GIT_MOCK_MODE" != "missing-tag" ]]; then
      echo "abcd1234abcd1234abcd1234abcd1234abcd1234\\trefs/tags/v${RECOVERY_VERSION}"
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
echo "wl $*" >> "$EVENT_LOG"
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      echo '{"success":true,"workItems":[{"id":"SA-SHIP1","title":"Ship Me","needsProducerReview":false,"parentId":null,"updatedAt":"2026-01-01T00:00:00.000Z"}]}'
    else
      echo '{"success":true,"workItems":[]}'
    fi
    ;;
  show)
    echo '{"success":true,"workItem":{"id":"'"$2"'","status":"completed","stage":"done"},"children":[]}'
    ;;
  comment)
    # wl comment list <id> --json
    if [[ "$2" == "list" ]]; then
      id="$3"
      if grep -qx "$id" "$WL_SHIPPED_STATE" 2>/dev/null; then
        echo '{"success":true,"comments":[{"author":"ship","comment":"Closed with reason: Shipped in v${RECOVERY_VERSION}"}]}'
      else
        echo '{"success":true,"comments":[]}'
      fi
    else
      echo '{"success":true}'
    fi
    ;;
  close)
    echo "$@" >> "$WL_CLOSE_LOG"
    # Record the closure so a re-run's idempotency check sees the shipped marker.
    printf '%s\\n' "$2" >> "$WL_SHIPPED_STATE"
    echo '{"success":true}'
    ;;
  *)
    echo '{"success":true}'
    ;;
esac
`, { mode: 0o755 });

  // `gh` mock records every invocation so the test can prove no `gh pr create`.
  const ghMock = join(binDir, 'gh');
  writeFileSync(ghMock, '#!/usr/bin/env bash\necho "gh $*" >> "$EVENT_LOG"\nexit 0\n', { mode: 0o755 });

  const runReleasePath = join(skillScriptDir, 'run-release.js');
  const run = (args) => spawnSync(process.execPath, [runReleasePath, ...args], {
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
      WL_SHIPPED_STATE: shippedState,
      RECOVERY_VERSION,
      // Point the shared notifier stub at the SAME event log so the ordering
      // assertion sees notifications alongside git/wl/gh/cleanup.
      [DISCORD_NOTIFY_LOG_ENV]: eventLog,
    },
  });

  return {
    tmpDir,
    skillScriptDir,
    runReleasePath,
    eventLog,
    closeLog,
    shippedState,
    packageJsonPath,
    run,
  };
}

// ── Helper: event-log readers and assertions ────────────────────────────────

/**
 * Read the shared event log into a list of events (one per line).
 *
 * @param {string} eventLog - Path to the shared event log.
 * @returns {string[]} Event lines (empty when the log does not exist).
 */
function readEvents(eventLog) {
  if (!existsSync(eventLog)) return [];
  return readFileSync(eventLog, 'utf-8').split('\n').filter(Boolean);
}

/**
 * Assert that `expected` appears as an ordered subsequence of `events` — an
 * order assertion, not an exact-sequence one, so unrelated steps stay free.
 *
 * @param {string[]} events - Recorded event lines.
 * @param {string[]} expected - Substrings to find in order.
 * @param {string} label - Assertion label for failure messages.
 */
function assertOrderedSubsequence(events, expected, label) {
  let searchFrom = 0;
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
    searchFrom = found + 1;
  }
}

/**
 * Count the notifier-stub lines recorded in the event log.
 *
 * @param {string[]} events - Recorded event lines.
 * @returns {string[]} The notifier JSON lines.
 */
function notifierLines(events) {
  return events.filter((e) => e.includes('"stubbed":true'));
}

/**
 * Assert the recovery run produced NONE of the forward-release side effects:
 * no merge-script invocation, no `gh pr create`, no tag creation, no version
 * bump and no Code Freeze marker.
 *
 * @param {object} layout - The layout returned by {@link buildLayout}.
 * @param {string[]} events - Recorded event lines.
 */
function assertNoForwardSideEffects(layout, events) {
  assert.ok(
    !events.some((e) => e.includes('merge-script invoked')),
    `the merge script must NOT run on the recovery path, got:\n${events.join('\n')}`,
  );
  assert.ok(
    !events.some((e) => e.startsWith('gh pr create')),
    `no PR may be created on the recovery path, got:\n${events.join('\n')}`,
  );
  // Tag creation (`git tag -a`, `git tag vX`) — `git tag --list` is read-only.
  assert.ok(
    !events.some((e) => /^git tag (?!.*--list)/.test(e)),
    `no tag may be created on the recovery path, got:\n${events.join('\n')}`,
  );
  // No version bump: the project package.json version is untouched.
  const pkg = JSON.parse(readFileSync(layout.packageJsonPath, 'utf-8'));
  assert.equal(
    pkg.version,
    '0.1.0',
    'the recovery path must not bump the project version',
  );
  // No Code Freeze marker is written.
  assert.ok(
    !existsSync(join(layout.tmpDir, '.worklog', 'code-freeze.json')),
    'the recovery path must not set the Code Freeze marker',
  );
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('--finish-release recovery entry point', () => {
  test(
    'parseReleaseArgs recognises --finish-release <version>',
    () => {
      const parsed = parseReleaseArgs(['--finish-release', RECOVERY_VERSION]);
      assert.equal(parsed.action, 'run');
      assert.equal(parsed.flags.finishRelease, RECOVERY_VERSION);
    },
  );

  test(
    'a missing --finish-release value is an argument error',
    () => {
      const parsed = parseReleaseArgs(['--finish-release']);
      assert.equal(parsed.action, 'error');
    },
  );

  test(
    'an unknown flag is still an argument error',
    () => {
      const parsed = parseReleaseArgs(['--finish-release', RECOVERY_VERSION, '--not-a-flag']);
      assert.equal(parsed.action, 'error');
    },
  );

  test(
    'runs only the verified post-release tail and exits 0 (no merge/PR/tag/bump/freeze)',
    () => {
      const layout = buildLayout({ gitMode: 'success' });
      const res = layout.run(['--finish-release', RECOVERY_VERSION]);
      assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${res.stdout}\n${res.stderr}`);

      const events = readEvents(layout.eventLog);

      // The verified tail runs in order: verify → notify → close → cleanup →
      // sync dev last. This mirrors the reorder item's contract; the recovery
      // entry point reuses the same shared tail helper.
      assertOrderedSubsequence(
        events,
        [
          `git ls-remote origin refs/tags/v${RECOVERY_VERSION}`, // Step: verify merge
          '"stubbed":true',                                       // Step: notification
          'wl close SA-SHIP1',                                    // Step: close work items
          'cleanup summarize',                                    // Step: branch cleanup
          'git push origin dev',                                  // Step: sync dev (last)
        ],
        'recovery tail',
      );

      // Nothing from the forward-release path ran.
      assertNoForwardSideEffects(layout, events);

      // The release itself is notified exactly once, with explicit recovery
      // provenance (the tag pre-existed this run).
      const notified = notifierLines(events);
      assert.equal(notified.length, 1, `expected exactly one notification, got:\n${notified.join('\n')}`);
      const payload = JSON.parse(notified[0]);
      assert.equal(payload.version, RECOVERY_VERSION);
      assert.equal(
        payload.provenance.tagCreatedByThisRun,
        true,
        'the recovery path must supply explicit tagCreatedByThisRun provenance',
      );

      // The shipped item was closed exactly once.
      assert.ok(existsSync(layout.closeLog), 'the shipped item should be closed');
      const closed = readFileSync(layout.closeLog, 'utf-8').split('\n').filter(Boolean);
      assert.equal(closed.length, 1, `expected one close, got:\n${closed.join('\n')}`);
      assert.ok(closed[0].includes('SA-SHIP1'));
    },
  );

  test(
    'a failed merge verification exits 11 with no notification and no close',
    () => {
      const layout = buildLayout({ gitMode: 'not-ancestor' });
      const res = layout.run(['--finish-release', RECOVERY_VERSION]);
      assert.equal(res.status, 11, `expected exit 11, got ${res.status}\n${res.stdout}\n${res.stderr}`);

      const out = `${res.stdout}\n${res.stderr}`;
      assert.ok(
        out.includes('did not land') || out.includes('ancestor'),
        `should report the unverified merge, got:\n${out}`,
      );

      const events = readEvents(layout.eventLog);
      // Verification still ran on the recovery path.
      assert.ok(
        events.some((e) => e.includes(`git ls-remote origin refs/tags/v${RECOVERY_VERSION}`)),
        `verifyReleaseMerge should still run, got:\n${events.join('\n')}`,
      );
      // But nothing downstream of a failed verification did.
      assert.equal(
        notifierLines(events).length,
        0,
        `no notification may be emitted when verification fails, got:\n${events.join('\n')}`,
      );
      assert.ok(
        !existsSync(layout.closeLog) || readFileSync(layout.closeLog, 'utf-8').trim() === '',
        'no work items may be closed when verification fails',
      );
      assert.ok(
        !events.some((e) => e.startsWith('cleanup ')),
        'no branch cleanup may run when verification fails',
      );
    },
  );

  test(
    'a missing or invalid --finish-release argument exits 13 and executes nothing',
    () => {
      for (const args of [
        ['--finish-release'],                         // missing value
        ['--finish-release', RECOVERY_VERSION, '--nope'], // unknown flag
      ]) {
        const layout = buildLayout({ gitMode: 'success' });
        const res = layout.run(args);
        assert.equal(
          res.status,
          13,
          `expected exit 13 for ${args.join(' ')}, got ${res.status}\n${res.stdout}\n${res.stderr}`,
        );
        assert.deepEqual(
          readEvents(layout.eventLog),
          [],
          `argument errors must be inert; got events for ${args.join(' ')}`,
        );
        assert.ok(
          !existsSync(layout.closeLog) || readFileSync(layout.closeLog, 'utf-8').trim() === '',
          'argument errors must not close work items',
        );
      }
    },
  );

  test(
    're-running --finish-release is idempotent: already-shipped items are skipped and it still exits 0',
    () => {
      const layout = buildLayout({ gitMode: 'success' });

      const first = layout.run(['--finish-release', RECOVERY_VERSION]);
      assert.equal(first.status, 0, `first run should exit 0\n${first.stdout}\n${first.stderr}`);
      const closeLogAfterFirst = readFileSync(layout.closeLog, 'utf-8')
        .split('\n')
        .filter(Boolean);
      assert.equal(closeLogAfterFirst.length, 1, 'the first run closes the item once');

      const second = layout.run(['--finish-release', RECOVERY_VERSION]);
      assert.equal(second.status, 0, `second run should exit 0\n${second.stdout}\n${second.stderr}`);

      // The second run must NOT close (or re-annotate) the already-shipped item.
      const closeLogAfterSecond = readFileSync(layout.closeLog, 'utf-8')
        .split('\n')
        .filter(Boolean);
      assert.equal(
        closeLogAfterSecond.length,
        1,
        `the re-run must not close the already-shipped item again, got:\n${closeLogAfterSecond.join('\n')}`,
      );
      assert.ok(
        `${second.stdout}\n${second.stderr}`.includes('already shipped')
          || `${second.stdout}\n${second.stderr}`.includes('Skipping'),
        'the re-run should report the already-shipped skip',
      );

      // Exactly one notification per run — the re-run's duplicate is the
      // documented, accepted at-most-once behaviour (no dedupe store).
      const events = readEvents(layout.eventLog);
      assert.equal(
        notifierLines(events).length,
        2,
        `expected one notification per run (2 total), got:\n${events.join('\n')}`,
      );
    },
  );
});
