import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const PATH = join(REPO_ROOT, 'skill', 'triage', 'SKILL.md');

test('triage SKILL.md references its scripts via skill_path', () => {
  const content = readFileSync(PATH, 'utf-8');
  // Convention: script references resolve the skill dir at runtime via the
  // skill_path tool (never ./scripts/ relative to the project CWD, never
  // skill/<name>/scripts/ legacy paths).
  assert.ok(
    content.includes('$(skill_path triage)/scripts/check_or_create.py'),
    'SKILL.md should reference scripts/check_or_create.py via $(skill_path triage)/scripts/',
  );
  assert.ok(
    !/`\.\/scripts\//.test(content),
    'SKILL.md must not reference scripts via ./scripts/ (breaks when run from project CWD)',
  );
});