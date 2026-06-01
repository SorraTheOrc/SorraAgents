import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const RUN_RELEASE = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');

test('run-release: reports missing release script when absent', () => {
  const tmp = mkdtempSync(join(tmpdir(), 'run-release-'));
  const res = spawnSync(process.execPath, [RUN_RELEASE], { cwd: tmp, encoding: 'utf-8' });

  // Should exit non-zero and print the missing-script message to stderr
  assert.notStrictEqual(res.status, 0, `Expected non-zero exit status, got ${res.status}`);
  const out = (res.stdout || '') + '\n' + (res.stderr || '');
  assert.ok(
    out.includes('Ship automated release unavailable') || out.includes("repository is missing the canonical release script"),
    `Expected missing-script message in output, got:\n${out}`,
  );
});
