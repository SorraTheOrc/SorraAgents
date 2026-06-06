/**
 * Unit tests for skill/git-management/SKILL.md
 *
 * Validates that the git-management skill has valid frontmatter,
 * documents all required actions, safety constraints, and references
 * existing ship/cleanup infrastructure.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const SKILL_DIR = join(REPO_ROOT, 'skill', 'git-management');
const SKILL_MD = join(SKILL_DIR, 'SKILL.md');
const SCRIPTS_DIR = join(SKILL_DIR, 'scripts');

// ---------------------------------------------------------------------------
// 1. SKILL.md exists
// ---------------------------------------------------------------------------
test('git-management skill: SKILL.md exists', () => {
  assert.ok(existsSync(SKILL_MD), 'skill/git-management/SKILL.md should exist');
});

// ---------------------------------------------------------------------------
// 2. Valid YAML frontmatter with name field
// ---------------------------------------------------------------------------
test('git-management skill: has valid YAML frontmatter with name field', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

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
    frontmatter.includes('git-management') || frontmatter.includes('git_management'),
    'Frontmatter name should reference git-management',
  );
});

// ---------------------------------------------------------------------------
// 3. Description mentions unified git management
// ---------------------------------------------------------------------------
test('git-management skill: description mentions unified git lifecycle', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('create') &&
    content.includes('commit') &&
    content.includes('push') &&
    content.includes('cleanup'),
    'SKILL.md should mention the full lifecycle (create, commit, push, cleanup)',
  );
});

// ---------------------------------------------------------------------------
// 4. Documents all required actions
// ---------------------------------------------------------------------------
test('git-management skill: documents all required actions', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  const requiredActions = [
    'create-branch',
    'commit',
    'push',
    'create-pr',
    'merge-pr',
    'cleanup',
    'workflow',
  ];

  for (const action of requiredActions) {
    assert.ok(
      content.includes(action),
      `SKILL.md should document action: ${action}`,
    );
  }
});

// ---------------------------------------------------------------------------
// 5. Documents safety constraints
// ---------------------------------------------------------------------------
test('git-management skill: documents force-push prohibition', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('force-push') || content.includes('force_push'),
    'SKILL.md should mention force-push',
  );

  assert.ok(
    content.includes('prohibit') ||
    content.includes('not permitted') ||
    content.includes('rejected') ||
    content.includes('never'),
    'SKILL.md should state force-push is prohibited/rejected',
  );
});

test('git-management skill: documents protected branch protection', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('main') && content.includes('master'),
    'SKILL.md should reference protected branches (main, master)',
  );

  assert.ok(
    content.includes('protected') || content.includes('blocked'),
    'SKILL.md should use protected/blocked terminology',
  );
});

test('git-management skill: documents branch naming convention', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('wl-') && content.includes('work-item-id'),
    'SKILL.md should document the wl-<id>-<desc> branch naming convention',
  );
});

// ---------------------------------------------------------------------------
// 6. Delegates to existing infrastructure
// ---------------------------------------------------------------------------
test('git-management skill: references ship git-helpers', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('skill/ship/scripts/git-helpers') ||
    content.includes('git-helpers.js'),
    'SKILL.md should reference skill/ship/scripts/git-helpers.js',
  );
});

test('git-management skill: references ship.js', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('skill/ship/scripts/ship') ||
    content.includes('ship.js'),
    'SKILL.md should reference skill/ship/scripts/ship.js',
  );
});

test('git-management skill: references cleanup infrastructure', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('skill/cleanup') || content.includes('cleanup'),
    'SKILL.md should reference cleanup infrastructure',
  );
});

// ---------------------------------------------------------------------------
// 7. Script contract is explicit
// ---------------------------------------------------------------------------
test('git-management skill: documents exit codes', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('Exit code') || content.includes('exit code') || content.includes('Exit codes'),
    'SKILL.md should document exit codes',
  );
});

test('git-management skill: documents dry-run support', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('--dry-run') || content.includes('dry-run') || content.includes('dry run'),
    'SKILL.md should document dry-run support',
  );
});

test('git-management skill: documents JSON output', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('--json') || content.includes('JSON output') || content.includes('structured'),
    'SKILL.md should document JSON output support',
  );
});

test('git-management skill: documents prerequisite checks', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('Prerequisite') || content.includes('prerequisite') || content.includes('requires'),
    'SKILL.md should document prerequisite checks',
  );
});

// ---------------------------------------------------------------------------
// 8. Scripts directory exists with expected files
// ---------------------------------------------------------------------------
test('git-management skill: scripts directory exists', () => {
  assert.ok(
    existsSync(SCRIPTS_DIR),
    'skill/git-management/scripts/ directory should exist',
  );
});

test('git-management skill: shared helpers module exists', () => {
  assert.ok(
    existsSync(join(SCRIPTS_DIR, 'git-mgmt-helpers.mjs')),
    'git-mgmt-helpers.mjs should exist',
  );
});

// ---------------------------------------------------------------------------
// 9. No force-push or direct-to-main in documented contract
// ---------------------------------------------------------------------------
test('git-management skill: safety constraints prohibit direct-to-main', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('no direct-to-main') ||
    content.includes('not push directly to') ||
    content.includes('never push directly'),
    'SKILL.md should prohibit direct pushes to main',
  );
});

// ---------------------------------------------------------------------------
// 10. Purpose is clearly stated
// ---------------------------------------------------------------------------
test('git-management skill: has a clearly stated purpose', () => {
  const content = readFileSync(SKILL_MD, 'utf-8');

  assert.ok(
    content.includes('## Purpose') ||
    content.includes('## Goal') ||
    content.includes('## Summary'),
    'SKILL.md should have a clearly stated purpose/goal section',
  );
});
