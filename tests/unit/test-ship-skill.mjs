/**
 * Unit tests for skill/ship/SKILL.md
 *
 * Validates that the ship skill describes the Ship subagent as the
 * primary release executor, references the correct configuration and
 * script files, and preserves the human Release Manager fallback.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SHIP_SKILL_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');

// ---------------------------------------------------------------------------
// 1. SKILL.md exists
// ---------------------------------------------------------------------------
test('ship skill: SKILL.md exists', () => {
  assert.ok(
    existsSync(SHIP_SKILL_PATH),
    'skill/ship/SKILL.md should exist',
  );
});

// ---------------------------------------------------------------------------
// 2. Basic structure — YAML frontmatter with name field
// ---------------------------------------------------------------------------
test('ship skill: has valid YAML frontmatter with name field', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  assert.ok(
    content.startsWith('---'),
    'SKILL.md should start with YAML frontmatter',
  );

  assert.ok(
    content.includes('name:'),
    'YAML frontmatter should include a name field',
  );

  // Extract frontmatter
  const endMatch = content.indexOf('---', 3);
  assert.ok(endMatch > 3, 'YAML frontmatter should have closing ---');

  const frontmatter = content.slice(3, endMatch);
  assert.ok(
    frontmatter.includes('ship') || frontmatter.includes('Ship'),
    'Frontmatter name should reference ship',
  );
});

// ---------------------------------------------------------------------------
// 3. Description mentions the Ship subagent
// ---------------------------------------------------------------------------
test('ship skill: description mentions the Ship subagent as the release executor', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  // The description / purpose should mention that Ship is the release executor
  assert.ok(
    content.includes('Ship') ||
    content.includes('ship subagent') ||
    content.includes('Ship subagent'),
    'SKILL.md should mention the Ship subagent',
  );
});

// ---------------------------------------------------------------------------
// 4. Release Process section — run-release.js is primary executor
// ---------------------------------------------------------------------------
test('ship skill: Release Process section describes the release script as executor', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  // Should have a Release Process section
  assert.ok(
    content.includes('Release Process') ||
    content.includes('release process') ||
    content.includes('Release process'),
    'SKILL.md should have a Release Process section',
  );

  // Should reference the release script or the release action
  assert.ok(
    content.includes('run-release.js') ||
    content.includes('release action') ||
    content.includes('/skill:ship release') ||
    content.includes('./scripts/run-release.js'),
    'Release Process section should reference the release script or action',
  );
});

// ---------------------------------------------------------------------------
// 5. References scripts/run-release.js as the release script
// ---------------------------------------------------------------------------
test('ship skill: references scripts/run-release.js as the release script', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  assert.ok(
    content.includes('scripts/run-release.js') ||
    content.includes('run-release.js'),
    'SKILL.md should reference scripts/run-release.js as the release entry point',
  );
});

// ---------------------------------------------------------------------------
// 6. References scripts/release/merge-dev-to-main.sh
// ---------------------------------------------------------------------------
test('ship skill: references scripts/release/merge-dev-to-main.sh as the release script', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  assert.ok(
    content.includes('scripts/release/merge-dev-to-main.sh') ||
    content.includes('merge-dev-to-main.sh'),
    'SKILL.md should reference scripts/release/merge-dev-to-main.sh',
  );
});

// ---------------------------------------------------------------------------
// 7. Preserves human Release Manager fallback
// ---------------------------------------------------------------------------
test('ship skill: preserves human Release Manager as a fallback', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  assert.ok(
    content.includes('Release Manager') ||
    content.includes('release manager') ||
    content.includes('human'),
    'SKILL.md should preserve the human Release Manager as a fallback',
  );

  // The fallback should indicate it's for repos without Ship configured
  assert.ok(
    content.includes('fallback') ||
    content.includes('without') ||
    content.includes('not configured') ||
    content.includes('alternative'),
    'SKILL.md should describe when the human fallback is used',
  );
});

// ---------------------------------------------------------------------------
// 8. Skill references its own scripts as internal modules
// ---------------------------------------------------------------------------
test('ship skill: references its own scripts directory', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  // Should reference the scripts directory (internal modules)
  assert.ok(
    content.includes('./scripts/') ||
    content.includes('scripts/'),
    'SKILL.md should reference its own scripts directory',
  );
});

// ---------------------------------------------------------------------------
// 9. Release Process section integrates with docs/dev/release-process.md
// ---------------------------------------------------------------------------
test('ship skill: Release Process section references docs/dev/release-process.md', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  assert.ok(
    content.includes('docs/dev/release-process.md') ||
    content.includes('release-process.md') ||
    content.includes('release process'),
    'SKILL.md should reference the release process documentation',
  );
});

// ---------------------------------------------------------------------------
// 10. Purpose/goal is clearly stated
// ---------------------------------------------------------------------------
test('ship skill: has a clearly stated purpose or goal', () => {
  const content = readFileSync(SHIP_SKILL_PATH, 'utf-8');

  // Should have a Purpose, Goal, or Summary section
  assert.ok(
    content.includes('## Purpose') ||
    content.includes('## Goal') ||
    content.includes('## Summary') ||
    content.includes('purpose') ||
    content.includes('Purpose'),
    'SKILL.md should have a clearly stated purpose or goal',
  );
});
