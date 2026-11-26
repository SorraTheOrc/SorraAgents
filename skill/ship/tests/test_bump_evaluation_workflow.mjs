/**
 * Tests for the ship skill's "Version bump evaluation" workflow and its
 * in-session producer approval gate (SA-0MWFVQX4W0080O86).
 *
 * Two halves, both hermetic:
 *
 *   1. `bump-decision.js` rules — `shouldAskProducer`, `buildJustification`
 *      and `resolveApproval` (patch auto-proceed, non-patch approval-pending,
 *      decline, headless fail-closed).
 *   2. The documented workflow contract in `skill/ship/SKILL.md` — the
 *      evaluation step is present, is placed before the gating checks, and
 *      states the rubric, the in-session ask, the decline path, the
 *      fail-closed headless behaviour and the `--bump` override precedence.
 *
 * No test reads or mutates the live worklog, spawns a release, or touches real
 * git state: the decision module is pure and the SKILL.md half is a read.
 *
 * Node.js built-in `node --test` runner.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const MODULE_PATH = join(__dirname, '..', 'scripts', 'release', 'bump-decision.js');
const PREFLIGHT_PATH = join(__dirname, '..', 'scripts', 'release', 'bump-preflight.js');
const SKILL_PATH = join(__dirname, '..', 'SKILL.md');

const {
  BUMP_TYPES,
  DEFAULT_BUMP,
  FALLBACK_BUMP,
  buildJustification,
  isValidBumpType,
  normaliseProducerDecision,
  resolveApproval,
  shouldAskProducer,
} = await import(MODULE_PATH);

const SKILL_TEXT = readFileSync(SKILL_PATH, 'utf-8');
const DECISION_SOURCE = readFileSync(MODULE_PATH, 'utf-8');

// ── shouldAskProducer ────────────────────────────────────────────────────────

describe('shouldAskProducer', () => {
  it('auto-proceeds on patch and asks for minor/major', () => {
    assert.equal(shouldAskProducer('patch'), false);
    assert.equal(shouldAskProducer('minor'), true);
    assert.equal(shouldAskProducer('major'), true);
  });

  it('fails closed for unknown or missing bump types', () => {
    assert.equal(shouldAskProducer(undefined), true);
    assert.equal(shouldAskProducer(null), true);
    assert.equal(shouldAskProducer('huge'), true);
  });

  it('exposes the bump vocabulary and defaults', () => {
    assert.deepEqual([...BUMP_TYPES], ['patch', 'minor', 'major']);
    assert.equal(DEFAULT_BUMP, 'patch');
    assert.equal(FALLBACK_BUMP, 'patch');
    assert.equal(isValidBumpType('minor'), true);
    assert.equal(isValidBumpType('Minor'), false);
    assert.equal(isValidBumpType(undefined), false);
  });
});

// ── resolveApproval ──────────────────────────────────────────────────────────

describe('resolveApproval', () => {
  it('auto-proceeds with patch and never asks the producer', () => {
    const decision = resolveApproval({ proposedBump: 'patch' });

    assert.equal(decision.action, 'auto-proceed');
    assert.equal(decision.bump, 'patch');
    assert.equal(decision.requiresProducerApproval, false);
  });

  it('is approval-pending for minor/major until the producer answers (no bump applied)', () => {
    for (const bump of ['minor', 'major']) {
      const decision = resolveApproval({ proposedBump: bump });

      assert.equal(decision.action, 'await-approval', `${bump} must wait for the producer`);
      assert.equal(decision.bump, null, `${bump} must apply no bump while pending`);
      assert.equal(decision.requiresProducerApproval, true);
      assert.match(decision.reason, /ask the producer/i);
    }
  });

  it('applies the approved non-patch bump', () => {
    for (const answer of ['approve', 'yes', 'OK', true]) {
      const decision = resolveApproval({ proposedBump: 'minor', producerDecision: answer });
      assert.equal(decision.action, 'proceed', `answer ${String(answer)} should approve`);
      assert.equal(decision.bump, 'minor');
    }

    const major = resolveApproval({ proposedBump: 'major', producerDecision: 'approved' });
    assert.equal(major.action, 'proceed');
    assert.equal(major.bump, 'major');
  });

  it('honours a decline: no non-patch bump is applied and patch is the fallback', () => {
    for (const answer of ['decline', 'no', false]) {
      const decision = resolveApproval({ proposedBump: 'major', producerDecision: answer });
      assert.equal(decision.action, 'declined', `answer ${String(answer)} should decline`);
      assert.equal(decision.bump, null, 'a declined proposal is never applied');
      assert.equal(decision.fallbackBump, 'patch');
    }
  });

  it('never self-approves a non-patch bump in a headless run', () => {
    for (const bump of ['minor', 'major']) {
      const decision = resolveApproval({ proposedBump: bump, headless: true });

      assert.equal(decision.action, 'await-producer-review');
      assert.equal(decision.bump, null, 'headless runs must apply no non-patch bump');
      assert.equal(decision.requiresProducerApproval, true);
      assert.match(decision.reason, /headless/i);
      assert.match(decision.reason, /stop/i);
    }

    // A patch bump is unaffected by the headless path.
    const patch = resolveApproval({ proposedBump: 'patch', headless: true });
    assert.equal(patch.action, 'auto-proceed');
    assert.equal(patch.bump, 'patch');
  });

  it('rejects an invalid bump type without applying anything', () => {
    const decision = resolveApproval({ proposedBump: 'epic' });
    assert.equal(decision.action, 'invalid');
    assert.equal(decision.bump, null);
  });
});

// ── Producer answer normalisation ────────────────────────────────────────────

describe('normaliseProducerDecision', () => {
  it('maps common answers and ignores unrecognised input', () => {
    assert.equal(normaliseProducerDecision(' Approve '), 'approved');
    assert.equal(normaliseProducerDecision('no'), 'declined');
    assert.equal(normaliseProducerDecision(true), 'approved');
    assert.equal(normaliseProducerDecision(false), 'declined');
    assert.equal(normaliseProducerDecision('maybe later'), null);
    assert.equal(normaliseProducerDecision(undefined), null);
  });
});

// ── buildJustification ───────────────────────────────────────────────────────

describe('buildJustification', () => {
  it('names each contributing item, the reason and the proposed old → new version', () => {
    const text = buildJustification({
      proposedBump: 'minor',
      currentVersion: '0.1.29',
      proposedVersion: '0.2.0',
      contributing: [
        { id: 'SA-AAA', title: 'Deterministic replay', reason: 'adds a new user-facing capability' },
        { id: 'SA-BBB', title: 'Replay export', reason: 'adds a new command' },
      ],
      rationale: 'Both items add capability a user can newly do.',
    });

    assert.match(text, /Version bump proposal: minor/);
    assert.match(text, /0\.1\.29 → 0\.2\.0/);
    assert.match(text, /Producer approval required: yes/);
    assert.match(text, /SA-AAA/);
    assert.match(text, /Deterministic replay/);
    assert.match(text, /adds a new user-facing capability/);
    assert.match(text, /SA-BBB/);
    assert.match(text, /Replay export/);
    assert.match(text, /Both items add capability/);
  });

  it('tolerates bare item ids and a release-wide judgement', () => {
    const withIds = buildJustification({ proposedBump: 'major', contributing: ['SA-CCC'] });
    assert.match(withIds, /SA-CCC/);
    assert.match(withIds, /Producer approval required: yes/);

    const releaseWide = buildJustification({ proposedBump: 'minor' });
    assert.match(releaseWide, /no individual item identified/);
  });

  it('returns an empty string for an invalid bump type', () => {
    assert.equal(buildJustification({ proposedBump: 'nonsense' }), '');
    assert.equal(buildJustification({}), '');
  });
});

// ── Documented workflow contract ─────────────────────────────────────────────

describe('SKILL.md version-bump evaluation contract', () => {
  const section = SKILL_TEXT.split('## Version bump evaluation')[1] || '';

  it('documents a "Version bump evaluation" step before the gating checks', () => {
    assert.ok(section, 'SKILL.md must document a "Version bump evaluation" section');

    const evaluationIndex = SKILL_TEXT.indexOf('## Version bump evaluation');
    const gatingIndex = SKILL_TEXT.indexOf('## Gating');
    assert.ok(evaluationIndex > 0, 'the evaluation section must exist');
    assert.ok(gatingIndex > 0, 'the gating section must exist');
    assert.ok(
      evaluationIndex < gatingIndex,
      'the evaluation step must be documented before the gating checks',
    );
  });

  it('states that the evaluation runs before any git mutation', () => {
    const head = section.slice(0, 1200);
    assert.match(head, /before any (git mutation|gate)/i);
    assert.match(section, /read-only/i);
  });

  it('documents the content-based rubric for patch, minor and major', () => {
    assert.match(section, /`patch`[\s\S]{0,200}fix|fix[\s\S]{0,200}`patch`/i);
    assert.match(section, /`minor`[\s\S]{0,200}capabilit/i);
    assert.match(section, /`major`[\s\S]{0,300}(breaking|break|remov|incompatib)/i);
    assert.match(section, /content-based|from the items' \*\*content\*\*/i);
    assert.match(section, /never from `issueType`|not.{0,40}`issueType`/i);
  });

  it('documents the in-session producer approval gate for non-patch bumps', () => {
    assert.match(section, /patch[\s\S]{0,200}automatically/i);
    assert.match(section, /minor[\s\S]{0,200}major[\s\S]{0,400}(producer)/i);
    assert.match(section, /in (this|the) PI session|in-session/i);
    assert.match(section, /no\s+\*\*?git mutation|\*\*no\*\* git mutation|apply \*\*no\*\* git mutation/i);
  });

  it('documents the decline path and the headless fail-closed behaviour', () => {
    assert.match(section, /declin/i);
    assert.match(section, /preferred bump/i);
    assert.match(section, /headless/i);
    assert.match(section, /never\s+(auto-)?self-approve|never self-approve/i);
    assert.match(section, /needsProducerReview/);
  });

  it('documents the --bump override precedence and --skip-checks non-bypass', () => {
    assert.match(section, /`--bump <type>`[\s\S]{0,200}override/i);
    assert.match(
      section,
      /`--skip-checks`[\s\S]{0,240}not[\s\S]{0,40}bypass/i,
    );
  });

  it('names the preflight and decision helpers', () => {
    assert.match(section, /bump-preflight\.js/);
    assert.match(section, /bump-decision\.js/);
  });

  it('lists the evaluation as a release-process step before the gates', () => {
    const processSection = SKILL_TEXT.split('## Release Process')[1] || '';
    assert.match(processSection, /version bump evaluation/i);

    const processEvaluation = processSection.toLowerCase().indexOf('version bump evaluation');
    const processGates = processSection.toLowerCase().indexOf('unmerged branches check');
    assert.ok(processEvaluation >= 0, 'the process list must mention the evaluation');
    assert.ok(processGates >= 0, 'the process list must still list the gates');
    assert.ok(
      processEvaluation < processGates,
      'the evaluation must be listed before the first gate',
    );
  });
});

// ── No-mutation contract ─────────────────────────────────────────────────────

describe('no-mutation contract', () => {
  it('keeps the decision module pure: no subprocess, filesystem or worklog mutation', () => {
    for (const banned of ['child_process', 'execSync', 'spawnSync', 'writeFileSync', 'unlinkSync', 'rmSync']) {
      assert.ok(
        !DECISION_SOURCE.includes(banned),
        `bump-decision.js must stay pure (found ${banned})`,
      );
    }
    assert.doesNotMatch(DECISION_SOURCE, /wl (close|update|comment|create|delete|sync|import)/);
    assert.doesNotMatch(DECISION_SOURCE, /git (merge|tag|push|commit)/);
  });

  it('yields no bump for every approval-pending, declined or headless outcome', () => {
    const pending = [
      resolveApproval({ proposedBump: 'minor' }),
      resolveApproval({ proposedBump: 'major' }),
      resolveApproval({ proposedBump: 'minor', headless: true }),
      resolveApproval({ proposedBump: 'major', headless: true }),
      resolveApproval({ proposedBump: 'major', producerDecision: 'declined' }),
      resolveApproval({ proposedBump: 'epic' }),
    ];

    for (const decision of pending) {
      assert.equal(decision.bump, null, `${decision.action} must apply no bump`);
    }
  });

  it('keeps the preflight the only gatherer reused by the workflow', () => {
    const preflightSource = readFileSync(PREFLIGHT_PATH, 'utf-8');
    assert.match(preflightSource, /export function runBumpPreflight/);
    // The workflow must not re-implement candidate gathering or arithmetic.
    assert.doesNotMatch(DECISION_SOURCE, /getCandidateItems|bumpVersion/);
  });
});

// ── Reference documentation contract (SA-0MWFVQXSZ000EN5I) ──────────────────
//
// The maintainer-facing reference must describe the evaluation step and the
// in-session approval gate, list the new helpers in its scripts table, and
// keep the `--bump` forwarding contract documented and unchanged.

const REFERENCE_PATH = join(__dirname, '..', '..', '..', 'docs', 'dev', 'ship-skill-reference.md');
const RUN_RELEASE_PATH = join(__dirname, '..', 'scripts', 'run-release.js');
const REFERENCE_TEXT = readFileSync(REFERENCE_PATH, 'utf-8');
const RUN_RELEASE_SOURCE = readFileSync(RUN_RELEASE_PATH, 'utf-8');

const { releaseScriptForwardArgs } = await import(RUN_RELEASE_PATH);

describe('ship skill reference documents the bump evaluation', () => {
  it('has a version-bump evaluation section inside the release process', () => {
    assert.match(REFERENCE_TEXT, /### Version bump evaluation \(SA-0MW978ONE008WMIB\)/);

    const processIndex = REFERENCE_TEXT.indexOf('## Release Process');
    const evaluationIndex = REFERENCE_TEXT.indexOf('### Version bump evaluation');
    const changelogIndex = REFERENCE_TEXT.indexOf('### CHANGELOG release-section preservation');

    assert.ok(processIndex >= 0, 'the release process section must exist');
    assert.ok(evaluationIndex > processIndex, 'the evaluation section belongs to the release process');
    assert.ok(
      evaluationIndex < changelogIndex,
      'the evaluation section must precede the downstream release-process detail',
    );
  });

  it('orders the evaluation before the gates in the step list', () => {
    const steps = REFERENCE_TEXT.slice(
      REFERENCE_TEXT.indexOf('## Release Process'),
      REFERENCE_TEXT.indexOf('### Version bump evaluation'),
    );
    const evaluation = steps.toLowerCase().indexOf('version bump evaluation');
    const firstGate = steps.toLowerCase().indexOf('unmerged branches check');

    assert.ok(evaluation >= 0, 'the step list must mention the evaluation');
    assert.ok(firstGate > evaluation, 'the evaluation must be listed before the gates');
  });

  it('lists the preflight and decision helpers in the scripts table', () => {
    assert.match(REFERENCE_TEXT, /bump-preflight\.js`\s*\|/);
    assert.match(REFERENCE_TEXT, /bump-decision\.js`\s*\|/);
    assert.match(REFERENCE_TEXT, /read-only/i);
    assert.match(REFERENCE_TEXT, /no\s+judgement|no judgement/i);
  });

  it('documents the approval gate, headless fail-closed path and --bump override', () => {
    assert.match(REFERENCE_TEXT, /auto-proceed/i);
    assert.match(REFERENCE_TEXT, /await-approval/i);
    assert.match(REFERENCE_TEXT, /await-producer-review/i);
    assert.match(REFERENCE_TEXT, /headless/i);
    assert.match(REFERENCE_TEXT, /needsProducerReview/);
    assert.match(REFERENCE_TEXT, /`--bump patch\|minor\|major`[\s\S]{0,200}overrides the evaluation/i);
    assert.match(REFERENCE_TEXT, /`--skip-checks`[\s\S]{0,260}does \*\*not\*\* bypass/i);
  });

  it('documents the decided-bump log line', () => {
    assert.match(REFERENCE_TEXT, /Version bump: <type>/);
    assert.match(REFERENCE_TEXT, /log-only/i);
  });
});

describe('run-release.js decided-bump logging keeps forwarding unchanged', () => {
  it('echoes the decided bump (log-only) without a new side effect', () => {
    assert.match(RUN_RELEASE_SOURCE, /Version bump: \$\{decidedBump\}/);
    assert.match(RUN_RELEASE_SOURCE, /explicit --bump/);
    // The echo must not introduce git/tag/close work of its own.
    const logBlock = RUN_RELEASE_SOURCE.slice(
      RUN_RELEASE_SOURCE.indexOf('Decided-bump logging'),
      RUN_RELEASE_SOURCE.indexOf('const bumpFlagIndex'),
    );
    assert.doesNotMatch(logBlock, /execSync|spawnSync|git |wl close/);
  });

  it('forwards --bump values unchanged (explicit override precedence)', () => {
    assert.deepEqual(
      releaseScriptForwardArgs(['--bump', 'minor']),
      ['--bump', 'minor'],
    );
    assert.deepEqual(
      releaseScriptForwardArgs(['--skip-checks', '--bump', 'major']),
      ['--bump', 'major'],
    );
    // Still an explicitly documented wrapper flag with no filtering of its own.
    assert.match(RUN_RELEASE_SOURCE, /'--bump': 'bump'/);
  });
});
