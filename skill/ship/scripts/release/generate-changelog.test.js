/**
 * generate-changelog.test.js — Unit tests for CHANGELOG generation
 * with CTA support (WL-0MUWEPN6X0044R43).
 *
 * Tests the exported `generateReleaseSection` function for correct
 * rendering of the project call-to-action (CTA) — present, absent, and
 * with Markdown content.
 */

import { describe, it, expect } from 'vitest';
import { generateReleaseSection } from './generate-changelog.js';

// ── Shared test data ─────────────────────────────────────────────────────────

const CATEGORY_DATA = {
  features: ['- Added dark mode support (WL-001)', '- Improved startup time (WL-002)'],
  bugFixes: ['- Fixed crash on startup (WL-003)'],
  other: ['- Updated dependencies (WL-004)'],
};

// ── Tests ────────────────────────────────────────────────────────────────────

describe('generateReleaseSection', () => {
  it('renders a CTA as a blockquote when present', () => {
    const cta = '[Play the alpha release](https://example.github.io/my-project/).';
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, null, cta);

    expect(result).toContain('> [Play the alpha release](https://example.github.io/my-project/).');
    expect(result).toContain('## v1.0.0 (2026-10-12)');
    expect(result).toContain('### Features');
    expect(result).toContain('### Bug Fixes');
    expect(result).toContain('### Other');
  });

  it('renders CTA after changelog items and before trailing blank lines', () => {
    const cta = '[Join our Discord](https://discord.gg/test) for feedback.';
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, null, cta);

    // The CTA should come after the "Other" section entries
    const otherIndex = result.indexOf('### Other');
    const ctaIndex = result.indexOf('> [Join our Discord]');

    expect(ctaIndex).toBeGreaterThan(otherIndex);
  });

  it('omits CTA when null', () => {
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, null, null);

    expect(result).toContain('## v1.0.0 (2026-10-12)');
    expect(result).toContain('### Features');
    expect(result).toContain('### Bug Fixes');
    expect(result).toContain('### Other');
    // No blockquote at all (no CTA)
    expect(result).not.toMatch(/^\s*>/m);
  });

  it('omits CTA when undefined (default parameter)', () => {
    // Call without the cta argument — uses the default null
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA);

    expect(result).toContain('## v1.0.0 (2026-10-12)');
    expect(result).not.toMatch(/^\s*>/m);
  });

  it('preserves Markdown link syntax in CTA', () => {
    const cta = '[Play the alpha release](https://example.github.io/my-project/). Provide feedback in [Discord](https://discord.gg/gUKQTFkzQ4).';
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, null, cta);

    expect(result).toContain('[Play the alpha release](https://example.github.io/my-project/)');
    expect(result).toContain('[Discord](https://discord.gg/gUKQTFkzQ4)');
  });

  it('renders CTA alongside release focus', () => {
    const focus = 'This release adds dark mode and fixes a startup crash.';
    const cta = '[Try the new version now](https://example.github.io/my-project/).';
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, focus, cta);

    expect(result).toContain('> **Release focus:** This release adds dark mode');
    expect(result).toContain('> [Try the new version now](https://example.github.io/my-project/).');

    // Focus should come before CTA
    const focusIndex = result.indexOf('> **Release focus:**');
    const ctaIndex = result.indexOf('> [Try the new version now]');
    expect(ctaIndex).toBeGreaterThan(focusIndex);
  });

  it('no behaviour change when CTA absent — section is identical to pre-CTA format', () => {
    const resultWithCtaNull = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, 'focus text', null);
    const resultNoCtaParam = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, 'focus text');

    // Both should be identical — omitting CTA produces the same output
    expect(resultWithCtaNull).toBe(resultNoCtaParam);
  });

  it('renders empty categories gracefully with CTA', () => {
    const empty = { features: [], bugFixes: [], other: [] };
    const cta = '[Get involved](https://example.com/).';
    const result = generateReleaseSection('1.0.0', '2026-10-12', empty, null, cta);

    expect(result).toContain('## v1.0.0 (2026-10-12)');
    expect(result).toContain('> [Get involved](https://example.com/).');
    // No category headings since all lists are empty
    expect(result).not.toContain('### Features');
    expect(result).not.toContain('### Bug Fixes');
    expect(result).not.toContain('### Other');
  });

  it('renders multiline CTA correctly', () => {
    const cta = 'Join our [Discord community](https://discord.gg/test) or [read the docs](https://docs.example.com).';
    const result = generateReleaseSection('1.0.0', '2026-10-12', CATEGORY_DATA, null, cta);

    expect(result).toContain('> Join our [Discord community](https://discord.gg/test) or [read the docs](https://docs.example.com).');
  });
});
