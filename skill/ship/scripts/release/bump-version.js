#!/usr/bin/env node

/**
 * bump-version.js — Version increment helper for the ship release process.
 *
 * Resolves the repository's version source by convention — `package.json` in
 * the repository root when present, else `pyproject.toml`'s
 * `[project].version` — increments it according to the specified bump type,
 * writes the new version back, and prints the new version to stdout. Python
 * projects (no `package.json`) can therefore be released through the same
 * canonical pipeline as Node ones (SA-0MVN2B0DJ00135W0).
 *
 * Usage:
 *   node bump-version.js [--bump patch|minor|major]
 *
 * Options:
 *   --bump <type>   Which part of the version to increment (default: patch)
 *   --dry-run       Print the new version without modifying the version file
 *   -h, --help      Show this help message
 *
 * Exit codes:
 *   0  Success (new version printed to stdout)
 *   1  Error (invalid arguments, no version source, parse error)
 */

import { readFileSync, writeFileSync, existsSync, realpathSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// ── Resolve repo root ──────────────────────────────────────────────────────
// Use `git rev-parse --show-toplevel` so the script works regardless of
// where it is installed — locally inside a repo tree or globally via a
// skill manager at ~/.pi/agent/skills/ship/…
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
const PKG_PATH = resolve(REPO_ROOT, 'package.json');
const PYPROJECT_PATH = resolve(REPO_ROOT, 'pyproject.toml');

// ── bumpVersion ────────────────────────────────────────────────────────────

/**
 * Increment a semantic version string.
 *
 * @param {string} versionString - A semver string (e.g. "0.1.0").
 * @param {string} [bumpType='patch'] - One of 'patch', 'minor', 'major'.
 * @returns {string} The incremented version string.
 * @throws {Error} If the version string or bump type is invalid.
 */
export function bumpVersion(versionString, bumpType = 'patch') {
  if (!versionString || typeof versionString !== 'string') {
    throw new Error('Invalid version string: must be a non-empty string');
  }

  // Strip optional pre-release / build metadata for bumping (e.g. "1.0.0-alpha.1" → "1.0.0")
  const baseVersion = versionString.split('-')[0].split('+')[0];

  // Validate semver format: exactly three dot-separated numeric segments
  // Also reject leading zeros on any segment (e.g. "01.2.3")
  const semverRegex = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
  const match = baseVersion.match(semverRegex);

  if (!match) {
    throw new Error(
      `Cannot parse version string "${versionString}". Expected a valid semver format (e.g. "0.1.0").`,
    );
  }

  const major = parseInt(match[1], 10);
  const minor = parseInt(match[2], 10);
  const patch = parseInt(match[3], 10);

  switch (bumpType) {
    case 'major':
      return `${major + 1}.0.0`;
    case 'minor':
      return `${major}.${minor + 1}.0`;
    case 'patch':
      return `${major}.${minor}.${patch + 1}`;
    default:
      throw new Error(
        `Invalid bump type "${bumpType}". Must be one of: patch, minor, major.`,
      );
  }
}

// ── readVersion ────────────────────────────────────────────────────────────

/**
 * Read the version from package.json.
 *
 * @param {string} [pkgPath=PKG_PATH] - Path to package.json.
 * @returns {string} The current version string.
 * @throws {Error} If the file doesn't exist, can't be parsed, or has no version field.
 */
export function readVersion(pkgPath = PKG_PATH) {
  if (!existsSync(pkgPath)) {
    throw new Error(`package.json not found at ${pkgPath}`);
  }

  let pkg;
  try {
    const content = readFileSync(pkgPath, 'utf-8');
    pkg = JSON.parse(content);
  } catch (err) {
    throw new Error(
      `Failed to read or parse ${pkgPath}: ${err.message}`,
    );
  }

  if (!pkg.version || typeof pkg.version !== 'string') {
    throw new Error(
      `No "version" field found in ${pkgPath}. Add one or set a base version first.`,
    );
  }

  return pkg.version;
}

// ── writeVersion ───────────────────────────────────────────────────────────

/**
 * Write a new version to package.json, preserving all other fields.
 *
 * @param {string} newVersion - The new version string to write.
 * @param {string} [pkgPath=PKG_PATH] - Path to package.json.
 * @throws {Error} If the file can't be written.
 */
export function writeVersion(newVersion, pkgPath = PKG_PATH) {
  if (!existsSync(pkgPath)) {
    throw new Error(`package.json not found at ${pkgPath}`);
  }

  let pkg;
  try {
    const content = readFileSync(pkgPath, 'utf-8');
    pkg = JSON.parse(content);
  } catch (err) {
    throw new Error(
      `Failed to read or parse ${pkgPath}: ${err.message}`,
    );
  }

  pkg.version = newVersion;

  try {
    writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n', 'utf-8');
  } catch (err) {
    throw new Error(
      `Failed to write version to ${pkgPath}: ${err.message}`,
    );
  }
}

// ── pyproject.toml version support ─────────────────────────────────────────
//
// Python projects keep their version in `pyproject.toml` as
// `[project] version = "x.y.z"`. Node has no built-in TOML module, and a full
// parse/serialise round-trip would rewrite the file (dropping comments,
// reordering keys), so the edit is a line-based, section-aware rewrite of the
// single `version = "…"` line inside the `[project]` table. Every other byte —
// comments, key order, quoting of other values, line endings — is preserved.

/** A TOML table (or array-of-tables) header, capturing its dotted name. */
const TOML_TABLE_HEADER = /^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$/;

/** A `version = "…"` / `version = '…'` assignment with optional trailing comment. */
const PROJECT_VERSION_LINE = /^(\s*version\s*=\s*)(["'])([^"']*)\2(\s*(?:#.*)?)$/;

/**
 * Locate the `version` assignment inside the `[project]` table.
 *
 * @param {string} content - Full pyproject.toml text.
 * @returns {{index: number, version: string, prefix: string, suffix: string}|null}
 *   The matching line index plus the exact text before/after the value
 *   (quotes included), or null when no `[project].version` is declared.
 */
export function findProjectVersionLine(content) {
  const lines = String(content).split('\n');
  let table = null;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const header = line.match(TOML_TABLE_HEADER);
    if (header) {
      table = header[1];
      continue;
    }
    if (table !== 'project') continue;

    const match = line.match(PROJECT_VERSION_LINE);
    if (match) {
      return {
        index,
        version: match[3],
        prefix: match[1] + match[2],
        suffix: match[2] + match[4],
      };
    }
  }

  return null;
}

/**
 * Read the version declared in a pyproject.toml document.
 *
 * @param {string} content - Full pyproject.toml text.
 * @returns {string} The `[project].version` value.
 * @throws {Error} If no supported version declaration is present.
 */
export function readPyprojectVersion(content) {
  const found = findProjectVersionLine(content);

  if (!found) {
    throw new Error(
      'No version found in pyproject.toml. Expected a `version = "x.y.z"` assignment in the [project] table. '
      + 'Legacy [tool.poetry].version and `dynamic = ["version"]` (version supplied by the build backend) '
      + 'are not supported by the release bump — add [project].version or bump the version manually.',
    );
  }

  if (found.version.trim().toLowerCase() === 'dynamic') {
    throw new Error(
      'Unsupported version "dynamic" in the [project] table of pyproject.toml: the version is supplied by the '
      + 'build backend (`dynamic = ["version"]`). Add a literal [project].version or bump the version manually.',
    );
  }

  return found.version;
}

/**
 * Return *content* with only the `[project].version` value replaced.
 *
 * Pure: the caller decides whether to write the result, so `--dry-run` cannot
 * modify the file and a failed bump never leaves a partial write behind.
 *
 * @param {string} content - Full pyproject.toml text.
 * @param {string} newVersion - Replacement version value.
 * @returns {string} The rewritten document (byte-identical apart from the value).
 * @throws {Error} If no `[project].version` assignment is present.
 */
export function replaceProjectVersion(content, newVersion) {
  if (!newVersion || typeof newVersion !== 'string') {
    throw new Error('Invalid new version: must be a non-empty string');
  }

  const found = findProjectVersionLine(content);
  if (!found) {
    throw new Error('No `version = "x.y.z"` assignment found in the [project] table of pyproject.toml.');
  }

  const lines = String(content).split('\n');
  lines[found.index] = `${found.prefix}${newVersion}${found.suffix}`;
  return lines.join('\n');
}

/**
 * Write a new version into pyproject.toml's `[project].version`.
 *
 * @param {string} newVersion - The new version string to write.
 * @param {string} [pyprojectPath=PYPROJECT_PATH] - Path to pyproject.toml.
 * @throws {Error} If the file is missing, unreadable, or declares no version.
 */
export function writePyprojectVersionFile(newVersion, pyprojectPath = PYPROJECT_PATH) {
  if (!existsSync(pyprojectPath)) {
    throw new Error(`pyproject.toml not found at ${pyprojectPath}`);
  }

  let content;
  try {
    content = readFileSync(pyprojectPath, 'utf-8');
  } catch (err) {
    throw new Error(`Failed to read ${pyprojectPath}: ${err.message}`);
  }

  // Validate/normalise the document before touching the file so a failed bump
  // can never leave a partially written pyproject.toml behind.
  const updated = replaceProjectVersion(content, newVersion);

  try {
    writeFileSync(pyprojectPath, updated, 'utf-8');
  } catch (err) {
    throw new Error(`Failed to write version to ${pyprojectPath}: ${err.message}`);
  }
}

// ── Version source resolution ──────────────────────────────────────────────

/**
 * Resolve the repository's version source by convention.
 *
 * `package.json` (repository root) when it declares a version, else
 * `pyproject.toml`'s `[project].version`. The combined, actionable error
 * names every candidate and why it was rejected when neither declares a
 * version.
 *
 * @param {string} [repoRoot=REPO_ROOT] - Repository root to inspect.
 * @returns {{kind: 'package.json'|'pyproject.toml', path: string, version: string}}
 * @throws {Error} If no candidate file declares a version.
 */
export function resolveVersionSource(repoRoot = REPO_ROOT) {
  const pkgPath = resolve(repoRoot, 'package.json');
  const pyprojectPath = resolve(repoRoot, 'pyproject.toml');
  const reasons = [];

  if (existsSync(pkgPath)) {
    try {
      return { kind: 'package.json', path: pkgPath, version: readVersion(pkgPath) };
    } catch (err) {
      reasons.push(`package.json: ${err.message}`);
    }
  } else {
    reasons.push('package.json: not found');
  }

  if (existsSync(pyprojectPath)) {
    try {
      return {
        kind: 'pyproject.toml',
        path: pyprojectPath,
        version: readPyprojectVersion(readFileSync(pyprojectPath, 'utf-8')),
      };
    } catch (err) {
      reasons.push(`pyproject.toml: ${err.message}`);
    }
  } else {
    reasons.push('pyproject.toml: not found');
  }

  throw new Error(
    `No version source found in ${repoRoot}. Expected a "version" field in package.json `
    + `or a [project].version in pyproject.toml.\n  - ${reasons.join('\n  - ')}`,
  );
}

/**
 * Write *newVersion* back to the resolved version source.
 *
 * @param {{kind: string, path: string}} source - Result of resolveVersionSource().
 * @param {string} newVersion - The new version string to write.
 */
export function writeResolvedVersion(source, newVersion) {
  if (source.kind === 'pyproject.toml') {
    writePyprojectVersionFile(newVersion, source.path);
    return;
  }
  writeVersion(newVersion, source.path);
}

// ── CLI Entry Point ────────────────────────────────────────────────────────

function printUsage() {
  console.error(`
Usage: node bump-version.js [options]

Options:
  --bump <type>   Which part of the version to increment (default: patch)
                  One of: patch, minor, major
  --dry-run       Print the new version without modifying the version file
  -h, --help      Show this help message

Version source (by convention):
  package.json when present, else pyproject.toml's [project].version.

Examples:
  node bump-version.js                  # Bump patch: 0.1.0 → 0.1.1
  node bump-version.js --bump minor     # Bump minor: 0.1.0 → 0.2.0
  node bump-version.js --bump major     # Bump major: 0.1.0 → 1.0.0
  node bump-version.js --dry-run        # Print new version without writing
`);
}

function main() {
  const args = process.argv.slice(2);
  let bumpType = 'patch';
  let dryRun = false;

  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--bump':
        i++;
        if (i >= args.length) {
          console.error('Error: --bump requires an argument (patch|minor|major)');
          printUsage();
          process.exit(1);
        }
        bumpType = args[i];
        break;
      case '--dry-run':
        dryRun = true;
        break;
      case '-h':
      case '--help':
        printUsage();
        process.exit(0);
      default:
        console.error(`Unknown option: ${args[i]}`);
        printUsage();
        process.exit(1);
    }
  }

  // Validate bump type early
  if (!['patch', 'minor', 'major'].includes(bumpType)) {
    console.error(`Error: Invalid bump type "${bumpType}". Must be one of: patch, minor, major.`);
    process.exit(1);
  }

  try {
    const source = resolveVersionSource(REPO_ROOT);
    const currentVersion = source.version;
    const newVersion = bumpVersion(currentVersion, bumpType);

    if (dryRun) {
      console.log(`Current version: ${currentVersion}`);
      console.log(`New version (${bumpType} bump): ${newVersion}`);
      console.log(`Dry-run: ${basename(source.path)} was NOT modified.`);
    } else {
      writeResolvedVersion(source, newVersion);
      console.log(newVersion);
    }
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
}

// Allow both import (ESM) and direct CLI execution
// Use realpathSync on both sides to handle symlinked install paths:
// import.meta.url resolves to the real path while process.argv[1]
// may retain the symlink path.
const isMainModule = process.argv[1] &&
  (realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1])));

if (isMainModule) {
  main();
}
