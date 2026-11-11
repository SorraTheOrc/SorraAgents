/**
 * Tests that the ship release gate audits strictly (SA-0MW0JJ36Z000AF57 AC6).
 *
 * Covers the three audit-runner call sites (check-audit-gate.js,
 * check-final-validation.js, refresh-audits.js) — whose `runAuditCommand`
 * defaults must construct `... issue <id> --strict` — and the surfaced manual
 * remediation commands (buildRemediationCommand, the producer-review/stale
 * hints, and the offline batch refresh hint).
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
      // The default runner constructs `python3 "<runner>" issue <id> --strict`.
      const match = src.match(
        /issue \$\{workItemId\}([^`]*)`/,
      );
      assert.ok(match, `${file} should construct an "issue" audit command`);
      assert.match(
        match[1],
        /--strict/,
        `${file} audit runner must pass --strict`,
      );
    });
  }
});

describe('surfaced manual remediation commands include --strict', () => {
  it('buildRemediationCommand includes --strict', () => {
    const command = buildRemediationCommand('SA-TEST123');
    assert.match(command, /audit_runner\.py issue SA-TEST123 --strict/);
  });

  it('offline batch refresh hint includes --strict', () => {
    assert.match(OFFLINE_AUDIT_REFRESH_HINT, /audit_runner\.py batch --strict/);
  });

  it('check-audit-gate surfaced commands include --strict', () => {
    const src = sourceOf('check-audit-gate.js');
    assert.match(src, /issue \$\{workItemId\} --strict --force/);
  });
});
