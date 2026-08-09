/**
 * Unit tests for verifyReleaseMerge in run-release.js
 *
 * Verifies the merge-verification guard (SA-0MSJ2XMQL006CVQS): work items may
 * only be closed after a dev→main merge is verified — the released version
 * tag must exist on origin AND the tag commit must be an ancestor of
 * origin/main. Without this guard, a close step invoked outside a real
 * release (or after a failed merge) spuriously closes real work items.
 *
 * The guard's command runner is injected, so these tests never touch a real
 * git repository.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Build a fake command runner backed by a behaviour map.
 *
 * Each key is an exact command string; each value is either a string (stdout)
 * or an Error (the command throws, simulating a non-zero exit).
 *
 * @param {Record<string, string | Error>} behavior - Command → stdout/Error map.
 * @returns {{ calls: string[], run: (cmd: string) => string }}
 */
function makeRunner(behavior) {
  const calls = [];
  const run = (cmd) => {
    calls.push(cmd);
    if (behavior[cmd] === undefined) {
      throw new Error(`Unexpected command in verifyReleaseMerge test: ${cmd}`);
    }
    if (behavior[cmd] instanceof Error) {
      throw behavior[cmd];
    }
    return behavior[cmd];
  };
  return { calls, run };
}

// A released-merge scenario: tag v0.2.0 exists on origin and its commit is an
// ancestor of origin/main.
function releasedBehavior() {
  return {
    'git fetch origin --prune': '',
    'git ls-remote origin refs/tags/v0.2.0': 'abcd1234abcd1234abcd1234abcd1234abcd1234\trefs/tags/v0.2.0',
    'git rev-parse --verify v0.2.0^{commit}': 'abcd1234abcd1234abcd1234abcd1234abcd1234',
    'git merge-base --is-ancestor abcd1234abcd1234abcd1234abcd1234abcd1234 origin/main': '',
  };
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('verifyReleaseMerge', () => {
  test('run-release.js exports verifyReleaseMerge', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    assert.equal(typeof mod.verifyReleaseMerge, 'function');
  });

  test('returns failure when no version is provided', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const { calls, run } = makeRunner({});
    const result = mod.verifyReleaseMerge(null, { run });
    assert.equal(result.success, false);
    assert.ok(result.message.includes('No version'));
    assert.equal(calls.length, 0, 'no git commands should run without a version');
  });

  test('returns failure when git fetch fails', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const behavior = { 'git fetch origin --prune': new Error('fetch failed') };
    const result = mod.verifyReleaseMerge('0.2.0', { run: makeRunner(behavior).run });
    assert.equal(result.success, false);
    assert.ok(result.message.includes('fetch'), 'message should mention the fetch failure');
  });

  test('returns failure when the release tag is missing on origin', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const behavior = {
      'git fetch origin --prune': '',
      'git ls-remote origin refs/tags/v0.2.0': '',
    };
    const result = mod.verifyReleaseMerge('0.2.0', { run: makeRunner(behavior).run });
    assert.equal(result.success, false);
    assert.ok(
      result.message.includes('not found on origin'),
      `message should report the missing tag, got: ${result.message}`,
    );
  });

  test('returns failure when the tag exists but is not an ancestor of origin/main', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const behavior = releasedBehavior();
    behavior['git merge-base --is-ancestor abcd1234abcd1234abcd1234abcd1234abcd1234 origin/main'] =
      new Error('not ancestor');
    const result = mod.verifyReleaseMerge('0.2.0', { run: makeRunner(behavior).run });
    assert.equal(result.success, false);
    assert.ok(
      result.message.includes('did not land') || result.message.includes('ancestor'),
      `message should report the unverified merge, got: ${result.message}`,
    );
  });

  test('returns success when the tag exists and is an ancestor of origin/main', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const { calls, run } = makeRunner(releasedBehavior());
    const result = mod.verifyReleaseMerge('0.2.0', { run });
    assert.equal(result.success, true);
    assert.ok(result.message.includes('verified'));
    // The guard must run the full verification sequence.
    assert.deepEqual(calls, [
      'git fetch origin --prune',
      'git ls-remote origin refs/tags/v0.2.0',
      'git rev-parse --verify v0.2.0^{commit}',
      'git merge-base --is-ancestor abcd1234abcd1234abcd1234abcd1234abcd1234 origin/main',
    ]);
  });

  test('tag output without the ref is treated as missing (empty ls-remote)', async () => {
    const mod = await import(RUN_RELEASE_PATH);
    const behavior = {
      'git fetch origin --prune': '',
      // ls-remote succeeds (exit 0) but lists no matching ref.
      'git ls-remote origin refs/tags/v0.2.0': '',
    };
    const result = mod.verifyReleaseMerge('0.2.0', { run: makeRunner(behavior).run });
    assert.equal(result.success, false);
    assert.ok(result.message.includes('not found on origin'));
  });
});
