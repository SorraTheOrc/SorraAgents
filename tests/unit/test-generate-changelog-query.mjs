/**
 * Unit tests for skill/ship/scripts/release/generate-changelog.js
 *
 * Verifies the getCompletedOrInReviewItems() query/projection contract:
 * exactly ONE `wl list --stage in_review --json` invocation piped through jq
 * (SA-0MSLW5P7J0068UFZ, SA-0MSPPHTYA002212R), with a SKINNY projection
 * ({id, title, issueType, parentId}) and an explicit maxBuffer so a large
 * worklog can never overflow execSync's default 1 MB buffer (ENOBUFS).
 * Also covers the lazy, bounded description fetch that replaces the old
 * full-description projection (SA-0MWE4BT63005N8OE).
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
// getCompletedOrInReviewItems — single stage query + jq projection
// ---------------------------------------------------------------------------

describe('getCompletedOrInReviewItems - single stage query + jq projection', () => {
  test('issues exactly one stage-only query', async () => {
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
    assert.ok(calls[0].includes('--stage in_review'), `should filter stage, got: ${calls[0]}`);
    assert.ok(!calls[0].includes('--status completed'), `should drop redundant status filter (completed-minus-done == in_review), got: ${calls[0]}`);
    assert.ok(calls[0].includes('--json'), `should request JSON, got: ${calls[0]}`);
    assert.deepEqual(items, [{
      id: 'SA-1',
      title: 'One',
      issueType: 'feature',
      parentId: null,
    }], 'projection should expose exactly id/title/issueType/parentId (description is fetched lazily)');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('does not overflow execSync buffer when the projected output exceeds 1MB (AC1/AC4)', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    // The PROJECTED JSON itself is >1MB: 200 items with ~6KB titles. Titles
    // ARE projected, so even the skinny projection overflows execSync's
    // default 1MB maxBuffer — only the explicit 64MB maxBuffer (AC1) keeps
    // it alive. A large non-projected description/enrichment is included too,
    // so the test also covers the original full-description overflow shape.
    const workItems = Array.from({ length: 200 }, (_, i) => ({
      id: `SA-LARGE-${i}`,
      title: `Large item ${i} ${'z'.repeat(6000)}`,
      issueType: 'bug',
      description: 'd'.repeat(6000),
      enrichment: 'z'.repeat(6000),
      tags: ['a', 'b', 'c'],
    }));
    const binDir = createWlMock({ workItems, argsLogPath });

    const items = withWlMock(binDir, () => mod.getCompletedOrInReviewItems());

    assert.equal(items.length, 200, 'all items should be returned without ENOBUFS');
    for (const item of items) {
      assert.deepEqual(
        Object.keys(item).sort(),
        ['id', 'issueType', 'parentId', 'title'],
        'projection is skinny — description/enrichment/tags never enter the buffer',
      );
    }
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns [] and surfaces the real error (not a misleading "wl not available?")', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlMock({ workItems: [], argsLogPath, fail: true });

    const messages = [];
    const original = console.error;
    console.error = (...args) => messages.push(args.join(' '));
    let items;
    try {
      items = withWlMock(binDir, () => mod.getCompletedOrInReviewItems());
    } finally {
      console.error = original;
    }

    assert.deepEqual(items, [], 'failed query should yield no items');
    const logged = messages.join('\n');
    assert.ok(/could not query in_review work items/i.test(logged), `should log an actionable query warning, got: ${logged}`);
    assert.ok(/Command failed|exit|code|ENOBUFS|ETIMEDOUT/i.test(logged), `should surface the underlying error detail, got: ${logged}`);
    assert.ok(!/wl not available\?/i.test(logged), 'should not emit the old misleading message');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // Regression for dash: getCompletedOrInReviewItems must invoke the query
  // via 'bash -c' so that 'set -o pipefail' does not fail on dash systems
  // (where /bin/sh → dash).  The mock 'wl' only emits its payload when its
  // parent process is bash — if the outer shell is dash the mock exits 1,
  // and getCompletedOrInReviewItems() should return [].
  // (LP-0MSQ0NTMO00577UJ)
  test('wraps execSync command in bash -c for dash compatibility', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const payloadPath = join(tmpDir, 'payload.json');
    writeFileSync(payloadPath, JSON.stringify({
      success: true,
      workItems: [{
        id: 'SA-1', title: 'Dash-safe', issueType: 'task',
        description: 'desc', parentId: null,
      }],
    }));
    const wlPath = join(tmpDir, 'wl');
    // This script checks its parent process name; it only emits payload
    // when the parent is bash (i.e. the query runs under bash -c).
    const script = `#!/usr/bin/env bash\n` +
      `parent=$(ps -o comm= -p $PPID 2>/dev/null)\n` +
      `case "$parent" in *bash*) cat "${payloadPath}" ;; *) exit 1 ;; esac\n`;
    writeFileSync(wlPath, script, { mode: 0o755 });

    const items = withWlMock(tmpDir, () => mod.getCompletedOrInReviewItems());

    assert.deepEqual(items, [{
      id: 'SA-1', title: 'Dash-safe', issueType: 'task',
      parentId: null,
    }], 'query must run under bash so set -o pipefail works on dash');
    rmSync(tmpDir, { recursive: true, force: true });
  });
});

// ---------------------------------------------------------------------------
// Lazy description fetch — bounded, per-item, degrades gracefully (AC2)
// ---------------------------------------------------------------------------

/**
 * Create a fake `wl` that answers `wl show <id> --json` from a map of
 * descriptions, recording each invocation.
 *
 * @param {object} opts
 * @param {Record<string,string>} opts.descriptions - id → description.
 * @param {string} opts.argsLogPath - File where the fake wl appends its args.
 * @param {string[]} [opts.failIds] - ids whose `wl show` exits 1.
 * @returns {string} The temp bin dir to prepend to PATH.
 */
function createWlShowMock({ descriptions, argsLogPath, failIds = [] }) {
  const binDir = mkdtempSync(join(tmpdir(), 'fakeshow-'));
  for (const [id, description] of Object.entries(descriptions)) {
    writeFileSync(
      join(binDir, `${id}.json`),
      JSON.stringify({ success: true, workItem: { id, description } }),
      'utf-8',
    );
  }
  const wlPath = join(binDir, 'wl');
  const failPattern = failIds.length ? failIds.join('|') : '___never___';
  const script = `#!/usr/bin/env bash\n`
    + `echo "$@" >> "${argsLogPath}"\n`
    + `id="$2"\n`
    + `case "$id" in\n`
    + `  ${failPattern}) echo "boom" >&2; exit 1 ;;\n`
    + `  *) if [ -f "${binDir}/$id.json" ]; then cat "${binDir}/$id.json"; else echo '{"success":false}'; fi ;;\n`
    + `esac\n`;
  writeFileSync(wlPath, script, { mode: 0o755 });
  return binDir;
}

describe('lazy description fetch (AC2)', () => {
  test('fetchItemDescription returns the description via a single bounded wl show', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlShowMock({
      descriptions: { 'SA-DESC-1': 'Fetched description' },
      argsLogPath,
    });

    const desc = withWlMock(binDir, () => mod.fetchItemDescription('SA-DESC-1'));

    assert.equal(desc, 'Fetched description');
    const calls = readFileSync(argsLogPath, 'utf-8').trim().split('\n').filter(Boolean);
    assert.deepEqual(calls, ['show SA-DESC-1 --json'], 'should query a single item via wl show');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('fetchItemDescription degrades to "" and logs on failure', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlShowMock({ descriptions: {}, argsLogPath, failIds: ['SA-BOOM'] });

    const messages = [];
    const original = console.error;
    console.error = (...args) => messages.push(args.join(' '));
    let desc;
    try {
      desc = withWlMock(binDir, () => mod.fetchItemDescription('SA-BOOM'));
    } finally {
      console.error = original;
    }

    assert.equal(desc, '', 'failed fetch should degrade to an empty description');
    assert.ok(/could not fetch description for SA-BOOM/i.test(messages.join('\n')), 'failure should be logged');
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('fetchItemDescription rejects non-work-item-shaped ids without a subprocess', async () => {
    const mod = await import(MODULE_PATH);
    const messages = [];
    const original = console.error;
    console.error = (...args) => messages.push(args.join(' '));
    let desc;
    try {
      desc = mod.fetchItemDescription('bad; rm -rf /');
    } finally {
      console.error = original;
    }
    assert.equal(desc, '');
    assert.ok(/invalid work item id/i.test(messages.join('\n')), 'invalid id should be rejected and logged');
  });

  test('enrichWithDescriptions fetches only when a description is missing', async () => {
    const mod = await import(MODULE_PATH);
    const tmpDir = mkdtempSync(join(tmpdir(), 'wlargs-'));
    const argsLogPath = join(tmpDir, 'args.log');
    const binDir = createWlShowMock({
      descriptions: { 'SA-NEED': 'Fetched' },
      argsLogPath,
    });
    const items = [
      { id: 'SA-HAVE', title: 'Has', issueType: 'task', description: 'existing', parentId: null },
      { id: 'SA-NEED', title: 'Needs', issueType: 'task', parentId: null },
    ];

    const enriched = withWlMock(binDir, () => mod.enrichWithDescriptions(items));

    assert.equal(enriched[0].description, 'existing', 'pre-enriched items are untouched');
    assert.equal(enriched[1].description, 'Fetched', 'missing descriptions are fetched');
    const calls = readFileSync(argsLogPath, 'utf-8').trim().split('\n').filter(Boolean);
    assert.deepEqual(calls, ['show SA-NEED --json'], 'only the item missing a description should be fetched');
    rmSync(tmpDir, { recursive: true, force: true });
  });
});
