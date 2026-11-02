/**
 * temp-dir — shared temp-directory helper for node test suites.
 *
 * Test harnesses create transient sandbox trees (fake skill layouts, git
 * fixtures, webhook configs) via `mkdtempSync`. Historically several suites
 * leaked those trees: every full-suite run left unremoved directories under
 * the OS temp dir, and on a tmpfs with a user quota (`/tmp` on this host) the
 * accumulation eventually exhausts the quota and a later run fails with an
 * opaque `writeFileSync: Unknown system error -122` (EDQUOT) — e.g. the
 * 2026-10-29 `run-release: --help is side-effect free` failure
 * (SA-0MVTSV5SO008625I).
 *
 * `makeTempDir()` creates a fresh directory, registers it for best-effort
 * removal when the test process exits (synchronous so it is safe inside a
 * process `exit` listener), and wraps creation failures such as EDQUOT/ENOSPC
 * in an actionable diagnostic instead of the raw errno.
 */
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const created = new Set();

/**
 * Create a temp directory that is removed when the test process exits.
 *
 * @param {string} [prefix] - Directory name prefix ('test-' by default).
 * @returns {string} Absolute path to the new directory.
 */
export function makeTempDir(prefix = 'test-') {
  const base = tmpdir();
  let dir;
  try {
    dir = mkdtempSync(join(base, prefix));
  } catch (err) {
    throw new Error(
      `makeTempDir: cannot create a temp dir under ${base} (${err.code}). ` +
        'The OS temp dir is likely full (quota/space exhausted) — inspect ' +
        `\`df -h ${base}\` and remove stale test artefacts before re-running.`,
      { cause: err },
    );
  }
  created.add(dir);
  return dir;
}

/** Remove every directory created by makeTempDir (best-effort, idempotent). */
export function cleanupTempDirs() {
  for (const dir of created) {
    try {
      rmSync(dir, { recursive: true, force: true });
    } catch {
      // best-effort — a failure here must never mask the test result.
    }
  }
  created.clear();
}

// Exit listeners may only run synchronous code — rmSync qualifies.
process.on('exit', cleanupTempDirs);
