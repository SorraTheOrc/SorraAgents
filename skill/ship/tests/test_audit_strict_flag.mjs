/**
 * Tests that the ship release gate audits strictly (SA-0MW0JJ36Z000AF57 AC6),
 * that ship-triggered runs carry the `--ship-triggered` marker
 * (SA-0MWJ7KPC00073EMP), and that the failing-verdict boundary of that marker
 * stays intact on the ship side (SA-0MWJCDX79003103I, parent AC2).
 *
 * Covers:
 *   - the three audit-runner call sites (check-audit-gate.js,
 *     check-final-validation.js, refresh-audits.js) — whose `runAuditCommand`
 *     defaults must construct `... issue <id> --strict --ship-triggered`;
 *   - the surfaced manual remediation commands (buildRemediationCommand, the
 *     producer-review/stale hints, and the offline batch refresh hint) which
 *     must remain marker-free;
 *   - the failing-verdict boundary: a release-gate re-audit that stays
 *     `Ready to close: No` still blocks the gate (never a silent pass) and the
 *     gate never writes the producer-review flag itself — the runner stays the
 *     single decision authority (it demotes + flags in one atomic write);
 *   - the no-deadlock contract: the demoted item leaves the stage-scoped
 *     release candidate set, so the close step's `needsProducerReview`
 *     refusal cannot see it afterwards.
 *
 * Node.js built-in `node --test` runner; no network, no git, no live worklog.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  buildProducerReviewRemediationCommand,
  buildRemediationCommand,
  checkAuditReadyToClose,
  checkProducerReviewStatus,
} from '../scripts/check-audit-gate.js';
import { OFFLINE_AUDIT_REFRESH_HINT } from '../scripts/audit-remediation.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SCRIPTS = join(__dirname, '..', 'scripts');

const GATE_SCRIPTS = [
  'check-audit-gate.js',
  'check-final-validation.js',
  'refresh-audits.js',
];

function sourceOf(file) {
  return readFileSync(join(SCRIPTS, file), 'utf-8');
}

// The `runAuditCommand` default: `python3 "${runnerPath}" issue ${workItemId} ...`
const RUNNER_DEFAULT = /\$\{runnerPath\}"\s+issue\s+\$\{workItemId\}([^`]*)`/g;

function runnerDefaultFragments(file) {
  return [...sourceOf(file).matchAll(RUNNER_DEFAULT)].map((m) => m[1]);
}

describe('ship audit call sites pass --strict', () => {
  for (const file of GATE_SCRIPTS) {
    it(`${file}: runAuditCommand default builds "... issue <id> --strict"`, () => {
      const fragments = runnerDefaultFragments(file);
      assert.ok(
        fragments.length > 0,
        `${file}: should construct a runAuditCommand with runnerPath`,
      );
      // Match specifically the runAuditCommand default (uses ${runnerPath}),
      // not the buildRemediationCommand helper (uses a literal path).
      for (const fragment of fragments) {
        assert.match(
          fragment,
          /--strict/,
          `${file}: runAuditCommand must pass --strict`,
        );
      }
    });
  }
});

describe('ship call sites pass --ship-triggered', () => {
  for (const file of GATE_SCRIPTS) {
    it(`${file}: runAuditCommand default includes --ship-triggered`, () => {
      const fragments = runnerDefaultFragments(file);
      assert.ok(
        fragments.length > 0,
        `${file}: should construct a runAuditCommand with runnerPath`,
      );
      // Match specifically the runAuditCommand default (uses ${runnerPath}),
      // not the buildRemediationCommand helper (uses a literal path).
      for (const fragment of fragments) {
        assert.match(
          fragment,
          /--ship-triggered/,
          `${file}: runAuditCommand must pass --ship-triggered`,
        );
      }
    });

    it(`${file}: the marker is unconditional (no verdict-dependent branch)`, () => {
      // Exactly one occurrence — inside the default command. Nothing strips,
      // adds or conditions the marker on a passing/failing verdict, so both
      // verdicts reach the runner with the same marker and only the runner
      // decides the flag (SA-0MWJ8WGQD0073MG1 AC2/AC3).
      const occurrences = sourceOf(file).split('--ship-triggered').length - 1;
      assert.equal(
        occurrences,
        1,
        `${file}: expected exactly one --ship-triggered occurrence (the runAuditCommand default)`,
      );
    });
  }

  it('the marker is a CLI flag, never an environment variable', () => {
    // A marker settable in an operator's shell would let manual audits
    // masquerade as release-automation verifications (SA-0MW92TODX00064XQ
    // marker-leakage constraint, carried by SA-0MWJCDX79003103I AC5).
    for (const file of GATE_SCRIPTS) {
      const src = sourceOf(file);
      assert.doesNotMatch(
        src,
        /process\s*\.\s*env[^\n]*ship[-_]?triggered/i,
        `${file}: ship-triggered must not be read from the environment`,
      );
      assert.doesNotMatch(
        src,
        /env\s*\[[^\]]*ship[-_]?triggered/i,
        `${file}: ship-triggered must not be read from the environment`,
      );
    }
  });
});

describe('surfaced manual remediation commands are marker-free', () => {
  it('buildRemediationCommand does NOT include --ship-triggered', () => {
    const command = buildRemediationCommand('SA-TEST123');
    assert.doesNotMatch(command, /--ship-triggered/);
    assert.match(command, /audit_runner\.py issue SA-TEST123 --strict/);
  });

  it('buildProducerReviewRemediationCommand does NOT include --ship-triggered', () => {
    const command = buildProducerReviewRemediationCommand('SA-TEST123');
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

// ---------------------------------------------------------------------------
// Failing-verdict boundary (SA-0MWJCDX79003103I, parent AC2)
// ---------------------------------------------------------------------------

const BOUNDARY_ITEM_ID = 'SA-SHIPBOUNDARY1';
const BOUNDARY_ITEM = {
  id: BOUNDARY_ITEM_ID,
  title: 'Boundary item',
  needsProducerReview: false,
  parentId: null,
  updatedAt: '2026-11-20T12:00:00.000Z',
};

function failingAudit(auditedAt) {
  return JSON.stringify({
    success: true,
    workItemId: BOUNDARY_ITEM_ID,
    audit: {
      readyToClose: false,
      auditedAt,
      summary: 'Acceptance criteria not met',
    },
  });
}

function stubBudget() {
  return {
    canAttempt: () => true,
    record: () => {},
    describe: () => '1/5 attempt(s), 0s of 1800s',
    clampedTimeoutMs: (ms) => ms,
  };
}

describe('failing ship-triggered re-audit boundary', () => {
  it('a re-audit that stays "No" blocks the gate and surfaces a marker-free independent audit', async () => {
    const invocations = [];
    let reAudited = false;

    const report = await checkAuditReadyToClose({
      getCandidateItemsFn: () => [BOUNDARY_ITEM],
      // First read: a stale failing verdict (not trustworthy) → in-gate
      // re-audit. Second read: the fresh verdict the re-audit persisted.
      runAuditShow: () => (reAudited
        ? failingAudit('2026-11-21T12:00:00.000Z')
        : failingAudit('2026-11-19T12:00:00.000Z')),
      runAuditCommand: (runnerPath, id) => {
        invocations.push([runnerPath, id]);
        reAudited = true;
        return '';
      },
      resolveAuditRunnerFn: () => '/tmp/fake-audit_runner.py',
      createRemediationBudgetFn: stubBudget,
      queryContentFreshnessFn: () => ({
        fresh: false,
        reason: 'content changed since the last audit',
        hasFingerprint: false,
        auditedAt: null,
      }),
    });

    assert.deepEqual(
      invocations,
      [['/tmp/fake-audit_runner.py', BOUNDARY_ITEM_ID]],
      'exactly one in-gate re-audit should be invoked',
    );
    assert.equal(
      report.hasBlockingItems,
      true,
      'a re-audit that still says "No" must block — never a silent pass',
    );
    assert.equal(report.blockingItems.length, 1);
    assert.equal(report.blockingItems[0].reason, 'Audit verdict: not ready to close');
    assert.equal(report.remediatedItems.length, 0);
    // The remediation surfaced to the operator is an INDEPENDENT audit (no
    // marker): re-running it re-flags the item, so a failure can never be
    // laundered by the release's own verification marker.
    assert.doesNotMatch(report.blockingItems[0].remediation, /--ship-triggered/);
    assert.match(report.blockingItems[0].remediation, /audit_runner\.py issue .* --strict/);
  });

  it('the in-gate re-audit path never writes the producer-review flag itself', () => {
    // The runner is the single decision authority: it demotes and flags in one
    // atomic `wl update`. A ship-side flag write here would duplicate (and
    // could contradict) that decision, so the gate must only invoke the runner
    // and re-read its persisted verdict.
    const src = sourceOf('check-audit-gate.js');
    const start = src.indexOf('export async function attemptAuditRemediation');
    const end = src.indexOf('// ── checkAuditReadyToClose', start);
    assert.ok(start >= 0, 'attemptAuditRemediation should be exported');
    assert.ok(end > start, 'attemptAuditRemediation section should be delimited');
    const body = src.slice(start, end);
    assert.doesNotMatch(body, /needs-producer-review/);
    assert.doesNotMatch(body, /\bwl update\b/);
  });

  it('a demoted+flagged item leaves the release set, so the close step cannot deadlock', () => {
    const demotedFlagged = {
      id: BOUNDARY_ITEM_ID,
      title: BOUNDARY_ITEM.title,
      needsProducerReview: true,
      parentId: null,
    };

    // Contrast: while the item is still a release candidate, a producer-review
    // flag blocks it — refusing the close step by design.
    const whileInReview = checkProducerReviewStatus([demotedFlagged]);
    assert.equal(whileInReview.hasBlockingItems, true);
    assert.equal(whileInReview.blockingItems[0].workItemId, BOUNDARY_ITEM_ID);

    // After the same run demotes it to open/plan_complete it is no longer an
    // in_review candidate, so the refusal has nothing to see: the release
    // proceeds while the item waits in the producer queue.
    const afterDemotion = checkProducerReviewStatus([]);
    assert.equal(afterDemotion.hasBlockingItems, false);

    // Pin the stage-scoped candidate queries that make the demotion
    // load-bearing (a demoted item is `open`/`plan_complete`, never in_review).
    for (const file of ['check-audit-gate.js', 'check-final-validation.js']) {
      assert.match(
        sourceOf(file),
        /wl list --stage in_review --json/,
        `${file}: release candidates must be stage-scoped to in_review`,
      );
    }
    assert.match(
      sourceOf('run-release.js'),
      /getCandidateItems/,
      'run-release.js: the close step must use the stage-scoped candidate query',
    );
  });
});
