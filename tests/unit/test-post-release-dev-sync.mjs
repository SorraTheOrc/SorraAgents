/**
 * Unit tests for post-release dev sync in run-release.js
 *
 * Tests that after a successful release, dev is synced with main. The sync
 * itself is exercised through an injected command runner, so no live
 * git fetch/checkout/merge/push happens during the unit tests
 * (SA-0MUY6U05F008RG86).
 *
 * Also pins the transport-only retry policy of `syncDevWithMain()`
 * (SA-0MWXMTIM7006760U): transport-class failures are retried with
 * exponential backoff, deterministic failures never are.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const MERGE_SCRIPT_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'release', 'merge-dev-to-main.sh');
const SKILL_MD_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');

// ---------------------------------------------------------------------------
// 1. Module files exist
// ---------------------------------------------------------------------------
test('post-release sync: run-release.js exists', () => {
  assert.ok(
    existsSync(RUN_RELEASE_PATH),
    'run-release.js should exist',
  );
});

test('post-release sync: merge-dev-to-main.sh exists', () => {
  assert.ok(
    existsSync(MERGE_SCRIPT_PATH),
    'merge-dev-to-main.sh should exist',
  );
});

// ---------------------------------------------------------------------------
// 2. run-release.js exports expected functions
// ---------------------------------------------------------------------------
test('post-release sync: run-release.js exports syncDevWithMain', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(
    typeof mod.syncDevWithMain,
    'function',
    'run-release.js should export syncDevWithMain function',
  );
});

test('post-release sync: run-release.js exports parsePRUrl', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(
    typeof mod.parsePRUrl,
    'function',
    'run-release.js should export parsePRUrl function',
  );
});

// ---------------------------------------------------------------------------
// 3. parsePRUrl extracts PR URL from output
// ---------------------------------------------------------------------------
describe('parsePRUrl', () => {
  test('extracts PR URL from "PR created: <url>" format', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const output = 'PR created: https://github.com/owner/repo/pull/123\nSome other text';
    assert.equal(
      mod.parsePRUrl(output),
      'https://github.com/owner/repo/pull/123',
    );
  });

  test('returns null when no PR URL found', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    assert.equal(
      mod.parsePRUrl('No PR was created.'),
      null,
    );
  });

  test('returns null for empty string', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    assert.equal(mod.parsePRUrl(''), null);
  });

  test('extracts PR URL from gh pr create output', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const output = 'https://github.com/owner/repo/pull/456\n';
    assert.equal(
      mod.parsePRUrl(output),
      'https://github.com/owner/repo/pull/456',
    );
  });
});

// ---------------------------------------------------------------------------
// 4. syncDevWithMain — hermetic via an injectable command runner
// ---------------------------------------------------------------------------
describe('syncDevWithMain (hermetic)', () => {
  test('runs fetch -> checkout -> merge -> push via the injected runner', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const commands = [];
    const result = mod.syncDevWithMain({
      run: (cmd) => {
        commands.push(cmd);
        return '';
      },
    });

    assert.equal(result.success, true);
    assert.equal(typeof result.message, 'string');
    assert.ok(result.message.includes('sync'), result.message);
    assert.deepEqual(commands, [
      'git fetch origin --prune',
      'git checkout dev',
      'git merge origin/main',
      'git push origin dev',
    ]);
  });

  test('returns a failure result when the runner throws', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const result = mod.syncDevWithMain({
      run: () => {
        const err = new Error('boom');
        err.stderr = 'fatal: not a git repository';
        throw err;
      },
    });

    assert.equal(result.success, false);
    assert.equal(typeof result.message, 'string');
    assert.match(result.message, /Failed to sync dev with main/);
    assert.match(result.message, /not a git repository/);
  });
});

// ---------------------------------------------------------------------------
// 4b. syncDevWithMain — transport-only retry policy (SA-0MWXMTIM7006760U)
// ---------------------------------------------------------------------------
//
// Red-first harness for the retry loop of `syncDevWithMain()` (implementation:
// SA-0MWXMTQ4Q001N0WJ). The 2026-11-26 release (v0.2.0) landed on `main` while
// the single `git push origin dev` in the post-release tail failed with
// "Connection to github.com closed by remote host"; the whole verified tail was
// then discarded (parent SA-0MWXIY8D2001DGZ0).
//
// Contract pinned here (all driven through the existing injectable `options.run`
// seam plus an injected `options.sleep`, so no test sleeps and no real
// repository is touched):
//
//   * `syncDevWithMain({ attempts, backoffMs, sleep, run })` retries ONLY
//     transport-class failures — an allowlist of connection reset/closed by
//     remote host, `RPC failed`, early EOF, TLS/SSL, proxy and timeout
//     signatures. Anything unrecognised fails closed and is never replayed,
//     because `git merge` is not idempotent.
//   * Defaults: 3 attempts with exponential backoff (500 ms, then 1000 ms).
//   * Every attempt restarts the full command sequence from a fresh
//     `git fetch origin --prune`, so a retried merge never compounds stale
//     state.
//   * The result records the number of attempts made (`result.attempts`) so a
//     caller can report how much retrying happened.
//
// These cases were written red-first (`{ todo: true }`, the `node:test`
// analogue of `pytest.mark.xfail(strict=True)` used by SA-0MW3GCHEL001G8CP)
// and the markers were removed by the retry implementation
// (SA-0MWXMTQ4Q001N0WJ).

const SYNC_SEQUENCE = [
  'git fetch origin --prune',
  'git checkout dev',
  'git merge origin/main',
  'git push origin dev',
];

const DEFAULT_ATTEMPTS = 3;
const DEFAULT_BACKOFF_MS = 500;

const TRANSPORT_SIGNATURES = [
  { label: 'connection closed by remote host', text: 'Connection to github.com closed by remote host.' },
  { label: 'connection reset', text: 'Connection reset by peer' },
  { label: 'RPC failure', text: 'error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502' },
  { label: 'early EOF', text: 'fatal: the remote end hung up unexpectedly\nfatal: early EOF' },
  { label: 'TLS/SSL error', text: "fatal: unable to access 'https://github.com/': SSL_ERROR_SYSCALL in connection to github.com:443" },
  { label: 'proxy error', text: "fatal: unable to access 'https://github.com/': Received HTTP code 407 from proxy after CONNECT" },
  { label: 'timeout', text: 'error: RPC failed; curl 28 Failed to connect to github.com port 443 after 30001 ms: Timeout was reached' },
];

const DETERMINISTIC_FAILURES = [
  {
    label: 'merge conflict',
    text: 'CONFLICT (content): merge conflict in skill/ship/scripts/run-release.js',
    command: 'git merge origin/main',
  },
  {
    label: 'non-fast-forward push rejection',
    text: '! [rejected]        dev -> dev (non-fast-forward)',
    command: 'git push origin dev',
  },
  {
    label: 'missing remote ref',
    text: "fatal: couldn't find remote ref refs/heads/main",
    command: 'git fetch origin --prune',
  },
];

/**
 * Build the error a failing git invocation throws — `syncDevWithMain()` reads
 * `err.stderr` first and falls back to `err.message`.
 *
 * @param {string} text - Command output to expose as the failure text.
 * @returns {Error & { stderr: Buffer }} The injected failure.
 */
function gitFailure(text) {
  const err = new Error(text);
  err.stderr = Buffer.from(text);
  return err;
}

/**
 * Command-logging fake runner factory for `syncDevWithMain()`'s `options.run`
 * seam.
 *
 * @param {Record<string, number>} [failuresByCommand] - Map of command string
 *   to the number of leading invocations that throw `errorText`. Commands
 *   absent from the map always succeed.
 * @param {string} [errorText] - Failure text every injected throw carries.
 * @returns {{ run: (cmd: string) => string, calls: string[] }} The runner and
 *   the recorded command list (one entry per invocation, including retries).
 */
function makeFakeRunner(
  failuresByCommand = {},
  errorText = TRANSPORT_SIGNATURES[0].text,
) {
  const calls = [];
  const remaining = { ...failuresByCommand };
  const run = (cmd) => {
    calls.push(cmd);
    if ((remaining[cmd] || 0) > 0) {
      remaining[cmd] -= 1;
      throw gitFailure(errorText);
    }
    return '';
  };
  return { run, calls };
}

/**
 * Build an injected `options.sleep` that records the requested delays instead
 * of sleeping, so retry backoff assertions run instantly.
 *
 * @returns {{ sleep: (ms: number) => void, sleeps: number[] }} The recorder.
 */
function makeRecordingSleep() {
  const sleeps = [];
  return {
    sleep: (ms) => { sleeps.push(ms); },
    sleeps,
  };
}

describe('syncDevWithMain (retry policy)', () => {
  for (const { label, text } of TRANSPORT_SIGNATURES) {
    test(
      `retries a transport-class failure: ${label}`,
      async () => {
        const mod = await import(RUN_RELEASE_PATH);
        const { run, calls } = makeFakeRunner({ 'git fetch origin --prune': 1 }, text);
        const { sleep, sleeps } = makeRecordingSleep();

        const result = await mod.syncDevWithMain({ run, sleep, backoffMs: 10 });

        assert.equal(result.success, true, `expected a retried success, got: ${result.message}`);
        assert.equal(result.attempts, 2, 'one initial attempt plus one retry');
        assert.deepEqual(sleeps, [10], 'one backoff before the single retry');
        assert.deepEqual(
          calls,
          ['git fetch origin --prune', ...SYNC_SEQUENCE],
          'a failing fetch aborts attempt 1 there; the retry replays the full sequence',
        );
      },
    );
  }

  test(
    'an unrecognised failure is not retried (allowlist: fail closed)',
    async () => {
      const mod = await import(RUN_RELEASE_PATH);
      const { run, calls } = makeFakeRunner(
        { 'git fetch origin --prune': 5 },
        'fatal: something nobody classified happened',
      );
      const { sleep, sleeps } = makeRecordingSleep();

      const result = await mod.syncDevWithMain({ run, sleep });

      assert.equal(result.success, false);
      assert.equal(result.attempts, 1, 'an unclassified failure must never be replayed');
      assert.deepEqual(sleeps, [], 'no backoff without a retry');
      assert.deepEqual(calls, ['git fetch origin --prune']);
    },
  );

  test(
    'retries transport failures then succeeds, recording 3 attempts and increasing delays',
    async () => {
      const mod = await import(RUN_RELEASE_PATH);
      const { run } = makeFakeRunner(
        { 'git fetch origin --prune': 2 },
        TRANSPORT_SIGNATURES[0].text,
      );
      const { sleep, sleeps } = makeRecordingSleep();

      const result = await mod.syncDevWithMain({ run, sleep, backoffMs: 10 });

      assert.equal(result.success, true);
      assert.equal(result.attempts, DEFAULT_ATTEMPTS);
      assert.deepEqual(sleeps, [10, 20], 'backoff doubles between attempts');
      assert.match(result.message, /sync/i);
    },
  );

  test(
    'defaults to 3 attempts with exponential backoff and honours the overrides',
    async () => {
      const mod = await import(RUN_RELEASE_PATH);

      const alwaysFails = makeFakeRunner(
        { 'git push origin dev': 99 },
        TRANSPORT_SIGNATURES[0].text,
      );
      const defaults = makeRecordingSleep();
      const failed = await mod.syncDevWithMain({ run: alwaysFails.run, sleep: defaults.sleep });

      assert.equal(failed.success, false);
      assert.equal(failed.attempts, DEFAULT_ATTEMPTS);
      assert.equal(alwaysFails.calls.length, DEFAULT_ATTEMPTS * SYNC_SEQUENCE.length);
      assert.deepEqual(
        defaults.sleeps,
        [DEFAULT_BACKOFF_MS, DEFAULT_BACKOFF_MS * 2],
        'default backoff is 500 ms doubling per retry',
      );

      const overridden = makeFakeRunner(
        { 'git push origin dev': 99 },
        TRANSPORT_SIGNATURES[0].text,
      );
      const tuned = makeRecordingSleep();
      const stillFailed = await mod.syncDevWithMain({
        run: overridden.run,
        sleep: tuned.sleep,
        attempts: 4,
        backoffMs: 100,
      });

      assert.equal(stillFailed.success, false);
      assert.equal(stillFailed.attempts, 4);
      assert.equal(overridden.calls.length, 4 * SYNC_SEQUENCE.length);
      assert.deepEqual(tuned.sleeps, [100, 200, 400]);
    },
  );

  for (const { label, text, command } of DETERMINISTIC_FAILURES) {
    test(
      `does not retry a deterministic failure: ${label}`,
      async () => {
        const mod = await import(RUN_RELEASE_PATH);
        const { run, calls } = makeFakeRunner({ [command]: 5 }, text);
        const { sleep, sleeps } = makeRecordingSleep();

        const result = await mod.syncDevWithMain({ run, sleep });

        assert.equal(result.success, false);
        assert.equal(result.attempts, 1, 'a deterministic failure is never retried');
        assert.deepEqual(sleeps, [], 'no backoff for a deterministic failure');
        assert.deepEqual(
          calls,
          SYNC_SEQUENCE.slice(0, SYNC_SEQUENCE.indexOf(command) + 1),
          'only the first attempt runs, up to and including the failing command',
        );
        assert.ok(
          result.message.includes(text),
          `failure message should carry the git output, got: ${result.message}`,
        );
      },
    );
  }

  test(
    'each retry restarts the full sequence from a fresh git fetch',
    async () => {
      const mod = await import(RUN_RELEASE_PATH);
      const { run, calls } = makeFakeRunner(
        { 'git push origin dev': 1 },
        TRANSPORT_SIGNATURES[0].text,
      );
      const { sleep, sleeps } = makeRecordingSleep();

      const result = await mod.syncDevWithMain({ run, sleep, backoffMs: 5 });

      assert.equal(result.success, true);
      assert.equal(result.attempts, 2);
      assert.deepEqual(sleeps, [5]);
      assert.deepEqual(
        calls,
        [...SYNC_SEQUENCE, ...SYNC_SEQUENCE],
        'the retry replays fetch → checkout → merge → push, not just the failed push',
      );
    },
  );
});

// ---------------------------------------------------------------------------
// 5. SKILL.md documents the post-release dev sync
// ---------------------------------------------------------------------------
test('post-release sync: SKILL.md documents the dev sync step', () => {
  const content = readFileSync(SKILL_MD_PATH, 'utf-8');

  assert.ok(
    content.includes('syncDevWithMain') ||
    content.includes('syncing dev') ||
    content.includes('Sync dev') ||
    (content.includes('dev') && content.includes('main') && content.includes('sync')),
    'SKILL.md should document the post-release dev sync step',
  );
});

// ---------------------------------------------------------------------------
// 6. merge-dev-to-main.sh has correct structure
// ---------------------------------------------------------------------------
test('post-release sync: merge-dev-to-main.sh is a valid bash script', () => {
  const content = readFileSync(MERGE_SCRIPT_PATH, 'utf-8');

  assert.ok(
    content.startsWith('#!/usr/bin/env bash') ||
    content.startsWith('#!/bin/bash'),
    'merge-dev-to-main.sh should be a valid bash script',
  );

  assert.ok(
    content.includes('gh pr merge') || content.includes('gh pr create'),
    'merge-dev-to-main.sh should handle PR operations',
  );
});

// ---------------------------------------------------------------------------
// 7. Release Process section documents post-release steps
// ---------------------------------------------------------------------------
test('post-release sync: Release Process has post-release steps documented', () => {
  const content = readFileSync(SKILL_MD_PATH, 'utf-8');

  // Should have numbered steps that include syncing dev
  const releaseProcessSection = content.match(/## Release Process[\s\S]*?(?=## |$)/);
  assert.ok(releaseProcessSection, 'SKILL.md should have a Release Process section');

  const sectionContent = releaseProcessSection[0];
  assert.ok(
    sectionContent.includes('dev') &&
    sectionContent.includes('main') &&
    (sectionContent.includes('synced') || sectionContent.includes('up to date') ||
     sectionContent.includes('sync') || sectionContent.includes('return')),
    'Release Process should document that dev is synced with main after release',
  );
});
