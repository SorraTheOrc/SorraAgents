/**
 * Tests for bump-preflight.js (SA-0MWFVQWGN006G4KA).
 *
 * The bump preflight is the read-only evidence gatherer used by the ship
 * skill's version-bump evaluation step: it collects every release candidate
 * (`stage: in_review`), enriches each with the fields needed to judge release
 * importance, and renders a review packet whose patch/minor/major outcomes
 * come from the existing `bump-version.js` arithmetic.
 *
 * Covers:
 *   - candidate gathering through the injected fetch boundary (never the live
 *     worklog), including the detail-enrichment fallback for thin projections;
 *   - field tolerance (missing id/title/description never throws);
 *   - rendering of every packet field (version, three outcomes, per-item
 *     evidence);
 *   - the empty and unreadable candidate sets failing safe to `patch` with a
 *     warning;
 *   - minor/major resetting the lower components (delegated to bumpVersion);
 *   - the read-only contract (no git/mutation side effects in the module).
 *
 * Node.js built-in `node --test` runner; no network, no live worklog, no real
 * git state.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const MODULE_PATH = join(__dirname, '..', 'scripts', 'release', 'bump-preflight.js');

const {
  DEFAULT_EXCERPT_LENGTH,
  enrichCandidate,
  gatherReleaseCandidates,
  renderReviewPacket,
  runBumpPreflight,
} = await import(MODULE_PATH);

// ── Fixtures ─────────────────────────────────────────────────────────────────

/** Injectable version source: never touches the real repository. */
function versionSource(version) {
  return () => ({ kind: 'package.json', path: '/fixture/package.json', version });
}

/** Capture `logWarning` calls so the fail-safe warning is asserted, not spammed. */
function warningSink() {
  const messages = [];
  return { messages, logWarning: (message) => messages.push(message) };
}

/** A rich candidate, as a fully-projecting fetch boundary would return it. */
const RICH_CANDIDATE = {
  id: 'SA-RICH1',
  title: 'Add deterministic replay',
  issueType: 'feature',
  priority: 'high',
  description: [
    '## Objective',
    '',
    'Replays become deterministic.',
    '',
    '## Impact',
    '',
    'Users can replay any match and get identical results every time.',
    '',
    '## Notes',
    '',
    'Implementation detail that must not leak into the impact summary.',
  ].join('\n'),
};

// ── Candidate gathering ──────────────────────────────────────────────────────

describe('gatherReleaseCandidates', () => {
  it('resolves candidates through the injected boundary and enriches every field', () => {
    let detailCalls = 0;
    const { candidates, warning } = gatherReleaseCandidates({
      getCandidateItemsFn: () => [RICH_CANDIDATE],
      getItemDetailsFn: () => {
        detailCalls += 1;
        return null;
      },
    });

    assert.equal(warning, null);
    assert.equal(detailCalls, 0, 'a rich projection needs no detail round-trip');
    assert.equal(candidates.length, 1);
    assert.equal(candidates[0].id, 'SA-RICH1');
    assert.equal(candidates[0].title, 'Add deterministic replay');
    assert.equal(candidates[0].issueType, 'feature');
    assert.equal(candidates[0].priority, 'high');
    assert.equal(
      candidates[0].impact,
      'Users can replay any match and get identical results every time.',
    );
    assert.doesNotMatch(candidates[0].impact, /Implementation detail/);
    assert.match(candidates[0].descriptionExcerpt, /Replays become deterministic/);
  });

  it('fetches details through the injected boundary when the projection is thin', () => {
    const detailIds = [];
    const { candidates } = gatherReleaseCandidates({
      // Mirrors check-audit-gate.js getCandidateItems(): id/title only.
      getCandidateItemsFn: () => [{ id: 'SA-THIN1', title: 'Thin projection title' }],
      getItemDetailsFn: (id) => {
        detailIds.push(id);
        return {
          id,
          issueType: 'bug',
          priority: 'critical',
          description: '## User Story\n\nAs a player I want no crashes.',
        };
      },
    });

    assert.deepEqual(detailIds, ['SA-THIN1']);
    assert.equal(candidates.length, 1);
    assert.equal(candidates[0].title, 'Thin projection title');
    assert.equal(candidates[0].issueType, 'bug');
    assert.equal(candidates[0].priority, 'critical');
    assert.equal(candidates[0].impact, 'As a player I want no crashes.');
  });

  it('tolerates missing fields: falls back to the id/title and an empty excerpt', () => {
    const bare = enrichCandidate({ id: 'SA-BARE1' });

    assert.equal(bare.id, 'SA-BARE1');
    assert.equal(bare.title, 'SA-BARE1');
    assert.equal(bare.issueType, 'unknown');
    assert.equal(bare.priority, 'unknown');
    assert.equal(bare.impact, 'SA-BARE1');
    assert.equal(bare.descriptionExcerpt, '');

    const { candidates } = gatherReleaseCandidates({
      getCandidateItemsFn: () => [{ id: 'SA-BARE2' }],
      getItemDetailsFn: () => null,
    });
    assert.equal(candidates[0].title, 'SA-BARE2');
    assert.equal(candidates[0].descriptionExcerpt, '');
  });

  it('truncates the description excerpt (default and custom lengths)', () => {
    const longDescription = 'x'.repeat(DEFAULT_EXCERPT_LENGTH + 80);

    const byDefault = enrichCandidate({
      id: 'SA-LONG1',
      title: 'Long description',
      description: longDescription,
    });
    assert.ok(
      byDefault.descriptionExcerpt.length <= DEFAULT_EXCERPT_LENGTH,
      `excerpt should be capped at ${DEFAULT_EXCERPT_LENGTH} chars`,
    );
    assert.ok(byDefault.descriptionExcerpt.endsWith('…'));

    const custom = enrichCandidate(
      { id: 'SA-LONG2', description: longDescription },
      { excerptLength: 20 },
    );
    assert.ok(custom.descriptionExcerpt.length <= 20);
    assert.ok(custom.descriptionExcerpt.endsWith('…'));
  });
});

// ── Review packet ────────────────────────────────────────────────────────────

describe('renderReviewPacket', () => {
  it('renders the current version, every outcome and the per-item evidence', () => {
    const result = runBumpPreflight({
      getCandidateItemsFn: () => [RICH_CANDIDATE],
      getItemDetailsFn: () => null,
      resolveVersionSourceFn: versionSource('1.2.3'),
      logWarning: () => {},
    });

    assert.equal(result.currentVersion, '1.2.3');
    assert.deepEqual(result.proposals, {
      patch: '1.2.4',
      minor: '1.3.0',
      major: '2.0.0',
    });
    assert.equal(result.proposedBump, 'patch');

    const { packet } = result;
    assert.match(packet, /Current version: 1\.2\.3/);
    assert.match(packet, /patch[^\n]*1\.2\.4/);
    assert.match(packet, /minor[^\n]*1\.3\.0/);
    assert.match(packet, /major[^\n]*2\.0\.0/);
    assert.match(packet, /SA-RICH1/);
    assert.match(packet, /Add deterministic replay/);
    assert.match(packet, /feature/);
    assert.match(packet, /high/);
    assert.match(packet, /Users can replay any match/);
    assert.match(packet, /Replays become deterministic/);
    assert.equal(result.warning, null);
  });

  it('delegates minor/major lower-component resets to bumpVersion', () => {
    const result = runBumpPreflight({
      getCandidateItemsFn: () => [RICH_CANDIDATE],
      resolveVersionSourceFn: versionSource('0.4.7'),
      logWarning: () => {},
    });

    // 0.4.7 → patch 0.4.8 | minor resets z | major resets y and z
    assert.equal(result.proposals.patch, '0.4.8');
    assert.equal(result.proposals.minor, '0.5.0');
    assert.equal(result.proposals.major, '1.0.0');
  });
});

// ── Fail-safe paths ──────────────────────────────────────────────────────────

describe('fail-safe candidate sets', () => {
  it('defaults an empty candidate set to patch and warns', () => {
    const sink = warningSink();
    const result = runBumpPreflight({
      getCandidateItemsFn: () => [],
      resolveVersionSourceFn: versionSource('0.4.2'),
      logWarning: sink.logWarning,
    });

    assert.equal(result.proposedBump, 'patch');
    assert.equal(result.proposals.patch, '0.4.3');
    assert.equal(result.candidates.length, 0);
    assert.match(result.warning, /no release candidates/i);
    assert.equal(sink.messages.length, 1);
    assert.match(sink.messages[0], /no release candidates/i);
    assert.match(result.packet, /no release candidates/i);
  });

  it('defaults an unreadable candidate set to patch, warns and never throws', () => {
    const sink = warningSink();
    const result = runBumpPreflight({
      getCandidateItemsFn: () => {
        throw new Error('wl exploded');
      },
      resolveVersionSourceFn: versionSource('0.4.2'),
      logWarning: sink.logWarning,
    });

    assert.equal(result.proposedBump, 'patch');
    assert.equal(result.proposals.patch, '0.4.3');
    assert.match(result.warning, /could not read release candidates/i);
    assert.match(result.warning, /wl exploded/);
    assert.equal(sink.messages.length, 1);
  });

  it('treats a non-array candidate result as unreadable', () => {
    const sink = warningSink();
    const result = runBumpPreflight({
      getCandidateItemsFn: () => ({ workItems: [] }),
      resolveVersionSourceFn: versionSource('0.4.2'),
      logWarning: sink.logWarning,
    });

    assert.equal(result.proposedBump, 'patch');
    assert.match(result.warning, /could not read release candidates/i);
  });

  it('still reports the candidates when the version source is unreadable', () => {
    const sink = warningSink();
    const result = runBumpPreflight({
      getCandidateItemsFn: () => [RICH_CANDIDATE],
      resolveVersionSourceFn: () => {
        throw new Error('no version source');
      },
      logWarning: sink.logWarning,
    });

    assert.equal(result.currentVersion, null);
    assert.deepEqual(result.proposals, { patch: null, minor: null, major: null });
    assert.match(result.warning, /could not compute version proposals/i);
    assert.match(result.packet, /SA-RICH1/);
    assert.equal(sink.messages.length, 1);
  });
});

// ── Read-only contract ───────────────────────────────────────────────────────

describe('read-only contract', () => {
  it('performs no git/tag/PR/worklog mutation and reuses the shared helpers', () => {
    const source = readFileSync(MODULE_PATH, 'utf-8');

    for (const mutation of [
      'writeFileSync',
      'unlinkSync',
      'rmSync',
      'mkdirSync',
      'appendFileSync',
      'renameSync',
      'spawnSync',
    ]) {
      assert.ok(
        !source.includes(mutation),
        `bump-preflight.js must stay read-only (found ${mutation})`,
      );
    }
    assert.doesNotMatch(source, /execSync\(\s*[`'"]git/);
    assert.doesNotMatch(source, /wl (close|update|comment|create|delete|sync|import)/);

    // Candidate boundary and semver arithmetic are reused, not duplicated.
    assert.match(source, /getCandidateItems/);
    assert.match(source, /resolveVersionSource/);
    assert.match(source, /bumpVersion/);
    assert.doesNotMatch(source, /major \+ 1|minor \+ 1|patch \+ 1/);
  });
});
