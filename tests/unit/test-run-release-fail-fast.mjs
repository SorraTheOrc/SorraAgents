/**
 * Regression tests for the ship release wrapper's fail-fast argument handling
 * and newly-created-release-tag gating (SA-0MV0PZYMI004SUFR).
 *
 * The 2026-10-09 spurious-close incident: `run-release.js --help` forwarded
 * `--help` to the canonical merge script (which printed usage and exited 0
 * without merging), then stepped through the post-release tail, read the
 * newest *reachable* tag (`v0.1.18`) via `git describe`, verified that already
 * shipped tag as if it were this run's release, and closed 98 work items.
 *
 * These tests pin the two fixes:
 *
 *  1. `--help`/`-h` print wrapper usage and exit 0 with zero side effects
 *     (no gate, merge, `wl`, git-mutation, notification, or close commands).
 *  2. An unrecognised flag exits non-zero with usage and is never forwarded
 *     to the merge script.
 *  3. Post-release steps are refused unless the released tag was newly
 *     created by this run and is an ancestor of `origin/main`.
 *
 * Layout mirrors `test-run-release-merge-guard.mjs`: the real wrapper is
 * copied into a temp skill layout with mocked `git`, `wl` and `gh`, plus a
 * recording merge script that exits 0 without producing a tag.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { parseReleaseArgs, WRAPPER_USAGE } from '../../skill/ship/scripts/run-release.js';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SKILL_SCRIPTS = join(REPO_ROOT, 'skill', 'ship', 'scripts');

const DEP_FILES = [
  'check-unmerged-branches.js',
  'check-audit-gate.js',
  'audit-freshness.js',
  'audit-remediation.js',
  'refresh-audits.js',
  'check-final-validation.js',
  'check-critical-items.js',
  'check-worklog-refs.js',
  'discord-notify.js',
  'timing.js',
  'llm.js',
];

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Build a temp skill layout with the real wrapper + deps, a recording merge
 * script, and mocked `git`/`wl`/`gh` binaries that log their invocations.
 *
 * The git mock reports `$GIT_TAGS` for `git tag --list`, `$GIT_DESCRIBE` for
 * `git describe`, and fails `git tag --list` when `$GIT_TAG_LIST_FAIL` is set.
 *
 * @returns {{ tmpDir: string, runReleasePath: string, logs: object }}
 */
function buildLayout() {
  const tmpDir = mkdtempSync(join(tmpdir(), 'run-release-failfast-'));
  const skillScriptDir = join(tmpDir, 'skill', 'ship', 'scripts');
  mkdirSync(skillScriptDir, { recursive: true });

  writeFileSync(join(skillScriptDir, 'run-release.js'), readFileSync(join(SKILL_SCRIPTS, 'run-release.js'), 'utf8'));
  for (const dep of DEP_FILES) {
    writeFileSync(join(skillScriptDir, dep), readFileSync(join(SKILL_SCRIPTS, dep), 'utf8'));
  }

  const releaseDir = join(skillScriptDir, 'release');
  mkdirSync(releaseDir, { recursive: true });
  writeFileSync(
    join(releaseDir, 'merge-dev-to-main.sh'),
    '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$MERGE_LOG"\nexit 0\n',
  );

  const logs = {
    git: join(tmpDir, 'git.log'),
    wl: join(tmpDir, 'wl.log'),
    close: join(tmpDir, 'close.log'),
    merge: join(tmpDir, 'merge.log'),
    notify: join(tmpDir, 'notify.log'),
  };

  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });

  writeFileSync(join(binDir, 'git'), `#!/usr/bin/env bash
printf 'git %s\\n' "$*" >> "$GIT_LOG"
case "$1" in
  rev-parse)
    case "$2" in
      --show-toplevel) printf '%s\\n' "$MOCK_TOPLVL" ;;
      --verify) printf 'abcd1234abcd1234abcd1234abcd1234abcd1234\\n' ;;
      *) printf '%s\\n' "$MOCK_TOPLVL" ;;
    esac
    ;;
  describe) printf '%s\\n' "$GIT_DESCRIBE" ;;
  tag)
    if [ -n "$GIT_TAG_LIST_FAIL" ]; then exit 1; fi
    printf '%s\\n' "$GIT_TAGS"
    ;;
  ls-remote) printf 'abcd1234abcd1234abcd1234abcd1234abcd1234\\trefs/tags/%s\\n' "$GIT_DESCRIBE" ;;
  fetch|checkout|merge|push) exit 0 ;;
  merge-base) exit 0 ;;
esac
exit 0
`, { mode: 0o755 });

  writeFileSync(join(binDir, 'wl'), `#!/usr/bin/env bash
printf 'wl %s\\n' "$*" >> "$WL_LOG"
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      printf '{"success":true,"workItems":[{"id":"SA-PARENT1","title":"Parent One","needsProducerReview":false}]}\\n'
    else
      printf '{"success":true,"workItems":[]}\\n'
    fi
    ;;
  close)
    printf '%s\\n' "$*" >> "$WL_CLOSE_LOG"
    printf '{"success":true}\\n'
    ;;
  *) printf '{"success":true}\\n' ;;
esac
`, { mode: 0o755 });

  writeFileSync(join(binDir, 'gh'), '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });

  return { tmpDir, runReleasePath: join(skillScriptDir, 'run-release.js'), binDir, logs };
}

/**
 * Run the wrapper in a freshly built temp layout.
 *
 * @param {string[]} args - CLI args passed to run-release.js.
 * @param {object} [opts]
 * @param {string} [opts.gitTags] - Newline-separated `git tag --list` output.
 * @param {string} [opts.gitDescribe] - `git describe --tags --abbrev=0` output.
 * @param {boolean} [opts.tagListFails] - Make `git tag --list` exit non-zero.
 * @returns {{ status: number|null, stdout: string, stderr: string, read: Function }}
 */
function runWrapper(args, opts = {}) {
  const { tmpDir, runReleasePath, binDir, logs } = buildLayout();
  const res = spawnSync(process.execPath, [runReleasePath, ...args], {
    cwd: tmpDir,
    encoding: 'utf-8',
    timeout: 30000,
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH}`,
      MOCK_TOPLVL: tmpDir,
      GIT_LOG: logs.git,
      WL_LOG: logs.wl,
      WL_CLOSE_LOG: logs.close,
      MERGE_LOG: logs.merge,
      GIT_DESCRIBE: opts.gitDescribe || 'v0.1.18',
      GIT_TAGS: opts.gitTags || '',
      GIT_TAG_LIST_FAIL: opts.tagListFails ? '1' : '',
    },
  });
  const read = (path) => (existsSync(path) ? readFileSync(path, 'utf-8').trim() : '');
  return {
    status: res.status,
    stdout: res.stdout || '',
    stderr: res.stderr || '',
    readGit: () => read(logs.git),
    readWl: () => read(logs.wl),
    readClose: () => read(logs.close),
    readMerge: () => read(logs.merge),
  };
}

const combined = (res) => `${res.stdout}\n${res.stderr}`;

// ── Unit tests: parseReleaseArgs ─────────────────────────────────────────────

describe('run-release: parseReleaseArgs', () => {
  test('recognises --help and -h as the help action', () => {
    assert.deepEqual(parseReleaseArgs(['--help']), { action: 'help' });
    assert.deepEqual(parseReleaseArgs(['-h']), { action: 'help' });
  });

  test('rejects an unrecognised flag', () => {
    const parsed = parseReleaseArgs(['--nope']);
    assert.equal(parsed.action, 'error');
    assert.match(parsed.message, /--nope/);
  });

  test('rejects an unrecognised flag even when a known flag is present', () => {
    assert.equal(parseReleaseArgs(['--skip-checks', '--nope']).action, 'error');
  });

  test('parses the documented wrapper flags', () => {
    const parsed = parseReleaseArgs([
      '--dry-run',
      '--force',
      '--skip-checks',
      '--refresh-audits',
      '--skip-audit-remediation',
      '--work-item-id',
      'SA-123',
      '--bump',
      'minor',
    ]);
    assert.equal(parsed.action, 'run');
    assert.deepEqual(parsed.flags, {
      dryRun: true,
      force: true,
      skipChecks: true,
      refreshAudits: true,
      skipAuditRemediation: true,
      workItemId: 'SA-123',
      bump: 'minor',
    });
  });

  test('defaults every flag to false/null for the no-arg release path', () => {
    const parsed = parseReleaseArgs([]);
    assert.equal(parsed.action, 'run');
    assert.deepEqual(parsed.flags, {
      dryRun: false,
      force: false,
      skipChecks: false,
      refreshAudits: false,
      skipAuditRemediation: false,
      workItemId: null,
      bump: null,
    });
  });

  test('rejects a value flag with a missing value', () => {
    const parsed = parseReleaseArgs(['--bump']);
    assert.equal(parsed.action, 'error');
    assert.match(parsed.message, /--bump/);
  });

  test('exports a non-empty usage string', () => {
    assert.equal(typeof WRAPPER_USAGE, 'string');
    assert.match(WRAPPER_USAGE, /--help/);
  });
});

// ── Integration: --help / -h make no side effects ────────────────────────────

describe('run-release: --help is side-effect free', () => {
  test('--help exits 0, prints usage, and touches nothing', () => {
    const res = runWrapper(['--help']);
    assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${combined(res)}`);
    assert.match(res.stdout, /Usage:/, `expected usage on stdout, got:\n${combined(res)}`);
    assert.equal(res.readMerge(), '', 'merge script must not be invoked');
    assert.equal(res.readWl(), '', 'no wl command may run');
    assert.equal(res.readClose(), '', 'no close command may run');
    assert.equal(res.readGit(), '', 'no git command may run');
  });

  test('-h behaves identically to --help', () => {
    const res = runWrapper(['-h']);
    assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${combined(res)}`);
    assert.match(res.stdout, /Usage:/, `expected usage on stdout, got:\n${combined(res)}`);
    assert.equal(res.readMerge(), '', 'merge script must not be invoked');
    assert.equal(res.readWl(), '', 'no wl command may run');
    assert.equal(res.readGit(), '', 'no git command may run');
  });

  test('--help wins even alongside a documented flag', () => {
    const res = runWrapper(['--skip-checks', '--help']);
    assert.equal(res.status, 0, `expected exit 0, got ${res.status}\n${combined(res)}`);
    assert.equal(res.readMerge(), '', 'merge script must not be invoked');
    assert.equal(res.readWl(), '', 'no wl command may run');
  });
});

// ── Integration: unknown flag fails fast ─────────────────────────────────────

describe('run-release: unrecognised flag fails fast', () => {
  test('a bogus flag exits non-zero with usage and no side effects', () => {
    const res = runWrapper(['--nope']);
    assert.notEqual(res.status, 0, 'bogus flag must exit non-zero');
    assert.match(combined(res), /Unknown argument: --nope/, `expected unknown-arg diagnostic, got:\n${combined(res)}`);
    assert.match(combined(res), /Usage:/, `expected usage text, got:\n${combined(res)}`);
    assert.equal(res.readMerge(), '', 'bogus flag must never reach the merge script');
    assert.equal(res.readWl(), '', 'no wl command may run for a bogus flag');
    assert.equal(res.readGit(), '', 'no git command may run for a bogus flag');
  });

  test('a bogus flag is not forwarded even with --skip-checks', () => {
    const res = runWrapper(['--skip-checks', '--bogus']);
    assert.notEqual(res.status, 0, 'bogus flag must exit non-zero');
    assert.equal(res.readMerge(), '', 'bogus flag must never reach the merge script');
  });
});

// ── Integration: newly-created-tag gating ────────────────────────────────────

describe('run-release: post-release gated on a newly created tag', () => {
  test('refuses post-release steps when the resolved tag already existed', () => {
    const res = runWrapper(['--skip-checks'], {
      gitTags: 'v0.1.18\nv0.1.17',
      gitDescribe: 'v0.1.18',
    });
    assert.notEqual(res.status, 0, 'stale tag must not exit 0');
    assert.match(
      combined(res),
      /already existed|newly created|no new release/i,
      `expected a newly-created-tag diagnostic, got:\n${combined(res)}`,
    );
    assert.equal(res.readClose(), '', 'no work items may be closed against a stale tag');
  });

  test('proceeds when a genuinely new tag is present but older tags exist', () => {
    const res = runWrapper(['--skip-checks'], {
      gitTags: 'v0.1.18\nv0.1.17',
      gitDescribe: 'v0.2.0',
    });
    assert.equal(res.status, 0, `expected a clean exit, got ${res.status}\n${combined(res)}`);
    assert.doesNotMatch(combined(res), /already existed|no new release/i);
  });

  test('fails closed when the pre-run tag snapshot is unavailable', () => {
    const res = runWrapper(['--skip-checks'], {
      gitTags: '',
      gitDescribe: 'v0.2.0',
      tagListFails: true,
    });
    assert.notEqual(res.status, 0, 'an unavailable tag snapshot must not exit 0');
    assert.equal(res.readClose(), '', 'no work items may be closed without a tag snapshot');
  });
});
