/**
 * Unit tests for release-focus generation in
 * skill/ship/scripts/release/generate-changelog.js (SA-0MUVWKZQL0089GCB).
 *
 * Covers: focus generated from the categorised items and emitted as a
 * `> **Release focus:** …` marker directly under the version heading, focus
 * omitted when the LLM is unavailable, sentence clamping to 1–3 sentences,
 * and preservation of the existing changelog structure.
 *
 * The LLM boundary is injected via opts.fetchFn (no live network, no API key
 * required at rest).
 */

import { describe, test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const MODULE_PATH = join(
  REPO_ROOT,
  'skill', 'ship', 'scripts', 'release', 'generate-changelog.js',
);

/** A fetch stub returning a fixed assistant message. */
function stubFetch(content) {
  return async () => ({
    ok: true,
    json: async () => ({ choices: [{ message: { content } }] }),
  });
}

const CATEGORIZED = {
  features: ['- Add co-op mode (SA-1)'],
  bugFixes: ['- Fix login crash (SA-2)'],
  other: ['- Update dependencies (SA-3)'],
};

let savedKey;
beforeEach(() => { savedKey = process.env.DEEPSEEK_API_KEY; });
afterEach(() => {
  if (savedKey === undefined) delete process.env.DEEPSEEK_API_KEY;
  else process.env.DEEPSEEK_API_KEY = savedKey;
});

describe('generateReleaseFocus - LLM success and failure', () => {
  test('returns the LLM focus when the call succeeds', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { generateReleaseFocus } = await import(MODULE_PATH);

    const focus = await generateReleaseFocus(CATEGORIZED, {
      fetchFn: stubFetch('This release adds co-op mode and fixes a login crash.'),
    });

    assert.equal(
      focus,
      'This release adds co-op mode and fixes a login crash.',
      'focus should be the trimmed LLM content',
    );
  });

  test('returns null when no API key is configured', async () => {
    delete process.env.DEEPSEEK_API_KEY;
    const { generateReleaseFocus } = await import(MODULE_PATH);

    let called = false;
    const focus = await generateReleaseFocus(CATEGORIZED, {
      fetchFn: async () => { called = true; return { ok: true, json: async () => ({}) }; },
    });

    assert.equal(focus, null, 'missing key should yield null');
    assert.equal(called, false, 'fetch must not run without a key');
  });

  test('returns null when the LLM call fails', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { generateReleaseFocus } = await import(MODULE_PATH);

    const focus = await generateReleaseFocus(CATEGORIZED, {
      fetchFn: async () => { throw new Error('network down'); },
    });

    assert.equal(focus, null, 'failed LLM call should yield null');
  });

  test('returns null when there are no categorised items', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { generateReleaseFocus } = await import(MODULE_PATH);

    const focus = await generateReleaseFocus(
      { features: [], bugFixes: [], other: [] },
      { fetchFn: stubFetch('Should not be used.') },
    );

    assert.equal(focus, null, 'no items should yield null without an LLM call');
  });

  test('clamps an over-long focus to at most three sentences', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { generateReleaseFocus, RELEASE_FOCUS_MAX_SENTENCES } =
      await import(MODULE_PATH);

    const focus = await generateReleaseFocus(CATEGORIZED, {
      fetchFn: stubFetch('One. Two. Three. Four. Five.'),
    });

    assert.equal(focus, 'One. Two. Three.', 'focus should be clamped to 3 sentences');
    assert.equal(RELEASE_FOCUS_MAX_SENTENCES, 3);
  });
});

describe('generateReleaseSection - focus marker placement and structure', () => {
  test('emits the release-focus marker directly under the version heading', async () => {
    const { generateReleaseSection } = await import(MODULE_PATH);

    const section = generateReleaseSection(
      '0.2.0', '2026-10-06', CATEGORIZED, 'Co-op mode lands.',
    );
    const lines = section.split('\n');

    assert.equal(lines[0], '## v0.2.0 (2026-10-06)', 'first line is the version heading');
    assert.equal(lines[1], '> **Release focus:** Co-op mode lands.', 'marker directly under the heading');
  });

  test('preserves the existing feature/bug-fix/other structure alongside the marker', async () => {
    const { generateReleaseSection } = await import(MODULE_PATH);

    const section = generateReleaseSection(
      '0.2.0', '2026-10-06', CATEGORIZED, 'Co-op mode lands.',
    );

    assert.ok(section.includes('### Features'), 'Features section preserved');
    assert.ok(section.includes('- Add co-op mode (SA-1)'));
    assert.ok(section.includes('### Bug Fixes'), 'Bug Fixes section preserved');
    assert.ok(section.includes('- Fix login crash (SA-2)'));
    assert.ok(section.includes('### Other'), 'Other section preserved');
    assert.ok(section.includes('- Update dependencies (SA-3)'));
  });

  test('omits the marker and renders the section unchanged when focus is null', async () => {
    const { generateReleaseSection } = await import(MODULE_PATH);

    const section = generateReleaseSection('0.2.0', '2026-10-06', CATEGORIZED);

    assert.ok(!section.includes('Release focus'), 'no marker when focus is absent');
    assert.ok(section.startsWith('## v0.2.0 (2026-10-06)\n### Features'), 'heading followed straight by sections');
  });
});
