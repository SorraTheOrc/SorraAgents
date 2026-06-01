import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'triage', 'SKILL.md');

test('triage SKILL.md uses skill-relative script paths', () => {
  const content = readFileSync(PATH, 'utf-8');
  assert.ok(content.includes('skill/triage/scripts/check_or_create.py') || content.includes('skill/triage/scripts/'), 'SKILL.md should reference skill/triage/scripts/*');
});
