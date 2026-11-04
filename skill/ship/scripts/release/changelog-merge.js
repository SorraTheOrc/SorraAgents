#!/usr/bin/env node

/**
 * changelog-merge.js — preserve the generated release section across the
 * dev→main merge (SA-0MVXY0VPC002B0MO).
 *
 * `merge-dev-to-main.sh` generates the release's `## v<version> (date)`
 * CHANGELOG.md section (with its `> **Release focus:** …` marker) on the
 * release branch, and THEN merges `origin/dev`. Because dev carries its own
 * top-of-file history (an `## Unreleased` block and previously released
 * sections), the merge conflicts on CHANGELOG.md. The old resolution took
 * dev's whole file (`git checkout --theirs`), silently discarding the
 * generated section — the v0.1.23 regression: `main` shipped without a
 * `## v0.1.23` section, and the Discord embed lost both the release focus and
 * the release notes.
 *
 * This helper merges instead of discarding:
 *
 *   1. extract the generated `## v<version>` section from the release
 *      branch's CHANGELOG.md ("ours"), and
 *   2. re-insert it at the top of dev's CHANGELOG.md ("theirs"), keeping
 *      dev's content intact.
 *
 * The same module exposes the post-merge guard: `guardReleaseSection()` fails
 * when a section that WAS generated is missing from the merged tree, so the
 * script can abort before tagging rather than ship a release without notes.
 * A failed generation (no section was ever produced) keeps the non-fatal
 * contract — the guard does not fire.
 *
 * CLI usage (invoked by merge-dev-to-main.sh):
 *
 *   node changelog-merge.js merge --version <semver> --ours <file> --theirs <file>
 *     Writes the merged CHANGELOG.md content to stdout (exit 0). `--ours`
 *     missing/empty (a failed generation) is non-fatal: dev's content
 *     ("theirs") is written through unchanged and the section-lost outcome is
 *     reported on stderr.
 *
 *   node changelog-merge.js guard --version <semver> --generated <0|1> --file <file>
 *     Exit 0 when the guard passes; exit 1 with an actionable message on
 *     stderr when a section was generated but is missing from <file>.
 *
 * Exported functions are the unit-test surface; the CLI is a thin wrapper.
 */

import { readFileSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Escape a version string for safe inclusion in a RegExp.
 *
 * @param {string} version
 * @returns {string}
 */
function escapeRegExp(version) {
  return version.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Extract the generated release section for `version` from changelog content.
 *
 * The section starts at its `## v<version> (YYYY-MM-DD)` heading and runs to
 * the next `## v` heading (or end of file). The returned text includes the
 * heading (needed to re-insert it verbatim) and any `> **Release focus:** …`
 * marker that follows it.
 *
 * @param {string} changelog - Full CHANGELOG.md content (the release branch's).
 * @param {string} version - Semver version without the leading "v" (e.g. "0.1.23").
 * @returns {string|null} The section text, or null when it is absent.
 */
export function extractReleaseSection(changelog, version) {
  if (typeof changelog !== 'string' || changelog === '' || !version) return null;

  const headerRe = new RegExp(
    `^## v${escapeRegExp(version)} \\([0-9]{4}-[0-9]{2}-[0-9]{2}\\)`,
    'm',
  );
  const headerMatch = changelog.match(headerRe);
  if (!headerMatch) return null;

  const start = headerMatch.index;
  const afterHeading = changelog.slice(start + headerMatch[0].length);
  const nextHeading = afterHeading.match(/^## v/m);
  const end = nextHeading
    ? start + headerMatch[0].length + nextHeading.index
    : changelog.length;

  return changelog.slice(start, end).trimEnd();
}

/**
 * Insert a release section into changelog content, immediately after the
 * top-level heading block. Mirrors `updateChangelog()` in
 * generate-changelog.js so a re-inserted section lands exactly where the
 * generator would have put it.
 *
 * @param {string} changelog - Dev's CHANGELOG.md content ("theirs").
 * @param {string} section - The generated section (see extractReleaseSection).
 * @returns {string} Content with the section inserted at the top.
 */
export function insertReleaseSection(changelog, section) {
  let content = typeof changelog === 'string' ? changelog : '';

  // Ensure the file starts with a top-level heading (matches the generator).
  if (!content.trim()) {
    content = '# Changelog\n\n';
  } else if (!/^#\s/.test(content)) {
    content = '# Changelog\n\n' + content;
  }

  const headingEnd = content.indexOf('\n\n');
  if (headingEnd >= 0) {
    const header = content.substring(0, headingEnd + 2); // include the blank line
    const rest = content.substring(headingEnd + 2);
    return header + section + '\n\n' + rest;
  }

  return content.trimEnd() + '\n\n' + section + '\n\n';
}

/**
 * Merge the generated release section (from the release branch, "ours") into
 * dev's CHANGELOG.md ("theirs").
 *
 * Outcomes:
 *   - `status: 'merged'` — the generated section was found in "ours" and
 *     re-inserted into "theirs"; `sectionPresent` is true.
 *   - `status: 'section-lost'` — no generated `## v<version>` section exists
 *     in "ours" (a failed generation, or a lost section); "theirs" is
 *     returned unchanged and `sectionPresent` is false. This is the outcome
 *     the script-level guard uses to decide whether to abort.
 *
 * @param {object} input
 * @param {string} input.ours - Release-branch CHANGELOG.md content (with the
 *   generated section).
 * @param {string} input.theirs - Dev's CHANGELOG.md content.
 * @param {string} input.version - Released semver version (e.g. "0.1.23").
 * @returns {{ status: 'merged'|'section-lost', content: string, sectionPresent: boolean }}
 */
export function mergeReleaseSection({ ours, theirs, version } = {}) {
  const devContent = typeof theirs === 'string' ? theirs : '';
  const generatedSection = extractReleaseSection(ours, version);

  if (!generatedSection) {
    return { status: 'section-lost', content: devContent, sectionPresent: false };
  }

  const content = insertReleaseSection(devContent, generatedSection);
  const sectionPresent = extractReleaseSection(content, version) !== null;

  return {
    status: sectionPresent ? 'merged' : 'section-lost',
    content,
    sectionPresent,
  };
}

/**
 * Post-merge guard: a generated release section must survive the dev→main
 * merge.
 *
 * When `generated` is false nothing was produced for this release, so the
 * guard passes (a failed generation keeps the non-fatal contract). When
 * `generated` is true the merged content MUST contain the `## v<version>`
 * section; otherwise the release would ship without its changelog body and
 * release focus (the v0.1.23 regression).
 *
 * @param {object} input
 * @param {boolean} input.generated - Whether a release section was generated.
 * @param {string} input.content - The merged CHANGELOG.md content.
 * @param {string} input.version - Released semver version (e.g. "0.1.23").
 * @returns {{ ok: boolean, message: string|null }}
 */
export function guardReleaseSection({ generated, content, version } = {}) {
  if (!generated) {
    return { ok: true, message: null };
  }

  if (extractReleaseSection(content, version)) {
    return { ok: true, message: null };
  }

  return {
    ok: false,
    message:
      `CHANGELOG.md is missing the generated "## v${version}" section after the ` +
      'dev→main merge. The release notes (and any release focus) would be lost ' +
      'from the released tree.',
  };
}

// ── CLI ─────────────────────────────────────────────────────────────────────

function usage() {
  console.error(
    'Usage:\n' +
    '  node changelog-merge.js merge --version <semver> --ours <file> --theirs <file>\n' +
    '  node changelog-merge.js guard --version <semver> --generated <0|1> --file <file>\n',
  );
}

/**
 * Parse `--key value` pairs into a map.
 *
 * @param {string[]} argv
 * @returns {Record<string, string>}
 */
function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) continue;
    const key = arg.slice(2);
    const value = argv[i + 1];
    if (value === undefined || value.startsWith('--')) {
      throw new Error(`missing value for ${arg}`);
    }
    opts[key] = value;
    i += 1;
  }
  return opts;
}

function readFileOrEmpty(path) {
  if (!path) return '';
  try {
    return readFileSync(path, 'utf-8');
  } catch {
    return '';
  }
}

function main() {
  const [command, ...rest] = process.argv.slice(2);

  let opts;
  try {
    opts = parseArgs(rest);
  } catch (err) {
    console.error(`changelog-merge: ${err.message}`);
    usage();
    process.exit(2);
  }

  const version = opts.version;
  if (!version) {
    console.error('changelog-merge: --version is required');
    usage();
    process.exit(2);
  }

  if (command === 'merge') {
    const merged = mergeReleaseSection({
      ours: readFileOrEmpty(opts.ours),
      theirs: readFileOrEmpty(opts.theirs),
      version,
    });

    if (merged.sectionPresent) {
      console.error(
        `changelog-merge: re-inserted the generated "## v${version}" section into dev's CHANGELOG.md`,
      );
    } else {
      console.error(
        `changelog-merge: no generated "## v${version}" section found on the release ` +
        'branch — keeping dev\'s CHANGELOG.md unchanged (section-lost outcome)',
      );
    }

    // Write the merged content to stdout, normalising a trailing newline.
    const out = merged.content.endsWith('\n') ? merged.content : `${merged.content}\n`;
    process.stdout.write(out);
    process.exit(0);
  }

  if (command === 'guard') {
    const generated = opts.generated === '1' || opts.generated === 'true';
    const result = guardReleaseSection({
      generated,
      content: readFileOrEmpty(opts.file),
      version,
    });

    if (result.ok) {
      process.exit(0);
    }

    console.error(result.message);
    process.exit(1);
  }

  usage();
  process.exit(2);
}

// Allow both ESM import and direct CLI execution.
const isMainModule = process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1]));

if (isMainModule) {
  main();
}
