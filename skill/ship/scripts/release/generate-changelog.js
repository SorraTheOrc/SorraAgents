#!/usr/bin/env node

/**
 * generate-changelog.js — CHANGELOG.md generator for the ship release process.
 *
 * Queries Worklog for all completed / in_review work items, categorizes them
 * by issue_type, applies a simple keyword-based miscategorization check,
 * and updates / creates CHANGELOG.md in the repository root.
 *
 * Usage:
 *   node generate-changelog.js <version>
 *
 * Options:
 *   <version>   Semantic version string (e.g. "0.2.0") for the new release
 *
 * Example:
 *   node generate-changelog.js 0.2.0
 *
 * Exit codes:
 *   0  Success (new section prepended to CHANGELOG.md)
 *   1  Error
 */

import { readFileSync, writeFileSync, existsSync, realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// ── Resolve repo root ──────────────────────────────────────────────────────
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
const CHANGELOG_PATH = resolve(REPO_ROOT, 'CHANGELOG.md');

// ── Worklog helpers ────────────────────────────────────────────────────────

/**
 * Fetch all work items that should appear in the changelog:
 * those with status=completed OR stage=in_review.
 *
 * @returns {Array<{id:string, title:string, issueType:string, description:string}>}
 */
function getCompletedOrInReviewItems() {
  const seen = new Set();
  const items = [];

  // Items with status=completed
  try {
    const completedOut = execSync('wl list --status completed --json', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const completed = JSON.parse(completedOut);
    for (const item of (completed.workItems || [])) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        items.push(item);
      }
    }
  } catch {
    // wl may not be available; caller handles this gracefully
    console.error('Warning: could not query completed work items (wl not available?)');
  }

  // Items with stage=in_review (regardless of status)
  try {
    const inReviewOut = execSync('wl list --stage in_review --json', {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const inReview = JSON.parse(inReviewOut);
    for (const item of (inReview.workItems || [])) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        items.push(item);
      }
    }
  } catch {
    console.error('Warning: could not query in_review work items (wl not available?)');
  }

  return items;
}

// ── Miscategorization keywords ─────────────────────────────────────────────

const FEATURE_KEYWORDS = [
  'add', 'new', 'feature', 'implement', 'create', 'support',
  'introduce', 'enable', 'allow', 'ability', 'can now',
];

const BUG_KEYWORDS = [
  'fix', 'bug', 'error', 'crash', 'incorrect', 'wrong',
  'issue', 'broken', 'failing', 'fail', 'regression',
];

/**
 * Simple keyword-based miscategorization check for a work item.
 *
 * If the item is typed "bug" but its title/description strongly suggest
 * a feature, or typed "feature" but strongly suggests a bug fix, the
 * item's issue_type is updated and the corrected type is returned.
 *
 * @param {{id:string, title:string, issueType:string, description:string}} item
 * @returns {string} The (possibly corrected) issue type.
 */
function checkMiscategorization(item) {
  const title = (item.title || '').toLowerCase();
  const desc = (item.description || '').toLowerCase();
  const combined = `${title} ${desc}`;

  const isBug = item.issueType === 'bug';
  const isFeature = item.issueType === 'feature';

  let suggestedType = null;

  if (isBug) {
    const featureHits = FEATURE_KEYWORDS.filter(kw => combined.includes(kw)).length;
    const bugHits = BUG_KEYWORDS.filter(kw => combined.includes(kw)).length;

    if (featureHits > bugHits && featureHits >= 2) {
      suggestedType = 'feature';
      console.error(
        `[miscategorization] ${item.id}: reclassifying bug→feature ` +
        `(title matched ${featureHits}x feature keywords vs ${bugHits}x bug keywords)`,
      );
    }
  } else if (isFeature) {
    const bugHits = BUG_KEYWORDS.filter(kw => combined.includes(kw)).length;
    const featureHits = FEATURE_KEYWORDS.filter(kw => combined.includes(kw)).length;

    if (bugHits > featureHits && bugHits >= 2) {
      suggestedType = 'bug';
      console.error(
        `[miscategorization] ${item.id}: reclassifying feature→bug ` +
        `(title matched ${bugHits}x bug keywords vs ${featureHits}x feature keywords)`,
      );
    }
  }

  if (suggestedType) {
    try {
      execSync(`wl update ${item.id} --issue-type ${suggestedType} 2>/dev/null`, {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch {
      console.error(`[miscategorization] ${item.id}: failed to update issue_type`);
    }
    return suggestedType;
  }

  return item.issueType;
}

// ── Categorization ─────────────────────────────────────────────────────────

/**
 * Group items into Features, Bug Fixes, and Other categories.
 *
 * @param {Array} items
 * @returns {{features:string[], bugFixes:string[], other:string[]}}
 */
function categorizeItems(items) {
  const features = [];
  const bugFixes = [];
  const other = [];

  for (const item of items) {
    const effectiveType = checkMiscategorization(item);
    const entry = `- ${item.title} (${item.id})`;

    switch (effectiveType) {
      case 'feature':
        features.push(entry);
        break;
      case 'bug':
        bugFixes.push(entry);
        break;
      default:
        other.push(entry);
        break;
    }
  }

  return { features, bugFixes, other };
}

// ── Markdown generation ────────────────────────────────────────────────────

/**
 * Generate the Markdown section for a single release.
 *
 * @param {string} version  e.g. "0.2.0"
 * @param {string} date     e.g. "2026-07-08"
 * @param {{features:string[], bugFixes:string[], other:string[]}} categorized
 * @returns {string}
 */
function generateReleaseSection(version, date, categorized) {
  const lines = [];
  const push = (s) => { if (s !== '') lines.push(s); };

  push(`## v${version} (${date})`);
  push('');

  if (categorized.features.length > 0) {
    push('### Features');
    push('');
    categorized.features.forEach(e => push(e));
    push('');
  }

  if (categorized.bugFixes.length > 0) {
    push('### Bug Fixes');
    push('');
    categorized.bugFixes.forEach(e => push(e));
    push('');
  }

  if (categorized.other.length > 0) {
    push('### Other');
    push('');
    categorized.other.forEach(e => push(e));
    push('');
  }

  return lines.join('\n');
}

/**
 * Get today's date in ISO format (YYYY-MM-DD).
 *
 * @returns {string}
 */
function getTodaysDate() {
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, '0');
  const dd = String(now.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/**
 * Prepend a new release section to CHANGELOG.md.
 *
 * If the file does not exist it is created with a top-level heading.
 * If it exists, the new section is inserted after the first heading block.
 *
 * @param {string} newSection  Markdown section to prepend
 */
function updateChangelog(newSection) {
  let existingContent = '';

  if (existsSync(CHANGELOG_PATH)) {
    existingContent = readFileSync(CHANGELOG_PATH, 'utf-8');
  }

  // Ensure file starts with a top-level heading
  if (!existingContent.trim()) {
    existingContent = '# Changelog\n\n';
  } else if (!/^#\s/.test(existingContent)) {
    existingContent = '# Changelog\n\n' + existingContent;
  }

  // Prepend the new section after the first heading + blank line
  const headingEnd = existingContent.indexOf('\n\n');
  if (headingEnd >= 0) {
    const header = existingContent.substring(0, headingEnd + 2); // include \n\n
    const rest = existingContent.substring(headingEnd + 2);
    existingContent = header + newSection + '\n\n' + rest;
  } else {
    existingContent = existingContent.trimEnd() + '\n\n' + newSection + '\n\n';
  }

  writeFileSync(CHANGELOG_PATH, existingContent, 'utf-8');
}

// ── Main ───────────────────────────────────────────────────────────────────

function printUsage() {
  console.error(`
Usage: node generate-changelog.js <version>

Arguments:
  <version>   Semantic version string (e.g. "0.2.0")

Examples:
  node generate-changelog.js 0.2.0
  node generate-changelog.js 1.0.0
`);
}

function main() {
  const args = process.argv.slice(2);

  if (args.length < 1 || args[0] === '-h' || args[0] === '--help') {
    printUsage();
    process.exit(args.length < 1 ? 1 : 0);
  }

  const version = args[0];
  const date = getTodaysDate();

  console.error(`Generating CHANGELOG.md for v${version} (${date}) ...`);

  const items = getCompletedOrInReviewItems();
  console.error(`Found ${items.length} work item(s) (completed + in_review)`);

  const categorized = categorizeItems(items);
  console.error(
    `Categorised: ${categorized.features.length} feature(s), ` +
    `${categorized.bugFixes.length} bug fix(es), ` +
    `${categorized.other.length} other`,
  );

  const newSection = generateReleaseSection(version, date, categorized);
  updateChangelog(newSection);

  console.error(`CHANGELOG.md updated at ${CHANGELOG_PATH}`);
}

// Allow both ESM import and direct CLI execution
const isMainModule = process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1]));

if (isMainModule) {
  main();
}
