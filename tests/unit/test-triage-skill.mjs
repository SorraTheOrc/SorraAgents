import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'triage', 'SKILL.md');

test('triage SKILL.md references its scripts via relative paths', () => {
  const content = readFileSync(PATH, 'utf-8');
  // pi skill convention: in-skill references use ./scripts/ (not skill/<name>/scripts/)
  assert.ok(content.includes('./scripts/check_or_create.py'), 'SKILL.md should reference ./scripts/check_or_create.py');
});
