#!/usr/bin/env node

/**
 * bump-preflight.js — Read-only release-candidate gatherer for the ship
 * skill's version-bump evaluation step (SA-0MWFVQWGN006G4KA).
 *
 * Runs *before* any git mutation. It gathers every release candidate
 * (`stage: in_review`), enriches each item with the evidence needed to judge
 * the release's importance (`## Impact` / user story, type, priority and a
 * short description excerpt), and renders a compact review packet whose
 * patch/minor/major outcomes come from the existing `bump-version.js`
 * arithmetic.
 *
 * It performs **no judgement and no mutation**: the agent judges from the
 * packet, the producer approves a non-patch bump, and the default proposal is
 * always `patch`. An empty or unreadable candidate set fails safe to `patch`
 * with an explanatory warning — it never throws and never blocks a release.
 *
 * Reused boundaries (never duplicated):
 *   - candidates  → `getCandidateItems()` from `../check-audit-gate.js`
 *   - version     → `resolveVersionSource()` from `./bump-version.js`
 *   - arithmetic  → `bumpVersion()` from `./bump-version.js`
 *
 * All three are injectable so tests never read the live worklog or real git
 * state.
 *
 * Usage:
 *   node bump-preflight.js [--json]
 *
 * Options:
 *   --json   Print the structured result as JSON instead of the packet text
 *   -h, --help
 *
 * Exit codes:
 *   0  Always — the preflight is evidence gathering, never a release gate.
 */

import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { existsSync, realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { getCandidateItems } from '../check-audit-gate.js';
import { bumpVersion, resolveVersionSource } from './bump-version.js';

// ── Constants ──────────────────────────────────────────────────────────────

/** The three bump outcomes the packet always reports, in increasing order. */
export const BUMP_TYPES = Object.freeze(['patch', 'minor', 'major']);

/** Default maximum length of the per-item description excerpt. */
export const DEFAULT_EXCERPT_LENGTH = 200;

/** The fail-safe proposal when the evidence cannot be read. */
export const DEFAULT_PROPOSED_BUMP = 'patch';

/** Name of the description section that outranks the user story. */
const IMPACT_HEADING = 'Impact';

/** Name of the fallback description section. */
const USER_STORY_HEADING = 'User Story';

// ── Text helpers ───────────────────────────────────────────────────────────

/**
 * Flatten Markdown text to a single whitespace-collapsed line.
 *
 * @param {unknown} text
 * @returns {string} The flattened text ('' for non-strings).
 */
function flatten(text) {
  if (typeof text !== 'string') return '';
  return text.replace(/\s+/g, ' ').trim();
}

/**
 * Extract the body of a `## <heading>` section from a Markdown document.
 *
 * The section ends at the next level-2 heading (or end of document). `###`
 * and deeper headings are part of the body. The comparison is
 * case-insensitive and tolerant of surrounding whitespace.
 *
 * @param {unknown} markdown - Full description text.
 * @param {string} heading - Section heading to extract (e.g. "Impact").
 * @returns {string} The raw (unflattened) section body, or '' when absent.
 */
export function extractSection(markdown, heading) {
  if (typeof markdown !== 'string' || !markdown) return '';
  const needle = String(heading).trim().toLowerCase();
  const body = [];
  let collecting = false;

  for (const line of markdown.split('\n')) {
    const match = line.match(/^##\s+(\S.*?)\s*$/);
    if (match) {
      if (collecting) break;
      if (match[1].trim().toLowerCase() === needle) {
        collecting = true;
        continue;
      }
    }
    if (collecting) body.push(line);
  }

  return body.join('\n').trim();
}

/**
 * Truncate flattened text to *limit* characters, appending an ellipsis.
 *
 * @param {string} text - Already-flattened text.
 * @param {number} limit - Maximum returned length (ellipsis included).
 * @returns {string} The (possibly truncated) excerpt.
 */
function truncate(text, limit) {
  if (!text || !Number.isFinite(limit) || limit <= 0) return '';
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
}

// ── Candidate enrichment ───────────────────────────────────────────────────

/**
 * Enrich one release candidate with the fields needed to judge importance.
 *
 * Tolerant by design: a missing title falls back to the id, a missing id to
 * the title, a missing `## Impact` to the `## User Story` section, and both
 * to the title/id. Missing type/priority become `unknown` and a missing
 * description becomes an empty excerpt — never a throw.
 *
 * @param {object} item - A candidate from the fetch boundary (any richness).
 * @param {{excerptLength?: number}} [options]
 * @returns {{id: string, title: string, issueType: string, priority: string,
 *   impact: string, descriptionExcerpt: string}}
 */
export function enrichCandidate(item, { excerptLength = DEFAULT_EXCERPT_LENGTH } = {}) {
  const raw = item && typeof item === 'object' ? item : { id: item };
  const id = raw.id === undefined || raw.id === null ? '' : String(raw.id);
  const title = typeof raw.title === 'string' && raw.title.trim() ? raw.title.trim() : id;
  const description = typeof raw.description === 'string' ? raw.description : '';

  const issueType = typeof raw.issueType === 'string' && raw.issueType.trim()
    ? raw.issueType.trim()
    : 'unknown';
  const priority = typeof raw.priority === 'string' && raw.priority.trim()
    ? raw.priority.trim()
    : 'unknown';

  const impact = flatten(raw.impact)
    || flatten(extractSection(description, IMPACT_HEADING))
    || flatten(raw.userStory)
    || flatten(extractSection(description, USER_STORY_HEADING))
    || title
    || id;

  return {
    id,
    title,
    issueType,
    priority,
    impact,
    descriptionExcerpt: truncate(flatten(description), excerptLength),
  };
}

/**
 * Default detail boundary: read one work item from the worklog (read-only).
 *
 * `getCandidateItems()` deliberately projects a thin shape (id/title/audit
 * fields) to keep its bulk query small, so the preflight fetches the
 * judgement-bearing fields for those items separately. Failures are
 * non-fatal: the caller falls back to the thin projection.
 *
 * @param {string} id - Work item id.
 * @returns {object|null} The work item, or null when unreadable.
 */
export function fetchCandidateDetails(id) {
  if (!id) return null;
  try {
    const output = execFileSync('wl', ['show', String(id), '--json'], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      maxBuffer: 8 * 1024 * 1024,
    });
    const parsed = JSON.parse(output);
    if (parsed && typeof parsed === 'object' && parsed.workItem && typeof parsed.workItem === 'object') {
      return parsed.workItem;
    }
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Does a thin list projection need a detail round-trip for judgement fields?
 *
 * @param {object} item
 * @returns {boolean}
 */
function needsDetails(item) {
  return !item.description && item.impact === undefined && item.userStory === undefined;
}

// ── Candidate gathering ────────────────────────────────────────────────────

/**
 * Gather and enrich every release candidate through an injectable boundary.
 *
 * @param {object} [options]
 * @param {() => Array<object>} [options.getCandidateItemsFn=getCandidateItems]
 *   Candidate boundary; the default queries Worklog for `stage: in_review`.
 * @param {(id: string) => (object|null)} [options.getItemDetailsFn=fetchCandidateDetails]
 *   Detail boundary used only when the projection lacks judgement fields.
 * @param {number} [options.excerptLength=DEFAULT_EXCERPT_LENGTH]
 * @returns {{candidates: Array<object>, warning: string|null}}
 */
export function gatherReleaseCandidates({
  getCandidateItemsFn = getCandidateItems,
  getItemDetailsFn = fetchCandidateDetails,
  excerptLength = DEFAULT_EXCERPT_LENGTH,
} = {}) {
  let rawItems;
  try {
    rawItems = getCandidateItemsFn();
  } catch (err) {
    return {
      candidates: [],
      warning: `Could not read release candidates (${err && err.message ? err.message : String(err)}). `
        + 'Defaulting to a patch bump.',
    };
  }

  if (!Array.isArray(rawItems)) {
    return {
      candidates: [],
      warning: 'Could not read release candidates: the candidate boundary did not return a list. '
        + 'Defaulting to a patch bump.',
    };
  }

  const candidates = rawItems.map((item) => {
    const base = item && typeof item === 'object' ? item : { id: item };
    let enrichedInput = base;

    if (needsDetails(base) && typeof getItemDetailsFn === 'function') {
      try {
        const details = getItemDetailsFn(base.id);
        if (details && typeof details === 'object') {
          // The list projection stays authoritative for id/title; the detail
          // fetch supplies the judgement-bearing fields.
          enrichedInput = { ...details, ...base, title: base.title || details.title };
        }
      } catch {
        // Fall back to the thin projection — gathering must never throw.
      }
    }

    return enrichCandidate(enrichedInput, { excerptLength });
  });

  const warning = candidates.length === 0
    ? 'No release candidates found (no work items at stage `in_review`). Defaulting to a patch bump.'
    : null;

  return { candidates, warning };
}

// ── Packet rendering ───────────────────────────────────────────────────────

/**
 * Render the agent-readable review packet.
 *
 * The layout is stable and asserted by tests (and consumed by the ship
 * workflow): current version, the three candidate outcomes, then one block
 * per candidate with its id, title, type, priority, impact and excerpt.
 *
 * @param {{candidates?: Array<object>, currentVersion?: string|null,
 *   proposals?: Record<string, string|null>, proposedBump?: string,
 *   warning?: string|null}} result
 * @returns {string} Markdown packet.
 */
export function renderReviewPacket(result = {}) {
  const {
    candidates = [],
    currentVersion = null,
    proposals = {},
    proposedBump = DEFAULT_PROPOSED_BUMP,
    warning = null,
  } = result || {};

  const lines = [
    '# Version bump review packet',
    '',
    `Current version: ${currentVersion ?? 'unknown'}`,
    `Proposed bump: ${proposedBump} (default — the agent judges from the evidence below)`,
    '',
    '## Candidate outcomes',
    '',
    '| Bump | Version |',
    '| --- | --- |',
  ];

  for (const type of BUMP_TYPES) {
    lines.push(`| ${type} | ${proposals[type] ?? 'unknown'} |`);
  }

  lines.push('', `## Release candidates (${candidates.length})`, '');

  if (candidates.length === 0) {
    lines.push('No release candidates found (no work items at stage `in_review`).');
  } else {
    candidates.forEach((candidate, index) => {
      lines.push(`### ${index + 1}. ${candidate.title} (${candidate.id})`);
      lines.push('');
      lines.push(`- Type: ${candidate.issueType}`);
      lines.push(`- Priority: ${candidate.priority}`);
      lines.push(`- Impact: ${candidate.impact}`);
      lines.push(`- Description: ${candidate.descriptionExcerpt || '(none)'}`);
      lines.push('');
    });
  }

  if (warning) {
    lines.push('## Warning', '', warning, '');
  }

  return `${lines.join('\n').trimEnd()}\n`;
}

// ── Preflight entry point ──────────────────────────────────────────────────

/**
 * Run the full read-only bump preflight.
 *
 * @param {object} [options]
 * @param {() => Array<object>} [options.getCandidateItemsFn=getCandidateItems]
 * @param {(id: string) => (object|null)} [options.getItemDetailsFn=fetchCandidateDetails]
 * @param {(repoRoot?: string) => ({version?: string}|string)} [options.resolveVersionSourceFn=resolveVersionSource]
 * @param {string} [options.repoRoot] - Repository root for the version source.
 * @param {number} [options.excerptLength=DEFAULT_EXCERPT_LENGTH]
 * @param {(message: string) => void} [options.logWarning=console.warn]
 * @returns {{
 *   candidates: Array<object>,
 *   currentVersion: string|null,
 *   proposals: {patch: string|null, minor: string|null, major: string|null},
 *   proposedBump: string,
 *   warning: string|null,
 *   packet: string,
 * }}
 */
export function runBumpPreflight({
  getCandidateItemsFn = getCandidateItems,
  getItemDetailsFn = fetchCandidateDetails,
  resolveVersionSourceFn = resolveVersionSource,
  repoRoot,
  excerptLength = DEFAULT_EXCERPT_LENGTH,
  logWarning = console.warn,
} = {}) {
  const warnings = [];

  const { candidates, warning: gatherWarning } = gatherReleaseCandidates({
    getCandidateItemsFn,
    getItemDetailsFn,
    excerptLength,
  });
  if (gatherWarning) warnings.push(gatherWarning);

  let currentVersion = null;
  let proposals = { patch: null, minor: null, major: null };

  try {
    const source = resolveVersionSourceFn(repoRoot);
    currentVersion = typeof source === 'string'
      ? source
      : (source && typeof source.version === 'string' ? source.version : null);

    if (!currentVersion) {
      throw new Error('the version source did not report a version');
    }

    const computed = {};
    for (const type of BUMP_TYPES) {
      computed[type] = bumpVersion(currentVersion, type);
    }
    proposals = computed;
  } catch (err) {
    currentVersion = null;
    proposals = { patch: null, minor: null, major: null };
    warnings.push(
      `Could not compute version proposals (${err && err.message ? err.message : String(err)}). `
        + 'An explicit --bump can still be supplied to the release.',
    );
  }

  const warning = warnings.length ? warnings.join(' ') : null;
  if (warning && typeof logWarning === 'function') logWarning(warning);

  const result = {
    candidates,
    currentVersion,
    proposals,
    proposedBump: DEFAULT_PROPOSED_BUMP,
    warning,
  };

  return { ...result, packet: renderReviewPacket(result) };
}

// ── CLI Entry Point ────────────────────────────────────────────────────────

function printUsage() {
  console.error(`
Usage: node bump-preflight.js [options]

Read-only version-bump preflight for the ship release workflow. Gathers every
release candidate (stage: in_review) and prints the review packet the agent
judges. Performs no git/tag/PR/worklog mutation.

Options:
  --json        Print the structured result as JSON instead of the packet
  -h, --help    Show this help message

Exit codes:
  0  Always — evidence gathering never blocks a release.

Examples:
  node bump-preflight.js
  node bump-preflight.js --json
`);
}

function main() {
  const args = process.argv.slice(2);
  let asJson = false;

  for (const arg of args) {
    switch (arg) {
      case '--json':
        asJson = true;
        break;
      case '-h':
      case '--help':
        printUsage();
        process.exit(0);
        break;
      default:
        console.error(`Unknown option: ${arg}`);
        printUsage();
        process.exit(0);
    }
  }

  const result = runBumpPreflight();
  if (asJson) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(result.packet);
  }
}

const modulePath = fileURLToPath(import.meta.url);
const isMainModule = process.argv[1]
  && existsSync(process.argv[1])
  && realpathSync(modulePath) === realpathSync(resolve(process.argv[1]));

if (isMainModule) {
  main();
}
