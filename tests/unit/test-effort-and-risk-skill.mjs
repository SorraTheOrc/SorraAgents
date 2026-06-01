import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'effort-and-risk', 'SKILL.md');

test('effort-and-risk SKILL.md uses skill-relative script paths', () => {
  const content = readFileSync(PATH, 'utf-8');
  assert.ok(content.includes('skill/effort-and-risk/scripts/run_skill.py') || content.includes('skill/effort-and-risk/scripts/orchestrate_estimate.py') || content.includes('skill/effort-and-risk/scripts/'), 'SKILL.md should reference skill/effort-and-risk/scripts/*');
});
