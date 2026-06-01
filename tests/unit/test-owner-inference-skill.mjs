import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'owner-inference', 'SKILL.md');

test('owner-inference SKILL.md uses skill-relative script paths', () => {
  const content = readFileSync(PATH, 'utf-8');
  assert.ok(content.includes('skill/owner-inference/scripts/infer_owner.py') || content.includes('skill/owner-inference/scripts/'), 'SKILL.md should reference skill/owner-inference/scripts/*');
});
