/**
 * Unit tests for post-release dev sync in run-release.js
 *
 * Tests that after a successful release, dev is synced with main.
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
// 4. syncDevWithMain returns correct structure
// ---------------------------------------------------------------------------
test('post-release sync: syncDevWithMain returns expected structure', async () => {
  const mod = await import(RUN_RELEASE_PATH);
  const result = mod.syncDevWithMain();

  assert.ok(typeof result === 'object');
  assert.ok('success' in result);
  assert.ok('message' in result);
  assert.equal(typeof result.success, 'boolean');
  assert.equal(typeof result.message, 'string');
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
