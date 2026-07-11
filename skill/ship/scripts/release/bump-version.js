#!/usr/bin/env node

/**
 * bump-version.js — Version increment helper for the ship release process.
 *
 * Reads the current version from `package.json` in the repository root,
 * increments it according to the specified bump type, writes the new
 * version back, and prints the new version to stdout.
 *
 * Usage:
 *   node bump-version.js [--bump patch|minor|major]
 *
 * Options:
 *   --bump <type>   Which part of the version to increment (default: patch)
 *   --dry-run       Print the new version without modifying package.json
 *   -h, --help      Show this help message
 *
 * Exit codes:
 *   0  Success (new version printed to stdout)
 *   1  Error (invalid arguments, missing file, parse error)
 */

import { readFileSync, writeFileSync, existsSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// ── Resolve repo root ──────────────────────────────────────────────────────
// Use `git rev-parse --show-toplevel` so the script works regardless of
// where it is installed — locally inside a repo tree or globally via a
// skill manager at ~/.pi/agent/skills/ship/…
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
const PKG_PATH = resolve(REPO_ROOT, 'package.json');

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

// ── CLI Entry Point ────────────────────────────────────────────────────────

function printUsage() {
  console.error(`
Usage: node bump-version.js [options]

Options:
  --bump <type>   Which part of the version to increment (default: patch)
                  One of: patch, minor, major
  --dry-run       Print the new version without modifying package.json
  -h, --help      Show this help message

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
    const currentVersion = readVersion(PKG_PATH);
    const newVersion = bumpVersion(currentVersion, bumpType);

    if (dryRun) {
      console.log(`Current version: ${currentVersion}`);
      console.log(`New version (${bumpType} bump): ${newVersion}`);
      console.log('Dry-run: package.json was NOT modified.');
    } else {
      writeVersion(newVersion, PKG_PATH);
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
