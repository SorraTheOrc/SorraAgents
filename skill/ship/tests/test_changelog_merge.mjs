/**
 * Tests for changelog-merge.js (SA-0MVXY0VPC002B0MO).
 *
 * Covers the dev→main CHANGELOG.md merge-resolution helper and the
 * post-merge guard:
 *   - AC1: with dev's CHANGELOG.md carrying an `## Unreleased` block, the
 *     merge path yields the generated `## v<version>` section (with its
 *     `> **Release focus:**` marker) AND keeps dev's content.
 *   - AC2: a generated section that is missing is reported as the
 *     `section-lost` outcome, and the guard (the script's abort gate) exits
 *     non-zero with an actionable message naming the version.
 *   - AC3: a failed generation (no section was ever produced) keeps the
 *     non-fatal contract — the guard does not fire.
 *
 * Node.js built-in `node --test` runner; no network, no git, no live worklog.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const MODULE_PATH = join(__dirname, '..', 'scripts', 'release', 'changelog-merge.js');

const {
  extractReleaseSection,
  insertReleaseSection,
  mergeReleaseSection,
  guardReleaseSection,
} = await import(MODULE_PATH);

// ── Fixtures ─────────────────────────────────────────────────────────────────

/** A dev-style CHANGELOG.md: `## Unreleased` at the top + prior releases. */
const DEV_CHANGELOG = `# Changelog

## Unreleased

### Added

- Work-in-progress feature (SA-DEV1)

## v0.1.22 (2026-10-15)

### Features

- Previous feature (SA-PREV1)

### Bug Fixes

- Previous fix (SA-PREV2)
`;

/** The generated release section (as generate-changelog.js writes it). */
const GENERATED_SECTION = `## v0.1.23 (2026-11-01)
> **Release focus:** The changelog-preservation fix release.
### Bug Fixes
- Releases keep their changelog section after the dev→main merge. (SA-0MVXY0VPC002B0MO)`;

/** The release branch's CHANGELOG.md: generated section above prior history. */
const RELEASE_CHANGELOG = `# Changelog

${GENERATED_SECTION}

## v0.1.22 (2026-10-15)

### Features

- Previous feature (SA-PREV1)
`;

/** Create a temporary directory and return its path. */
function mkTmpDir(prefix = 'changelog-merge-test-') {
  return mkdtempSync(join(tmpdir(), prefix));
}

/** Clean up a temporary directory. */
function rmTmpDir(dir) {
  rmSync(dir, { recursive: true, force: true });
}

/** Write `content` to `name` inside `dir` and return the path. */
function writeFixture(dir, name, content) {
  const path = join(dir, name);
  writeFileSync(path, content);
  return path;
}

// ─── extractReleaseSection ──────────────────────────────────────────────────

describe('extractReleaseSection', () => {
  it('returns the generated section including its heading and focus marker', () => {
    const section = extractReleaseSection(RELEASE_CHANGELOG, '0.1.23');
    assert.ok(section, 'the generated section must be found');
    assert.ok(section.startsWith('## v0.1.23 (2026-11-01)'));
    assert.ok(section.includes('> **Release focus:** The changelog-preservation fix release.'));
    assert.ok(section.includes('SA-0MVXY0VPC002B0MO'));
  });

  it('stops at the next ## v heading (does not swallow prior releases)', () => {
    const section = extractReleaseSection(RELEASE_CHANGELOG, '0.1.23');
    assert.ok(!section.includes('## v0.1.22'), 'the next release must not be included');
    assert.ok(!section.includes('SA-PREV1'));
  });

  it('handles a section at the end of the file (no next heading)', () => {
    const section = extractReleaseSection(DEV_CHANGELOG.replace(/## v0\.1\.22[\s\S]*$/m, GENERATED_SECTION), '0.1.23');
    assert.ok(section);
    assert.ok(section.includes('SA-0MVXY0VPC002B0MO'));
  });

  it('returns null when the version section is absent', () => {
    assert.strictEqual(extractReleaseSection(DEV_CHANGELOG, '9.9.9'), null);
  });

  it('returns null for empty content, a missing version, or non-string input', () => {
    assert.strictEqual(extractReleaseSection('', '0.1.23'), null);
    assert.strictEqual(extractReleaseSection(RELEASE_CHANGELOG, ''), null);
    assert.strictEqual(extractReleaseSection(RELEASE_CHANGELOG, null), null);
    assert.strictEqual(extractReleaseSection(null, '0.1.23'), null);
  });

  it('escapes regex special characters in the version', () => {
    const changelog = '## v1.0.0-beta.1 (2026-03-01)\n### Features\n- Beta\n';
    const section = extractReleaseSection(changelog, '1.0.0-beta.1');
    assert.ok(section);
    assert.ok(section.includes('### Features'));
  });
});

// ─── insertReleaseSection ───────────────────────────────────────────────────

describe('insertReleaseSection', () => {
  it('inserts the section immediately after the top-level heading', () => {
    const merged = insertReleaseSection(DEV_CHANGELOG, GENERATED_SECTION);
    assert.ok(merged.startsWith('# Changelog\n\n## v0.1.23 (2026-11-01)'));
  });

  it('keeps dev content intact after the inserted section', () => {
    const merged = insertReleaseSection(DEV_CHANGELOG, GENERATED_SECTION);
    assert.ok(merged.includes('## Unreleased'));
    assert.ok(merged.includes('SA-DEV1'));
    assert.ok(merged.includes('## v0.1.22 (2026-10-15)'));
    assert.ok(merged.includes('SA-PREV1'));
    assert.ok(merged.includes('SA-PREV2'));
  });

  it('prepends a top-level heading when the file lacks one', () => {
    const merged = insertReleaseSection('## Unreleased\n\n- x\n', GENERATED_SECTION);
    assert.ok(merged.startsWith('# Changelog\n\n## v0.1.23 (2026-11-01)'));
  });

  it('creates a minimal changelog for empty input', () => {
    const merged = insertReleaseSection('', GENERATED_SECTION);
    assert.ok(merged.startsWith('# Changelog\n\n## v0.1.23 (2026-11-01)'));
  });
});

// ─── mergeReleaseSection (AC1 / AC2 helper outcome) ─────────────────────────

describe('mergeReleaseSection', () => {
  it('AC1: preserves the generated section AND dev content when dev has an Unreleased block', () => {
    const result = mergeReleaseSection({
      ours: RELEASE_CHANGELOG,
      theirs: DEV_CHANGELOG,
      version: '0.1.23',
    });

    assert.strictEqual(result.status, 'merged');
    assert.strictEqual(result.sectionPresent, true);

    // The generated release section survives with its focus marker.
    assert.ok(result.content.includes('## v0.1.23 (2026-11-01)'));
    assert.ok(result.content.includes('> **Release focus:** The changelog-preservation fix release.'));

    // Dev's content is kept (the whole point of taking dev's version).
    assert.ok(result.content.includes('## Unreleased'));
    assert.ok(result.content.includes('SA-DEV1'));
    assert.ok(result.content.includes('## v0.1.22 (2026-10-15)'));
    assert.ok(result.content.includes('SA-PREV2'));
  });

  it('places the generated section above dev\'s Unreleased block', () => {
    const result = mergeReleaseSection({
      ours: RELEASE_CHANGELOG,
      theirs: DEV_CHANGELOG,
      version: '0.1.23',
    });
    assert.ok(
      result.content.indexOf('## v0.1.23 (2026-11-01)') < result.content.indexOf('## Unreleased'),
      'the released version is the newest section',
    );
  });

  it('AC2 helper outcome: reports section-lost and keeps dev content when ours has no section', () => {
    // This models a failed generation (or a lost section): the release branch
    // carries no generated `## v0.1.23` section, so nothing can be re-inserted.
    const result = mergeReleaseSection({
      ours: '# Changelog\n\n## v0.1.22 (2026-10-15)\n### Features\n- old\n',
      theirs: DEV_CHANGELOG,
      version: '0.1.23',
    });

    assert.strictEqual(result.status, 'section-lost');
    assert.strictEqual(result.sectionPresent, false);
    assert.strictEqual(result.content, DEV_CHANGELOG, 'dev content is passed through unchanged');
  });

  it('treats null/empty inputs safely', () => {
    const result = mergeReleaseSection({ ours: null, theirs: null, version: '0.1.23' });
    assert.strictEqual(result.status, 'section-lost');
    assert.strictEqual(result.content, '');
  });
});

// ─── guardReleaseSection (AC2 script gate / AC3 failed generation) ─────────

describe('guardReleaseSection', () => {
  it('passes when a generated section is present in the merged content', () => {
    const merged = mergeReleaseSection({
      ours: RELEASE_CHANGELOG,
      theirs: DEV_CHANGELOG,
      version: '0.1.23',
    });
    const guard = guardReleaseSection({
      generated: true,
      content: merged.content,
      version: '0.1.23',
    });
    assert.strictEqual(guard.ok, true);
    assert.strictEqual(guard.message, null);
  });

  it('AC2 script gate: fails with an actionable, version-naming message when a generated section is missing', () => {
    const guard = guardReleaseSection({
      generated: true,
      content: DEV_CHANGELOG, // the discarded-section outcome
      version: '0.1.23',
    });
    assert.strictEqual(guard.ok, false);
    assert.ok(guard.message.includes('## v0.1.23'));
    assert.ok(/aborting|abort/i.test(guard.message) || /missing/i.test(guard.message));
  });

  it('AC3: does not fire for a section that was never generated', () => {
    const guard = guardReleaseSection({
      generated: false,
      content: DEV_CHANGELOG,
      version: '0.1.23',
    });
    assert.strictEqual(guard.ok, true);
    assert.strictEqual(guard.message, null);
  });
});

// ─── CLI (the bash script boundary) ─────────────────────────────────────────

describe('changelog-merge CLI', () => {
  it('merge: writes the merged content to stdout and exits 0', () => {
    const dir = mkTmpDir();
    try {
      const oursPath = writeFixture(dir, 'ours.md', RELEASE_CHANGELOG);
      const theirsPath = writeFixture(dir, 'theirs.md', DEV_CHANGELOG);

      const res = spawnSync(
        process.execPath,
        [MODULE_PATH, 'merge', '--version', '0.1.23', '--ours', oursPath, '--theirs', theirsPath],
        { encoding: 'utf-8' },
      );

      assert.strictEqual(res.status, 0, res.stderr);
      assert.ok(res.stdout.includes('## v0.1.23 (2026-11-01)'));
      assert.ok(res.stdout.includes('> **Release focus:**'));
      assert.ok(res.stdout.includes('## Unreleased'));
      assert.ok(res.stdout.includes('SA-DEV1'));
      // Diagnostics go to stderr, never stdout, so the stdout is a clean file.
      assert.ok(!res.stdout.includes('changelog-merge:'));
    } finally {
      rmTmpDir(dir);
    }
  });

  it('merge: keeps dev content and still exits 0 when no section was generated', () => {
    const dir = mkTmpDir();
    try {
      const oursPath = writeFixture(dir, 'ours.md', '# Changelog\n');
      const theirsPath = writeFixture(dir, 'theirs.md', DEV_CHANGELOG);

      const res = spawnSync(
        process.execPath,
        [MODULE_PATH, 'merge', '--version', '0.1.23', '--ours', oursPath, '--theirs', theirsPath],
        { encoding: 'utf-8' },
      );

      assert.strictEqual(res.status, 0);
      assert.strictEqual(res.stdout, DEV_CHANGELOG);
      assert.ok(/section-lost|no generated/.test(res.stderr));
    } finally {
      rmTmpDir(dir);
    }
  });

  it('AC2 script gate: guard exits 1 with a version-naming message when the section is missing', () => {
    const dir = mkTmpDir();
    try {
      const filePath = writeFixture(dir, 'CHANGELOG.md', DEV_CHANGELOG);

      const res = spawnSync(
        process.execPath,
        [MODULE_PATH, 'guard', '--version', '0.1.23', '--generated', '1', '--file', filePath],
        { encoding: 'utf-8' },
      );

      assert.strictEqual(res.status, 1, 'a lost generated section must abort the release');
      assert.ok(res.stderr.includes('## v0.1.23'));
    } finally {
      rmTmpDir(dir);
    }
  });

  it('AC2 script gate: guard exits 0 when the generated section is present', () => {
    const dir = mkTmpDir();
    try {
      const merged = mergeReleaseSection({
        ours: RELEASE_CHANGELOG,
        theirs: DEV_CHANGELOG,
        version: '0.1.23',
      });
      const filePath = writeFixture(dir, 'CHANGELOG.md', merged.content);

      const res = spawnSync(
        process.execPath,
        [MODULE_PATH, 'guard', '--version', '0.1.23', '--generated', '1', '--file', filePath],
        { encoding: 'utf-8' },
      );

      assert.strictEqual(res.status, 0, res.stderr);
    } finally {
      rmTmpDir(dir);
    }
  });

  it('AC3: guard exits 0 when generation failed (generated=0)', () => {
    const dir = mkTmpDir();
    try {
      const filePath = writeFixture(dir, 'CHANGELOG.md', DEV_CHANGELOG);

      const res = spawnSync(
        process.execPath,
        [MODULE_PATH, 'guard', '--version', '0.1.23', '--generated', '0', '--file', filePath],
        { encoding: 'utf-8' },
      );

      assert.strictEqual(res.status, 0, 'a failed generation must not block the release');
    } finally {
      rmTmpDir(dir);
    }
  });
});
