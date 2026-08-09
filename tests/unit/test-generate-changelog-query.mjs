/**
 * Unit tests for skill/ship/scripts/release/generate-changelog.js
 *
 * Verifies getCompletedOrInReviewItems() query/projection contract:
 * exactly ONE `wl list --status completed --stage in_review --json`
 * invocation piped through jq (SA-0MSLW5P7J0068UFZ), so large worklogs
 * cannot overflow execSync's default 1 MB buffer (ENOBUFS).
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(
  REPO_ROOT,
  'skill', 'ship', 'scripts', 'release', 'generate-changelog.js',
);

// ---------------------------------------------------------------------------
// Mock wl on PATH (shared helpers)
// ---------------------------------------------------------------------------

/**
 * Create a fake `wl` executable on a temp PATH that records its arguments
 * and emits a canned workItems payload (or fails).
 *
 * @param {object} opts
 * @param {Array<object>} opts.workItems - Items for the fake `wl list` output.
 * @param {string} opts.argsLogPath - File where the fake wl appends its args.
 * @param {boolean} [opts.fail] - When true the fake wl exits 1.
 * @returns {string} The temp bin dir to prepend to PATH.
 */
function createWlMock({ workItems, argsLogPath, fail = false }) {
  const binDir = mkdtempSync(join(tmpdir(), 'fakebin-'));
  const payloadPath = join(binDir, 'payload.json');
  writeFileSync(payloadPath, JSON.stringify({ success: true, workItems }), 'utf-8');
  const wlPath = join(binDir, 'wl');
  const script = `#!/usr/bin/env bash\n`
    + `echo "$@" >> "${argsLogPath}"\n`
    + (fail ? 'exit 1\n' : `cat "${payloadPath}"\n`);
  writeFileSync(wlPath, script, { mode: 0o755 });
  return binDir;
}

/**
 * Run *fn* with *binDir* prepended to PATH, restoring PATH afterwards.
 */
function withWlMock(binDir, fn) {
  const savedPath = process.env.PATH;
  process.env.PATH = `${binDir}:${savedPath}`;
  try {
    return fn();
  } finally {
    process.env.PATH = savedPath;
    rmSync(binDir, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------
// getCompletedOrInReviewItems — single AND query + jq projection
// ---------------------------------------------------------------------------

describe('getCompletedOrInReviewItems - single AND query + jq projection', () => {
  test('issues exactly one combined query (status + stage)', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const workItems = [{
      id: 'SA-1',
      title: 'One',
      issueType: 'feature',
      description: 'desc one',
      extra: 'should not appear',
    }];
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCompletedOrInReviewItems());
    const calls = readFileSync(argsLogPath, 'utf-8').trim().split('\n').filter(Boolean);

    assert.equal(calls.length, 1, 'should be exactly one wl list invocation');
    assert.ok(calls[0].includes('--status completed'), `should filter status, got: ${calls[0]}`);
    assert.ok(calls[0].includes('--stage in_review'), `should filter stage, got: ${calls[0]}`);
    assert.ok(calls[0].includes('--json'), `should request JSON, got: ${calls[0]}`);
    assert.deepEqual(items, [{
      id: 'SA-1',
      title: 'One',
      issueType: 'feature',
      description: 'desc one',
    }], 'projection should expose exactly id/title/issueType/description');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('does not overflow execSync buffer with >1MB wl output', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    // ~200 items x ~6KB of audit enrichment (NOT projected) ≈ 1.2MB raw
    // output — over execSync's default 1MB maxBuffer. jq drops the
    // enrichment so only the 4 projected fields enter Node's buffer.
    const workItems = Array.from({ length: 200 }, (_, i) => ({
      id: `SA-LARGE-${i}`,
      title: `Large item ${i}`,
      issueType: 'bug',
      description: `description of item ${i}`,
      enrichment: 'z'.repeat(6000),
      tags: ['a', 'b', 'c'],
    }));
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCompletedOrInReviewItems());

    assert.equal(items.length, 200, 'all items should be returned without ENOBUFS');
    for (const item of items) {
      assert.ok('id' in item && 'title' in item && 'issueType' in item && 'description' in item);
      assert.ok(!('enrichment' in item) && !('tags' in item), 'non-projected fields dropped');
    }
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns [] and logs a warning when wl query fails', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlMock({ workItems: [], argsLogPath, fail: true });

    const items = withWlMock(binDir, () => mod.getCompletedOrInReviewItems());

    assert.deepEqual(items, [], 'failed query should yield no items');
    rmSync(tmpDir, { recursive: true, force: true });
  });
});
