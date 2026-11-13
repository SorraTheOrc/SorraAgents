#!/usr/bin/env node

/**
 * generate-changelog.js — CHANGELOG.md generator for the ship release process.
 *
 * Queries Worklog for all completed / in_review work items, filters to
 * parent-level items, categorizes them by issue_type with an LLM review
 * pass, generates player-focused descriptions via an LLM prompt, and
 * updates / creates CHANGELOG.md in the repository root.
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

// Shared LLM caller, extracted so discord-notify.js can reuse it without
// importing this module (which runs `git rev-parse` at import time)
// (SA-0MUVV6DK0002ISW4).
import { callLlm } from '../llm.js';
// CTA resolver — reuse the same three-layer precedence as discord-notify.js.
import { resolveCta } from '../discord-notify.js';

// ── Resolve repo root ──────────────────────────────────────────────────────
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
const CHANGELOG_PATH = resolve(REPO_ROOT, 'CHANGELOG.md');

// ── Worklog helpers ────────────────────────────────────────────────────────

/**
 * Explicit `maxBuffer` for the bulk `wl list` projection (64 MB).
 *
 * The projection is skinny (see `getCompletedOrInReviewItems`), but the
 * buffer must still comfortably hold the projected JSON for a very large
 * in_review backlog. Left at Node's default (1 MB) an oversized projection
 * trips `ENOBUFS`; the historical bug then swallowed the error and emitted an
 * empty changelog section (SA-0MWE4BT63005N8OE AC1).
 */
export const WL_QUERY_MAX_BUFFER = 64 * 1024 * 1024;

/**
 * Explicit `maxBuffer` for a single-item `wl show` description fetch (1 MB).
 * A single work item description is never legitimately larger than this, and
 * keeping it small bounds the lazy-fetch path independently of the bulk query
 * (SA-0MWE4BT63005N8OE AC2 constraint).
 */
export const DESCRIPTION_FETCH_MAX_BUFFER = 1024 * 1024;

/**
 * Render an execSync error as `CODE: message` for an actionable warning.
 *
 * @param {unknown} err
 * @returns {string}
 */
function describeExecError(err) {
  if (!err) return String(err);
  const code = err.code ? `${err.code}: ` : '';
  return `${code}${err.message || String(err)}`;
}

/**
 * Fetch all work items that should appear in the changelog:
 * those with stage=in_review (status=completed — the release candidate
 * set per the stage/status model).
 *
 * A single `--stage in_review` query replaces the previous union
 * (SA-0MSPPHTYA002212R): the old `--status completed` arm contributed only
 * stage=done items, which are already released and already appear in prior
 * changelog sections (a correctness bug — previously released items were
 * re-listed). The output is piped through `jq` so only the needed field
 * projection enters execSync's buffer — the full `wl list --json` output
 * for a large worklog can exceed the default 1 MB buffer (ENOBUFS), while
 * the OS pipe between `wl` and `jq` is unbounded. The command is
 * invoked via `bash -c` (not plain /bin/sh) because `set -o pipefail`
 * is a bash-ism not supported by dash — see LP-0MSQ0NTMO00577UJ.
 * `set -o pipefail` ensures a `wl` failure still surfaces as an execSync
 * error so the warning path below fires.
 *
 * The projection is deliberately **skinny**: `description` is omitted and
 * fetched lazily (via `enrichWithDescriptions`) for the small subset of
 * parent items that survive `getParentsOnly`. This bounds the projected
 * output regardless of backlog size, so a huge in_review set can never
 * overflow the buffer (SA-0MWE4BT63005N8OE AC2). `maxBuffer` is still set
 * explicitly as a safety net (AC1).
 *
 * @returns {Array<{id:string, title:string, issueType:string, parentId?:string|null}>}
 */
export function getCompletedOrInReviewItems() {
  try {
    // Single query (stage=in_review implies status=completed), piped through
    // jq so only {id, title, issueType, parentId} enters the buffer. parentId
    // is needed to filter parent-only work items for the changelog.
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c \"[.workItems[] | {id, title, issueType, parentId}]\"'`,
      { encoding: 'utf-8', maxBuffer: WL_QUERY_MAX_BUFFER, stdio: ['pipe', 'pipe', 'pipe'] },
    );
    return JSON.parse(output) || [];
  } catch (err) {
    // Surface the real failure (e.g. ENOBUFS, wl missing, jq error) instead
    // of a misleading "wl not available?" warning (SA-0MWE4BT63005N8OE AC3).
    console.error(
      `Warning: could not query in_review work items: ${describeExecError(err)}`,
    );
    return [];
  }
}

// ── Parent-only filtering ──────────────────────────────────────────────────

/**
 * Filter work items to only parent items (those with no parentId).
 * This excludes child/subtask items that would create redundant changelog entries.
 *
 * @param {Array<{id:string, title:string, issueType:string, parentId?:string|null}>} items
 * @returns {Array<{id:string, title:string, issueType:string, parentId?:string|null}>}
 */
export function getParentsOnly(items) {
  return items.filter(
    (item) => item.parentId === null || item.parentId === undefined,
  );
}

/**
 * Fetch a single work item's full description by id.
 *
 * Uses a single-item `wl show <id> --json` wrapped in `bash -c` (dash-safe
 * `set -o pipefail`) with its own explicit `maxBuffer`. Returns '' and logs a
 * warning when the id is invalid or the fetch fails, so a bad lookup degrades
 * to a title-only changelog entry instead of aborting the release
 * (SA-0MWE4BT63005N8OE AC2 constraint).
 *
 * @param {string} id
 * @returns {string}
 */
export function fetchItemDescription(id) {
  // Defence in depth: ids are shell-substituted, so reject anything that is
  // not a work-item-shaped token before it reaches the command string.
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(String(id))) {
    console.error(`Warning: could not fetch description for invalid work item id: ${String(id)}`);
    return '';
  }
  try {
    const output = execSync(
      `bash -c 'set -o pipefail; wl show ${id} --json'`,
      { encoding: 'utf-8', maxBuffer: DESCRIPTION_FETCH_MAX_BUFFER, stdio: ['pipe', 'pipe', 'pipe'] },
    );
    const data = JSON.parse(output);
    return (data && data.workItem && data.workItem.description) || '';
  } catch (err) {
    console.error(
      `Warning: could not fetch description for ${id}: ${describeExecError(err)}`,
    );
    return '';
  }
}

/**
 * Fill in missing `description` fields by fetching each item's description
 * lazily with a single bounded `wl show <id> --json` call.
 *
 * Defence in depth (SA-0MWE4BT63005N8OE AC2): the bulk query projects skinny
 * fields only, so descriptions are fetched here for just the (small) set of
 * parent items that reach the changelog. Each fetch has its own explicit
 * `maxBuffer`, so a huge description can never re-introduce `ENOBUFS` on the
 * bulk query; a single failed fetch degrades to a title-only entry.
 *
 * Items that already carry a non-empty description are returned unchanged, so
 * callers with pre-enriched items trigger no subprocesses.
 *
 * @param {Array<{id:string, title:string, issueType:string, description?:string, parentId?:string|null}>} items
 * @returns {Array<{id:string, title:string, issueType:string, description:string, parentId?:string|null}>}
 */
export function enrichWithDescriptions(items) {
  return items.map((item) => {
    if (typeof item.description === 'string' && item.description.length > 0) {
      return item;
    }
    return { ...item, description: fetchItemDescription(item.id) };
  });
}

// ── LLM-based player description generation ─────────────────────────────────

/**
 * Generate a player-focused description for a work item using an LLM.
 *
 * Uses the DeepSeek API (OpenAI-compatible) if the DEEPSEEK_API_KEY
 * environment variable is set; otherwise falls back to the raw title.
 *
 * @param {{id:string, title:string, issueType:string, description:string}} item
 * @returns {Promise<string>} A player-friendly description.
 */
export async function generatePlayerDescription(item) {
  const prompt = `
You are a changelog writer for a game. Convert the following work item into a short, player-friendly description.

Focus on WHY this change matters to players, not technical implementation details.
Keep it concise (under 100 characters). Do NOT include the work item ID, technical jargon, or markdown formatting.

Title: ${escapeForPrompt(item.title)}
Description: ${escapeForPrompt(item.description || 'No additional description.')}

Player-facing description:`;

  const content = await callLlm([
    { role: 'system', content: 'You are a helpful changelog writer. Always respond with a single line — the player-friendly description. No extra text, no quotes, no ID references.' },
    { role: 'user', content: prompt.trim() },
  ]);

  if (content === null) {
    // No LLM API available: fall back to the raw title (backward compatible)
    return item.title;
  }

  // Clean up the response: remove surrounding quotes, extra whitespace
  return content
    .trim()
    .replace(/^"|"$/g, '')
    .replace(/^'|'$/g, '');
}

/**
 * Escape special characters in a string for safe inclusion in a prompt.
 *
 * @param {string} str
 * @returns {string}
 */
function escapeForPrompt(str) {
  return str
    .replace(/\\/g, '\\\\')
    .replace(/\n/g, '\\n')
    .replace(/"/g, '\\"');
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

// ── Classification review ──────────────────────────────────────────────────

/**
 * Map a Worklog issue type to a changelog category.
 *
 * @type {Record<string, 'feature'|'bug'|'other'>}
 */
const ISSUE_TYPE_CATEGORY = {
  feature: 'feature',
  bug: 'bug',
  chore: 'other',
  docs: 'other',
  task: 'other',
  epic: 'other',
};

/**
 * Convert a Worklog issue type to a changelog category.
 *
 * @param {string} issueType
 * @returns {'feature'|'bug'|'other'}
 */
export function toCategory(issueType) {
  return ISSUE_TYPE_CATEGORY[issueType] || 'other';
}

/**
 * Review and validate the classification of a work item.
 *
 * Starts from the keyword-based classification (including the
 * miscategorization heuristic) and, when an LLM API key is configured,
 * asks the LLM to confirm or correct it. If the LLM disagrees, its
 * classification wins and the entry is flagged for the operator's
 * attention in stderr.
 *
 * @param {{id:string, title:string, issueType:string, description:string}} item
 * @returns {Promise<{type:'feature'|'bug'|'other', flagged:boolean, note:string}>}
 */
export async function reviewClassification(item) {
  const keywordType = toCategory(checkMiscategorization(item));

  const prompt = `
Classify this work item as exactly one of: feature, bug, other.

Work item title: ${escapeForPrompt(item.title)}
Description: ${escapeForPrompt(item.description || 'No description.')}
Current classification: ${keywordType}

Respond with ONLY the word 'feature', 'bug', or 'other' — nothing else.`;

  const content = await callLlm([
    { role: 'system', content: 'You are a changelog classifier. Always respond with exactly one word: feature, bug, or other.' },
    { role: 'user', content: prompt.trim() },
  ], { maxTokens: 10, temperature: 0.1 });

  if (content === null) {
    // No LLM API available: keyword-based classification stands
    return { type: keywordType, flagged: false, note: '' };
  }

  const llmType = content.trim().toLowerCase();
  if (llmType === 'feature' || llmType === 'bug' || llmType === 'other') {
    if (llmType !== keywordType) {
      return {
        type: llmType,
        flagged: true,
        note: `${item.id}: LLM reclassified ${keywordType} → ${llmType}`,
      };
    }
    return { type: keywordType, flagged: false, note: '' };
  }

  // Unparseable LLM response: keep keyword-based classification
  console.error(`[classification review] ${item.id}: unexpected LLM response "${content.trim()}"`);
  return { type: keywordType, flagged: false, note: '' };
}

// ── Categorization ─────────────────────────────────────────────────────────

/**
 * Map `fn` over `items` with a bounded concurrency limit.
 *
 * Prevents overwhelming the LLM API when a release has many work items.
 *
 * @param {Array} items
 * @param {number} limit  Maximum concurrent executions
 * @param {(item: unknown) => Promise<unknown>} fn
 * @returns {Promise<Array<unknown>>}
 */
async function mapWithConcurrency(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;

  async function worker() {
    while (next < items.length) {
      const idx = next;
      next += 1;
      results[idx] = await fn(items[idx]);
    }
  }

  const workers = Array.from({ length: Math.min(limit, items.length) }, worker);
  await Promise.all(workers);
  return results;
}

/**
 * Group items into Features, Bug Fixes, and Other categories.
 *
 * Only parent-level items (no parentId) are processed — child items are
 * skipped so they never create redundant changelog entries (AC1). Each item
 * is processed by the LLM to generate a player-focused description (AC2) and
 * its classification is reviewed and corrected if needed (AC3). When no LLM
 * API key is configured the raw title and keyword-based classification are
 * used — backward-compatible behaviour.
 *
 * @param {Array} items
 * @returns {Promise<{features:string[], bugFixes:string[], other:string[]}>}
 */
export async function categorizeItems(items) {
  const features = [];
  const bugFixes = [];
  const other = [];

  const parents = enrichWithDescriptions(getParentsOnly(items));
  const processed = await mapWithConcurrency(parents, 4, async (item) => {
    const description = await generatePlayerDescription(item);
    const review = await reviewClassification(item);
    return { item, description, ...review };
  });

  for (const { item, description, type, flagged, note } of processed) {
    if (flagged) console.error(`[classification review] ${note}`);
    const entry = `- ${description} (${item.id})`;

    switch (type) {
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

// ── Release focus generation ───────────────────────────────────────────────

/** Maximum number of sentences in the generated release focus (focus AC2). */
export const RELEASE_FOCUS_MAX_SENTENCES = 3;

/**
 * Clamp *text* to at most *max* sentences (sentence boundaries are
 * whitespace following `.`, `!` or `?`), so an over-long LLM response can
 * never bloat the changelog.
 *
 * @param {string} text
 * @param {number} [max=RELEASE_FOCUS_MAX_SENTENCES]
 * @returns {string}
 */
function clampSentences(text, max = RELEASE_FOCUS_MAX_SENTENCES) {
  const trimmed = (text || '').trim();
  if (!trimmed) return '';
  const sentences = trimmed.split(/(?<=[.!?])\s+/).filter(Boolean);
  return sentences.slice(0, max).join(' ').trim();
}

/**
 * Generate a 1–3 sentence summary of the release's main focus from the
 * categorised work items.
 *
 * The focus is persisted by the caller as a `> **Release focus:** …` marker
 * directly under the version heading so discord-notify.js can extract it.
 * Returns null when there are no items, no LLM API key is configured, or the
 * call fails — callers then omit the marker and produce the section as before
 * (focus AC3).
 *
 * @param {{features:string[], bugFixes:string[], other:string[]}} categorized
 * @param {{fetchFn?:Function, maxTokens?:number, temperature?:number}} [opts]
 *   Injectable LLM boundary and sampling overrides (used by unit tests).
 * @returns {Promise<string|null>}
 */
export async function generateReleaseFocus(categorized, opts = {}) {
  const entries = [
    ...(categorized.features || []),
    ...(categorized.bugFixes || []),
    ...(categorized.other || []),
  ].filter(Boolean);

  if (entries.length === 0) return null;

  const content = await callLlm([
    {
      role: 'system',
      content: 'You are a release-notes editor. Respond with a single plain-text ' +
        'paragraph of one to three sentences summarising the main focus of the ' +
        'release. No markdown, no bullets, no quotes.',
    },
    {
      role: 'user',
      content: `Release notes:\n${entries.map((e) => `- ${e}`).join('\n')}\n\n` +
        'Release focus (1-3 sentences):',
    },
  ], { maxTokens: 200, temperature: 0.3, ...opts });

  if (content === null) return null;

  const cleaned = clampSentences(
    content.trim().replace(/^"|"$/g, '').replace(/^'|'$/g, ''),
  );
  return cleaned || null;
}

// ── Markdown generation ────────────────────────────────────────────────────

/**
 * Generate the Markdown section for a single release.
 *
 * When *focus* is provided it is emitted as a `> **Release focus:** …`
 * marker on the first line directly under the version heading (focus AC2).
 * When *cta* is provided it is rendered as a blockquote after the changelog
 * items (cta AC1). Absent CTA produces no output change.
 *
 * @param {string} version  e.g. "0.2.0"
 * @param {string} date     e.g. "2026-07-08"
 * @param {{features:string[], bugFixes:string[], other:string[]}} categorized
 * @param {string|null} [focus]  1–3 sentence release focus, or null to omit.
 * @param {string|null} [cta]      Call-to-action Markdown string, or null to omit.
 * @returns {string}
 */
export function generateReleaseSection(version, date, categorized, focus = null, cta = null) {
  const lines = [];
  const push = (s) => { if (s !== '') lines.push(s); };

  push(`## v${version} (${date})`);
  push('');

  if (focus) {
    push(`> **Release focus:** ${focus}`);
    push('');
  }

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

  if (cta) {
    push(`> ${cta}`);
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

async function main() {
  const args = process.argv.slice(2);

  if (args.length < 1 || args[0] === '-h' || args[0] === '--help') {
    printUsage();
    process.exit(args.length < 1 ? 1 : 0);
  }

  const version = args[0];
  const date = getTodaysDate();

  console.error(`Generating CHANGELOG.md for v${version} (${date}) ...`);

  const allItems = getCompletedOrInReviewItems();
  if (allItems.length === 0) {
    // Loud, actionable warning: an empty set silently produced a header-only
    // changelog section in v1.0.29 (SA-0MWE4BT63005N8OE AC5).
    console.error(
      'WARNING: no work items in stage=in_review — the generated changelog ' +
      'section will be EMPTY. Check that the release candidate set is staged.',
    );
  }
  console.error(`Found ${allItems.length} work item(s) (completed + in_review)`);

  const parentItems = getParentsOnly(allItems);
  console.error(`Of those, ${parentItems.length} parent item(s) will appear in the changelog`);

  const categorized = await categorizeItems(parentItems);
  console.error(
    `Categorised: ${categorized.features.length} feature(s), ` +
    `${categorized.bugFixes.length} bug fix(es), ` +
    `${categorized.other.length} other`,
  );

  const focus = await generateReleaseFocus(categorized);
  if (focus) {
    console.error(`Release focus: ${focus}`);
  } else {
    console.error('Release focus omitted (LLM unavailable or no work items).');
  }

  // Resolve the per-project CTA (same precedence as discord-notify.js).
  // Non-blocking: a missing/malformed CTA is simply omitted.
  const cta = resolveCta();
  if (cta) {
    console.error(`CTA resolved: ${cta}`);
  }

  const newSection = generateReleaseSection(version, date, categorized, focus, cta);
  updateChangelog(newSection);

  console.error(`CHANGELOG.md updated at ${CHANGELOG_PATH}`);
}

// Allow both ESM import and direct CLI execution
const isMainModule = process.argv[1] &&
  realpathSync(fileURLToPath(import.meta.url)) === realpathSync(resolve(process.argv[1]));

if (isMainModule) {
  main().catch((err) => {
    console.error(`Error generating changelog: ${err.message}`);
    process.exit(1);
  });
}
