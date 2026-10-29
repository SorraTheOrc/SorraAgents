/**
 * Regression tests for the shared temp-dir helper (SA-0MVTSV5SO008625I).
 *
 * Context: several node harnesses created harness sandboxes with a bare
 * `mkdtempSync` and never removed them. On a tmpfs with a per-user quota the
 * accumulated directories exhausted `/tmp`, and a later full-suite run failed
 * with an opaque `writeFileSync: Unknown system error -122` (EDQUOT) — the
 * 2026-10-29 `run-release: --help is side-effect free` failure.
 *
 * These tests pin the helper's contract: every directory it creates is
 * tracked and removed by `cleanupTempDirs()` (which the module also runs on
 * process exit), and cleanup is idempotent.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { basename } from 'node:path';

import { makeTempDir, cleanupTempDirs } from '../helpers/temp-dir.mjs';

describe('temp-dir: makeTempDir / cleanupTempDirs', () => {
  test('creates an existing directory carrying the requested prefix', () => {
    const dir = makeTempDir('temp-dir-contract-');
    assert.ok(existsSync(dir), 'makeTempDir must create the directory');
    assert.match(basename(dir), /^temp-dir-contract-/);
  });

  test('cleanupTempDirs removes every directory it created', () => {
    const first = makeTempDir('temp-dir-cleanup-a-');
    const second = makeTempDir('temp-dir-cleanup-b-');

    cleanupTempDirs();

    assert.equal(existsSync(first), false, 'first temp dir must be removed');
    assert.equal(existsSync(second), false, 'second temp dir must be removed');
  });

  test('cleanupTempDirs is idempotent', () => {
    makeTempDir('temp-dir-idempotent-');
    cleanupTempDirs();
    assert.doesNotThrow(() => cleanupTempDirs());
  });
});
