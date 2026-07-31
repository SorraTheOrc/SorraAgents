import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'owner-inference', 'SKILL.md');

test('owner-inference SKILL.md references its scripts via relative paths', () => {
  const content = readFileSync(PATH, 'utf-8');
  // pi skill convention: in-skill references use ./scripts/ (not skill/<name>/scripts/)
  assert.ok(content.includes('./scripts/infer_owner.py'), 'SKILL.md should reference ./scripts/infer_owner.py');
});
