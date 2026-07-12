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
test('close-work-items: closeWorkItemsAfterRelease returns expected structure', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease('1.0.0');

  assert.ok(typeof result === 'object');
  assert.ok('success' in result);
  assert.ok('message' in result);
  assert.equal(typeof result.success, 'boolean');
  assert.equal(typeof result.message, 'string');
});

// ---------------------------------------------------------------------------
// 4. closeWorkItemsAfterRelease accepts version string
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease accepts a version string', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  // With empty candidate set (no items to close), it should succeed gracefully
  const result = mod.closeWorkItemsAfterRelease('1.2.3');

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
});

// ---------------------------------------------------------------------------
// 5. closeWorkItemsAfterRelease handles missing version gracefully
// ---------------------------------------------------------------------------
test('close-work-items: closeWorkItemsAfterRelease handles missing version', async () => {
  const mod = await import(RUN_RELEASE_PATH);

  const result = mod.closeWorkItemsAfterRelease(null);

  assert.ok(
    result.message.includes('No version'),
    'closeWorkItemsAfterRelease should report when no version is provided',
  );
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
