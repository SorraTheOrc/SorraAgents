/**
 * Tests that the ship release gate audits strictly (SA-0MW0JJ36Z000AF57 AC6)
 * and that ship-triggered runs carry the `--ship-triggered` marker
 * (SA-0MWJ7KPC00073EMP).
 *
 * Covers the three audit-runner call sites (check-audit-gate.js,
 * check-final-validation.js, refresh-audits.js) — whose `runAuditCommand`
 * defaults must construct `... issue <id> --strict --ship-triggered` — and
 * the surfaced manual remediation commands (buildRemediationCommand, the
 * producer-review/stale hints, and the offline batch refresh hint) which
 * must remain marker-free.
 *
 * Node.js built-in `node --test` runner; no network, no git, no live worklog.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { buildRemediationCommand } from '../scripts/check-audit-gate.js';
import { OFFLINE_AUDIT_REFRESH_HINT } from '../scripts/audit-remediation.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SCRIPTS = join(__dirname, '..', 'scripts');

function sourceOf(file) {
  return readFileSync(join(SCRIPTS, file), 'utf-8');
}

describe('ship audit call sites pass --strict', () => {
  for (const file of [
    'check-audit-gate.js',
    'check-final-validation.js',
    'refresh-audits.js',
  ]) {
    it(`${file}: runAuditCommand default builds "... issue <id> --strict"`, () => {
      const src = sourceOf(file);
      // The default runner constructs `python3 "<runner>" issue <id> --strict --ship-triggered`.
      // Match specifically the runAuditCommand default (uses ${runnerPath}),
      // not the buildRemediationCommand helper (uses a literal path).
      const match = src.match(
        /\$\{runnerPath\}" issue \$\{workItemId\}([^`]*)`/,
      );
      assert.ok(match, `${file}: should construct a runAuditCommand with runnerPath`);
      assert.match(
        match[1],
        /--strict/,
        `${file}: runAuditCommand must pass --strict`,
      );
    });
  }
});

describe('ship call sites pass --ship-triggered', () => {
  for (const file of [
    'check-audit-gate.js',
    'check-final-validation.js',
    'refresh-audits.js',
  ]) {
    it(`${file}: runAuditCommand default includes --ship-triggered`, () => {
      const src = sourceOf(file);
      // The default runner constructs `python3 "<runner>" issue <id> --strict --ship-triggered`.
      // Match specifically the runAuditCommand default (uses ${runnerPath}),
      // not the buildRemediationCommand helper (uses a literal path).
      const match = src.match(
        /\$\{runnerPath\}" issue \$\{workItemId\}([^`]*)`/,
      );
      assert.ok(match, `${file}: should construct a runAuditCommand with runnerPath`);
      assert.match(
        match[1],
        /--ship-triggered/,
        `${file}: runAuditCommand must pass --ship-triggered`,
      );
    });
  }
});

describe('surfaced manual remediation commands are marker-free', () => {
  it('buildRemediationCommand does NOT include --ship-triggered', () => {
    const command = buildRemediationCommand('SA-TEST123');
    assert.doesNotMatch(command, /--ship-triggered/);
    assert.match(command, /audit_runner\.py issue SA-TEST123 --strict/);
  });

  it('offline batch refresh hint does NOT include --ship-triggered', () => {
    assert.doesNotMatch(OFFLINE_AUDIT_REFRESH_HINT, /--ship-triggered/);
    assert.match(OFFLINE_AUDIT_REFRESH_HINT, /audit_runner\.py batch --strict/);
  });

  it('check-audit-gate surfaced commands do NOT include --ship-triggered', () => {
    const src = sourceOf('check-audit-gate.js');
    assert.doesNotMatch(src, /issue \$\{workItemId\} --strict --force.*--ship-triggered/);
    assert.match(src, /issue \$\{workItemId\} --strict --force/);
  });
});
