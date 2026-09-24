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

// ── Resolve repo root ──────────────────────────────────────────────────────
const REPO_ROOT = execSync('git rev-parse --show-toplevel', { encoding: 'utf-8' }).trim();
const CHANGELOG_PATH = resolve(REPO_ROOT, 'CHANGELOG.md');

// ── Worklog helpers ────────────────────────────────────────────────────────

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
 * @returns {Array<{id:string, title:string, issueType:string, description:string, parentId?:string|null}>}
 */
export function getCompletedOrInReviewItems() {
  try {
    // Single query (stage=in_review implies status=completed), piped through
    // jq so only {id, title, issueType, description, parentId} enters the buffer.
    // parentId is needed to filter parent-only work items for the changelog.
    const output = execSync(
      `bash -c 'set -o pipefail; wl list --stage in_review --json ` +
      `| jq -c \"[.workItems[] | {id, title, issueType, description, parentId}]\"'`,
      { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] },
    );
    return JSON.parse(output) || [];
  } catch {
    // wl may not be available; caller handles this gracefully
    console.error('Warning: could not query completed/in_review work items (wl not available?)');
    return [];
  }
}

// ── Parent-only filtering ──────────────────────────────────────────────────

/**
 * Filter work items to only parent items (those with no parentId).
 * This excludes child/subtask items that would create redundant changelog entries.
 *
 * @param {Array<{id:string, title:string, issueType:string, description:string, parentId?:string|null}>} items
 * @returns {Array<{id:string, title:string, issueType:string, description:string}>}
 */
export function getParentsOnly(items) {
  return items.filter(
    (item) => item.parentId === null || item.parentId === undefined,
  );
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

/**
 * Shared LLM chat-completion caller (DeepSeek, OpenAI-compatible API).
 *
 * Returns the assistant message content, or null when no API key is
 * configured or the call fails. Callers fall back to their non-LLM
 * behaviour in that case, so the script stays backward-compatible
 * when no key is present.
 *
 * @param {Array<{role:string, content:string}>} messages
 * @param {{maxTokens?:number, temperature?:number}} opts
 * @returns {Promise<string|null>}
 */
async function callLlm(messages, opts = {}) {
  const apiKey = process.env.DEEPSEEK_API_KEY;
  if (!apiKey) return null;

  try {
    const response = await fetch('https://api.deepseek.com/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model: 'deepseek-chat',
        messages,
        max_tokens: opts.maxTokens ?? 120,
        temperature: opts.temperature ?? 0.3,
      }),
      signal: AbortSignal.timeout(30000),
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    return data.choices?.[0]?.message?.content ?? null;
  } catch (err) {
    console.error(`[LLM] ${err.message}`);
    return null;
  }
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

  const parents = getParentsOnly(items);
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
  console.error(`Found ${allItems.length} work item(s) (completed + in_review)`);

  const parentItems = getParentsOnly(allItems);
  console.error(`Of those, ${parentItems.length} parent item(s) will appear in the changelog`);

  const categorized = await categorizeItems(parentItems);
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
  main().catch((err) => {
    console.error(`Error generating changelog: ${err.message}`);
    process.exit(1);
  });
}
