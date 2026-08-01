/**
 * Unit tests for waitForPRMerge in run-release.js — CI-optional behaviour.
 *
 * The release process must NOT require CI runs:
 *  - If status checks are present on the PR → they must pass (block on failure).
 *  - If no status checks exist (no CI configured) → proceed without a CI gate.
 *
 * The real `gh` CLI is replaced by a fake `gh` on PATH so tests run without
 * a GitHub connection or real network access.
 */

import { test, describe, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  mkdtempSync, writeFileSync, chmodSync, rmSync, readFileSync,
} from 'node:fs';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SKILL_MD_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const MERGE_SCRIPT_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh');

const PR_URL = 'https://github.com/acme/repo/pull/42';

// ── Fake gh / sleep harness ──────────────────────────────────────────────────

let fakeBinDir;
let originalPath;

before(() => {
  fakeBinDir = mkdtempSync(join(tmpdir(), 'fake-gh-'));

  // Fake `gh` — reads a scenario JSON from a response file written by the test.
  writeFileSync(join(fakeBinDir, 'gh'), `#!/bin/bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  cat "${fakeBinDir}/response.json"
elif [[ "$1" == "pr" && "$2" == "merge" ]]; then
  echo "Merged PR #$3"
  exit 0
fi
exit 0
`);
  chmodSync(join(fakeBinDir, 'gh'), 0o755);

  // Fake `sleep` — instant, so poll loops run fast in tests.
  writeFileSync(join(fakeBinDir, 'sleep'), '#!/bin/bash\nexit 0\n');
  chmodSync(join(fakeBinDir, 'sleep'), 0o755);

  originalPath = process.env.PATH;
  process.env.PATH = `${fakeBinDir}:${originalPath}`;
});

after(() => {
  process.env.PATH = originalPath;
  rmSync(fakeBinDir, { recursive: true, force: true });
});

/** Write the statusCheckRollup the fake `gh` will return. */
function setRollup(rollup) {
  writeFileSync(join(fakeBinDir, 'response.json'), JSON.stringify({ statusCheckRollup: rollup }));
}

/** Capture console.log/stdout output while running fn. */
function withCapturedLog(fn) {
  const chunks = [];
  const originalLog = console.log;
  const originalWrite = process.stdout.write;
  console.log = (...args) => { chunks.push(args.join(' ')); };
  process.stdout.write = (s) => { chunks.push(String(s)); return true; };
  try {
    const result = fn();
    return { result, log: chunks.join('\n') };
  } finally {
    console.log = originalLog;
    process.stdout.write = originalWrite;
  }
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('waitForPRMerge — CI is optional', () => {
  test('merges when no status checks exist (no CI configured)', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    setRollup([]);
    const { result, log } = withCapturedLog(() => mod.waitForPRMerge(PR_URL, 2));
    assert.equal(result.success, true, `expected merge success, got: ${result.message}`);
    assert.match(result.message, /merged/i);
    // The log should explicitly note there is no CI gate, not claim "CI passed".
    assert.match(
      log,
      /no ci|no status checks|without ci/i,
      `expected a no-CI notice in log, got:\n${log}`,
    );
  });

  test('merges when all present status checks pass', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    setRollup([
      { status: 'COMPLETED', conclusion: 'SUCCESS' },
      { status: 'COMPLETED', conclusion: 'SUCCESS' },
    ]);
    const { result } = withCapturedLog(() => mod.waitForPRMerge(PR_URL, 2));
    assert.equal(result.success, true, `expected merge success, got: ${result.message}`);
    assert.match(result.message, /merged/i);
  });

  test('blocks when a present status check fails', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    setRollup([
      { status: 'COMPLETED', conclusion: 'SUCCESS' },
      { status: 'COMPLETED', conclusion: 'FAILURE' },
    ]);
    const { result } = withCapturedLog(() => mod.waitForPRMerge(PR_URL, 2));
    assert.equal(result.success, false, 'expected merge to be blocked on failed check');
    assert.match(result.message, /failed/i);
  });

  test('blocks when a present status check is cancelled', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    setRollup([{ status: 'COMPLETED', conclusion: 'CANCELLED' }]);
    const { result } = withCapturedLog(() => mod.waitForPRMerge(PR_URL, 2));
    assert.equal(result.success, false, 'expected merge to be blocked on cancelled check');
    assert.match(result.message, /failed/i);
  });

  test('waits for pending checks to finish before merging', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    // First poll: pending; subsequent polls: completed+success. The fake gh
    // counts polls via a counter file.
    const counterPath = join(fakeBinDir, 'poll-count');
    writeFileSync(counterPath, '0');
    writeFileSync(join(fakeBinDir, 'gh'), `#!/bin/bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  n=$(cat "${counterPath}")
  n=$((n + 1))
  echo "$n" > "${counterPath}"
  if [[ "$n" -eq 1 ]]; then
    echo '{"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": null}]}'
  else
    echo '{"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}'
  fi
elif [[ "$1" == "pr" && "$2" == "merge" ]]; then
  echo "Merged PR #$3"
  exit 0
fi
exit 0
`);
    chmodSync(join(fakeBinDir, 'gh'), 0o755);

    const { result } = withCapturedLog(() => mod.waitForPRMerge(PR_URL, 5));
    assert.equal(result.success, true, `expected merge after pending->pass, got: ${result.message}`);
    assert.match(result.message, /merged/i);
  });
});

// ── SKILL.md / script doc consistency ────────────────────────────────────────

describe('ship skill docs — no hard CI gate', () => {
  test('SKILL.md does not reference dev-full-suite or dev-smoke as gates', () => {
    const content = readFileSync(SKILL_MD_PATH, 'utf8');
    assert.ok(
      !content.includes('dev-full-suite') && !content.includes('dev-smoke'),
      'SKILL.md should not reference removed CI workflows as release gates',
    );
  });

  test('SKILL.md documents that CI checks are optional (present=pass, absent=ignore)', () => {
    const content = readFileSync(SKILL_MD_PATH, 'utf8');
    assert.ok(
      /status checks?/i.test(content),
      'SKILL.md should describe status-check handling',
    );
    assert.ok(
      /no ci|without ci|no status checks|if present|optional/i.test(content),
      'SKILL.md should state CI is optional / not required',
    );
  });

  test('merge-dev-to-main.sh help text does not claim a hard CI gate', () => {
    const content = readFileSync(MERGE_SCRIPT_PATH, 'utf8');
    assert.ok(
      !content.includes('dev-full-suite') && !content.includes('dev-smoke'),
      'merge-dev-to-main.sh should not reference removed CI workflows',
    );
    assert.ok(
      !/proceed even if ci checks are not green/i.test(content),
      'merge-dev-to-main.sh --force help should not claim a hard CI gate',
    );
  });
});
