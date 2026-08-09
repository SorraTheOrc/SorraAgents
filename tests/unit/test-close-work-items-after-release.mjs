/**
 * Unit tests for close-work-items-after-release in run-release.js
 *
 * Tests that after a successful release, work items are closed
 * with the reason "Shipped in v<version>".
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SKILL_MD_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const RELEASE_PROCESS_PATH = join(REPO_ROOT, 'docs', 'dev', 'release-process.md');

// ---------------------------------------------------------------------------
// 1. Module file exists and exports expected functions
// ---------------------------------------------------------------------------
test('close-work-items: run-release.js exists', () => {
  assert.ok(
    existsSync(RUN_RELEASE_PATH),
    'run-release.js should exist',
  );
});

test('close-work-items: run-release.js exports closeWorkItemsAfterRelease', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  assert.equal(
    typeof mod.closeWorkItemsAfterRelease,
    'function',
    'run-release.js should export closeWorkItemsAfterRelease function',
  );
});

test('close-work-items: run-release.js imports getCandidateItems from check-audit-gate.js', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  assert.ok(
    content.includes('getCandidateItems'),
    'run-release.js should import getCandidateItems from check-audit-gate.js',
  );
});



// ---------------------------------------------------------------------------
// 3. closeWorkItemsAfterRelease returns expected structure
// ---------------------------------------------------------------------------
// Test-isolation note (SA-0MSJ2XMQL006CVQS): these tests MUST NOT invoke
// closeWorkItemsAfterRelease with the default worklog boundary — the buggy
// versions called it with hardcoded versions ('1.0.0'/'1.2.3') against the
// LIVE worklog, spuriously closing ~368 real work items. Every call below
// injects fake candidate/close functions so the suite never mutates the
// production worklog.

test('close-work-items: closeWorkItemsAfterRelease returns expected structure', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease('1.0.0', {
    getCandidateItemsFn: () => [],
    runCloseCommand: () => {},
  });

  assert.ok(typeof result === 'object');
  assert.ok('success' in result);
  assert.ok('message' in result);
  assert.equal(typeof result.success, 'boolean');
  assert.equal(typeof result.message, 'string');
});

// ---------------------------------------------------------------------------
// 4. closeWorkItemsAfterRelease accepts version string (empty candidate set)
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease accepts a version string', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  // With empty candidate set (no items to close), it should succeed gracefully.
  // Candidate query is injected so the live worklog is never touched.
  const result = mod.closeWorkItemsAfterRelease('1.2.3', {
    getCandidateItemsFn: () => [],
    runCloseCommand: () => {},
  });

  assert.equal(
    typeof result.message,
    'string',
    'closeWorkItemsAfterRelease should return a message string',
  );
  // Empty candidate set should say "no items" or similar
  assert.ok(
    result.message.includes('No work items') ||
    result.message.includes('no candidate') ||
    result.message.includes('no items'),
    'closeWorkItemsAfterRelease should handle empty candidate set gracefully',
  );
  assert.equal(result.closedCount, 0, 'no items should be closed');
});

// ---------------------------------------------------------------------------
// 4b. closeWorkItemsAfterRelease closes candidates via the injected close fn
// ---------------------------------------------------------------------------
test('close-work-items: closes only needsProducerReview=false candidates', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const closed = [];
  const skipped = [];
  const result = mod.closeWorkItemsAfterRelease('0.2.0', {
    getCandidateItemsFn: () => [
      { id: 'SA-CLOSE1', title: 'Close Me', needsProducerReview: false },
      { id: 'SA-CLOSE2', title: 'Close Me Too', needsProducerReview: false },
      { id: 'SA-SKIP1', title: 'Review Me', needsProducerReview: true },
      { id: 'SA-SKIP2', title: 'Unknown Review', needsProducerReview: null },
    ],
    runCloseCommand: (itemId, reason) => {
      closed.push({ itemId, reason });
    },
  });

  assert.deepEqual(
    closed.map((c) => c.itemId).sort(),
    ['SA-CLOSE1', 'SA-CLOSE2'],
    'only needsProducerReview=false items should be closed',
  );
  assert.ok(
    closed.every((c) => c.reason === 'Shipped in v0.2.0'),
    'close reason should be "Shipped in v<version>"',
  );
  assert.equal(result.closedCount, 2);
  assert.equal(result.skippedCount, 2);
  assert.deepEqual(
    result.skippedItems.map((s) => s.id).sort(),
    ['SA-SKIP1', 'SA-SKIP2'],
    'skipped items should be reported',
  );
});

// ---------------------------------------------------------------------------
// 4c. closeWorkItemsAfterRelease reports individual close failures
// ---------------------------------------------------------------------------
test('close-work-items: reports close failures without aborting the sweep', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease('0.2.0', {
    getCandidateItemsFn: () => [
      { id: 'SA-OK', title: 'OK', needsProducerReview: false },
      { id: 'SA-FAIL', title: 'Fails', needsProducerReview: false },
    ],
    runCloseCommand: (itemId) => {
      if (itemId === 'SA-FAIL') {
        throw new Error('wl close failed');
      }
    },
  });

  assert.equal(result.closedCount, 1);
  assert.equal(result.errorCount, 1);
  assert.equal(result.success, false);
  assert.ok(result.message.includes('SA-FAIL'), 'message should mention the failing item');
});

// ---------------------------------------------------------------------------
// 5. closeWorkItemsAfterRelease handles missing version gracefully
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease handles missing version', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease(null, {
    getCandidateItemsFn: () => [{ id: 'SA-X', title: 'X', needsProducerReview: false }],
    runCloseCommand: () => { throw new Error('should not be called'); },
  });

  assert.ok(
    result.message.includes('No version'),
    'closeWorkItemsAfterRelease should report when no version is provided',
  );
  assert.equal(result.closedCount, 0, 'no items should be closed without a version');
});

// ---------------------------------------------------------------------------
// 6. SKILL.md documents the close work items step
// ---------------------------------------------------------------------------
test('close-work-items: SKILL.md documents the close work items step', () => {
  const content = readFileSync(SKILL_MD_PATH, 'utf-8');

  assert.ok(
    (content.includes('close') && content.includes('work item') &&
     (content.includes('release') || content.includes('Shipped'))) ||
    content.includes('Close work items') ||
    content.includes('items are closed') ||
    content.includes('work items are automatically closed'),
    'SKILL.md should document the close-work-items step after release',
  );
});

// ---------------------------------------------------------------------------
// 7. docs/dev/release-process.md documents auto-close
// ---------------------------------------------------------------------------
test('close-work-items: release-process.md documents auto-close after release', () => {
  assert.ok(
    existsSync(RELEASE_PROCESS_PATH),
    'docs/dev/release-process.md should exist',
  );

  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');

  assert.ok(
    content.includes('close') ||
    content.includes('closed') ||
    content.includes('closing'),
    'release-process.md should mention closing work items after release',
  );
});

// ---------------------------------------------------------------------------
// 8. Release Process docs are updated with auto-close mention
// ---------------------------------------------------------------------------
test('close-work-items: Post-merge section mentions auto-close', () => {
  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');

  // Should mention auto-closing somewhere in the document
  assert.ok(
    content.includes('auto-clos') ||
    content.includes('automatically closed') ||
    content.includes('work items are closed'),
    'release-process.md should mention that work items are automatically closed',
  );
});

// ---------------------------------------------------------------------------
// F3: Filter closeWorkItemsAfterRelease by needsProducerReview
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease filters by needsProducerReview=false', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // Verify the function checks needsProducerReview before closing
  assert.ok(
    content.includes('needsProducerReview'),
    'closeWorkItemsAfterRelease should reference needsProducerReview for filtering',
  );
});

test('close-work-items: closeWorkItemsAfterRelease logs skipped items', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  // Find the closeWorkItemsAfterRelease function body
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  // Should mention skipping items
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skip') || fnBody.includes('Skip'),
    'closeWorkItemsAfterRelease should log skipped items',
  );
});

test('close-work-items: return value includes skippedCount', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skippedCount'),
    'closeWorkItemsAfterRelease return value should include skippedCount',
  );
});

test('close-work-items: return value includes skippedItems', () => {
  const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
  const fnStart = content.indexOf('export function closeWorkItemsAfterRelease');
  assert.ok(fnStart >= 0, 'Should find closeWorkItemsAfterRelease function');
  const fnBody = content.slice(fnStart);
  assert.ok(
    fnBody.includes('skippedItems'),
    'closeWorkItemsAfterRelease return value should include skippedItems',
  );
});

test('close-work-items: release-process.md mentions producer-review filtering', () => {
  const content = readFileSync(RELEASE_PROCESS_PATH, 'utf-8');
  assert.ok(
    content.includes('needsProducerReview') ||
    content.includes('producer review') ||
    content.includes('Producer review'),
    'release-process.md should mention producer-review filtering in close step',
  );
});

// ---------------------------------------------------------------------------
// F4: closeWorkItemsAfterRelease force-closes candidates (AC3)
// ---------------------------------------------------------------------------
// The audit gate (check-audit-gate.js) already verified audit readiness for
// every candidate before the release proceeds. The close step therefore uses
// `wl close --force` so a parent is closed even when a descendant is stuck in
// a non-terminal state (e.g. left at in_progress by a crashed audit) — the
// release close completes in a single pass instead of leaving items dangling.

import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

test('close-work-items: wl close is invoked with --force for candidates', () => {
  const tmpDir = mkdtempSync(join(tmpdir(), 'close-work-items-force-test-'));

  // Mock `wl` on PATH: answers the candidate queries used by
  // getCandidateItems() and records every `wl close` invocation.
  const closeLog = join(tmpDir, 'close.log');
  const binDir = join(tmpDir, 'bin');
  mkdirSync(binDir, { recursive: true });
  const wlMock = join(binDir, 'wl');
  writeFileSync(wlMock, `#!/usr/bin/env bash
case "$1" in
  list)
    if [[ "$*" == *"--stage in_review"* ]]; then
      echo '{"success":true,"workItems":[{"id":"SA-PARENT1","title":"Parent One","needsProducerReview":false},{"id":"SA-PARENT2","title":"Parent Two","needsProducerReview":false}]}'
    else
      echo '{"success":true,"workItems":[]}'
    fi
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

  // Driver script: import the real run-release.js and exercise the close step.
  const driver = join(tmpDir, 'driver.mjs');
  writeFileSync(driver, `
import { closeWorkItemsAfterRelease } from ${JSON.stringify(RUN_RELEASE_PATH)};
const result = closeWorkItemsAfterRelease('9.9.9');
console.log(JSON.stringify(result));
`);

  const res = spawnSync('node', [driver], {
    encoding: 'utf-8',
    env: {
      ...process.env,
      PATH: `${binDir}:${process.env.PATH}`,
      WL_CLOSE_LOG: closeLog,
    },
    timeout: 30000,
  });
  assert.equal(res.status, 0, `driver failed: ${res.stderr}`);

  const closeLines = readFileSync(closeLog, 'utf-8').trim().split('\n').filter(Boolean);
  assert.equal(closeLines.length, 2, `expected 2 close invocations, got: ${closeLines}`);
  for (const line of closeLines) {
    assert.ok(
      line.includes('--force'),
      `wl close should include --force (single-pass close, AC3), got: ${line}`,
    );
  }
});
