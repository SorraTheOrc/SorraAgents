#!/usr/bin/env node

/**
 * discord-notify.js — post-release Discord notification for the ship skill.
 *
 * After a successful, non-dry-run release has been verified on origin/main,
 * run-release.js calls sendReleaseNotification() to post release details
 * (version, git tag, release date, PR URL, and the new version's changelog
 * section) to a configured Discord channel via a webhook.
 *
 * Configuration (AC2 — precedence: private → project → global):
 *   1. <project>/.worklog/config.private.yaml  →  discord.webhook_url
 *   2. <project>/.worklog/config.yaml          →  discord.webhook_url
 *   3. ~/.pi/agent/config.yaml                 →  discord.webhook_url  (global fallback)
 * The first file that defines discord.webhook_url wins. If none set,
 * the notification is skipped with an info log — the release completes
 * normally.
 *
 * Behaviour (AC3 — non-blocking): every failure path (fetch rejection, HTTP
 * error status, timeout, missing changelog) logs a warning and returns a
 * success result with `notified: false`. The release exit code is never
 * changed by a notification failure.
 *
 * Limits (AC4): the embed description (changelog) is truncated to ≤ 4096
 * chars with an ellipsis marker.
 *
 * No runtime dependencies beyond Node.js 18+ (built-in fetch, AbortSignal).
 *
 * Usage (internal — invoked by run-release.js, not a user-facing CLI):
 *   import { sendReleaseNotification } from './discord-notify.js';
 *   await sendReleaseNotification({ version, prUrl, projectRoot });
 */

import { readFileSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

// Shared LLM caller (SA-0MUVV6DK0002ISW4) — used only for the README-derived
// project pitch. Importing it (not generate-changelog.js) avoids the latter's
// `git rev-parse` at module-import time.
import { callLlm } from './llm.js';

// Discord embed description character limit (AC4).
export const DISCORD_DESCRIPTION_LIMIT = 4096;

// ── Minimal YAML subset parser ───────────────────────────────────────────────

/**
 * Parse a tiny YAML subset: top-level scalar keys and one level of nesting
 * (enough for `discord.webhook_url`). Values are returned as strings with
 * surrounding quotes stripped. Unknown structure is ignored rather than
 * erroring — config parsing must never break a release.
 *
 * @param {string} content - Raw YAML file content.
 * @returns {Record<string, Record<string, string> | string>} Flat/2-level map.
 */
export function parseSimpleYaml(content) {
  const result = {};
  let current = null; // top-level key whose nested block is being read

  for (const rawLine of (content || '').split(/\r?\n/)) {
    if (/^\s*#/.test(rawLine)) continue;            // whole-line comment
    const line = rawLine.replace(/\s+#.*$/, '').trimEnd(); // inline comment
    if (!line.trim()) continue;

    const indent = line.search(/\S/);
    const match = line.trim().match(/^([A-Za-z0-9_.-]+):\s*(.*)$/);
    if (!match) continue;

    const [, key, value] = match;
    if (indent === 0) {
      if (value === '') {
        current = key;
        result[key] = {};
      } else {
        current = null;
        result[key] = stripScalar(value);
      }
    } else if (current) {
      result[current][key] = value === '' ? {} : stripScalar(value);
    }
  }

  return result;
}

/** Strip surrounding quotes from a scalar value. */
function stripScalar(value) {
  return value.replace(/^["']|["']$/g, '');
}

// ── Config resolution (AC2) ─────────────────────────────────────────────────

/**
 * Read `discord.webhook_url` from a YAML config file, or null if the file is
 * missing or the key is absent.
 *
 * @param {string} configPath - Absolute path to a YAML config file.
 * @returns {string|null} The webhook URL, or null.
 */
export function readWebhookUrlFromConfig(configPath) {
  if (!configPath || !existsSync(configPath)) return null;
  try {
    const parsed = parseSimpleYaml(readFileSync(configPath, 'utf-8'));
    const url = parsed.discord?.webhook_url;
    return typeof url === 'string' && url.trim() !== '' ? url.trim() : null;
  } catch {
    // A corrupt/unreadable config must never break the release — skip quietly.
    return null;
  }
}

/**
 * Resolve the Discord webhook URL with three-layer precedence (AC2):
 *   1. <project>/.worklog/config.private.yaml  (project private)
 *   2. <project>/.worklog/config.yaml          (project, tracked)
 *   3. ~/.pi/agent/config.yaml                 (global fallback)
 * The first file that defines discord.webhook_url wins.
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths (used by unit tests).
 * @param {string} [options.privateConfigPath] - Default <root>/.worklog/config.private.yaml.
 * @param {string} [options.projectConfigPath] - Default <root>/.worklog/config.yaml.
 * @param {string} [options.globalConfigPath] - Default ~/.pi/agent/config.yaml.
 * @returns {string|null} The resolved webhook URL, or null when unset.
 */
export function resolveDiscordWebhookUrl(projectRoot, options = {}) {
  const {
    privateConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.private.yaml'),
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
  } = options;

  const privateUrl = readWebhookUrlFromConfig(privateConfigPath);
  if (privateUrl) return privateUrl;

  const projectUrl = readWebhookUrlFromConfig(projectConfigPath);
  if (projectUrl) return projectUrl;

  return readWebhookUrlFromConfig(globalConfigPath);
}

// ── Project name resolution ─────────────────────────────────────────────────

/**
 * Read `projectName` from a YAML config file, or null if the file is
 * missing or the key is absent.
 *
 * @param {string} configPath - Absolute path to a YAML config file.
 * @returns {string|null} The project name, or null.
 */
export function readProjectNameFromConfig(configPath) {
  if (!configPath || !existsSync(configPath)) return null;
  try {
    const parsed = parseSimpleYaml(readFileSync(configPath, 'utf-8'));
    const name = parsed.projectName;
    return typeof name === 'string' && name.trim() !== '' ? name.trim() : null;
  } catch {
    // A corrupt/unreadable config must never break the release — skip quietly.
    return null;
  }
}

/**
 * Resolve the project name with the same precedence as the webhook URL (AC2):
 *   1. <project>/.worklog/config.private.yaml  (project private)
 *   2. <project>/.worklog/config.yaml          (project, tracked)
 *   3. ~/.pi/agent/config.yaml                 (global fallback)
 * The first file that defines projectName wins.
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths (used by unit tests).
 * @param {string} [options.privateConfigPath] - Default <root>/.worklog/config.private.yaml.
 * @param {string} [options.projectConfigPath] - Default <root>/.worklog/config.yaml.
 * @param {string} [options.globalConfigPath] - Default ~/.pi/agent/config.yaml.
 * @returns {string|null} The resolved project name, or null when unset.
 */
export function resolveProjectName(projectRoot, options = {}) {
  const {
    privateConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.private.yaml'),
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
  } = options;

  const privateName = readProjectNameFromConfig(privateConfigPath);
  if (privateName) return privateName;

  const projectName = readProjectNameFromConfig(projectConfigPath);
  if (projectName) return projectName;

  return readProjectNameFromConfig(globalConfigPath);
}

// ── Project description resolution ──────────────────────────────────────────

/**
 * Read `projectDescription` from a YAML config file, or null if the file is
 * missing or the key is absent.
 *
 * @param {string} configPath - Absolute path to a YAML config file.
 * @returns {string|null} The description, or null.
 */
export function readProjectDescriptionFromConfig(configPath) {
  if (!configPath || !existsSync(configPath)) return null;
  try {
    const parsed = parseSimpleYaml(readFileSync(configPath, 'utf-8'));
    const description = parsed.projectDescription;
    return typeof description === 'string' && description.trim() !== ''
      ? description.trim()
      : null;
  } catch {
    // A corrupt/unreadable config must never break the release — skip quietly.
    return null;
  }
}

/**
 * Resolve the project description with the same precedence as the webhook URL
 * and project name (projectDescription AC1):
 *   1. <project>/.worklog/config.private.yaml  (project private)
 *   2. <project>/.worklog/config.yaml          (project, tracked)
 *   3. ~/.pi/agent/config.yaml                 (global fallback)
 * The first file that defines projectDescription wins.
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths (used by unit tests).
 * @returns {string|null} The resolved description, or null when unset.
 */
export function resolveProjectDescription(projectRoot, options = {}) {
  const {
    privateConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.private.yaml'),
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
  } = options;

  const privateDescription = readProjectDescriptionFromConfig(privateConfigPath);
  if (privateDescription) return privateDescription;

  const projectDescription = readProjectDescriptionFromConfig(projectConfigPath);
  if (projectDescription) return projectDescription;

  return readProjectDescriptionFromConfig(globalConfigPath);
}

// ── Project call-to-action (CTA) resolution ─────────────────────────────────

/**
 * Read the top-level `cta` scalar from a YAML config file, or null if the file
 * is missing, unreadable or the key is absent/empty. Markdown link syntax in
 * the value is preserved verbatim.
 *
 * @param {string} configPath - Absolute path to a YAML config file.
 * @returns {string|null} The configured CTA, or null.
 */
export function readCtaFromConfig(configPath) {
  if (!configPath || !existsSync(configPath)) return null;
  try {
    const parsed = parseSimpleYaml(readFileSync(configPath, 'utf-8'));
    const cta = parsed.cta;
    return typeof cta === 'string' && cta.trim() !== '' ? cta.trim() : null;
  } catch {
    // A corrupt/unreadable config must never break the release — skip quietly.
    return null;
  }
}

/**
 * Resolve the project call-to-action with the same three-layer precedence as
 * the webhook URL, project name and project description (AC2):
 *   1. <project>/.worklog/config.private.yaml  (project private)
 *   2. <project>/.worklog/config.yaml          (project, tracked)
 *   3. ~/.pi/agent/config.yaml                 (global fallback)
 * The first file that defines the top-level `cta` key wins.
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths (used by unit tests).
 * @param {string} [options.privateConfigPath] - Default <root>/.worklog/config.private.yaml.
 * @param {string} [options.projectConfigPath] - Default <root>/.worklog/config.yaml.
 * @param {string} [options.globalConfigPath] - Default ~/.pi/agent/config.yaml.
 * @returns {string|null} The resolved CTA, or null when unset.
 */
export function resolveCta(projectRoot, options = {}) {
  const {
    privateConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.private.yaml'),
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
  } = options;

  const privateCta = readCtaFromConfig(privateConfigPath);
  if (privateCta) return privateCta;

  const projectCta = readCtaFromConfig(projectConfigPath);
  if (projectCta) return projectCta;

  return readCtaFromConfig(globalConfigPath);
}

// ── Changelog extraction (AC1) ──────────────────────────────────────────────

/**
 * Extract the changelog section for a given version from CHANGELOG.md.
 *
 * Sections follow the ship generator's format: `## vX.Y.Z (YYYY-MM-DD)`
 * followed by `### Features` / `### Bug Fixes` / `### Other` blocks. The
 * section runs until the next `## v` heading (or end of file).
 *
 * @param {string} changelog - Full CHANGELOG.md content.
 * @param {string} version - Semver version without the leading "v" (e.g. "1.2.3").
 * @returns {{ date: string, text: string } | null} The section's release date
 *   (from the header) and body text, or null when the section is absent.
 */
export function extractChangelogSection(changelog, version) {
  if (typeof changelog !== 'string' || changelog === '' || !version) return null;

  const escaped = version.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const headerRe = new RegExp(`^## v${escaped} \\(([0-9]{4}-[0-9]{2}-[0-9]{2})\\)`, 'm');
  const headerMatch = changelog.match(headerRe);
  if (!headerMatch) return null;

  const date = headerMatch[1];
  const rest = changelog.slice(headerMatch.index + headerMatch[0].length);
  const nextHeading = rest.match(/^## v/m);
  const text = nextHeading ? rest.slice(0, nextHeading.index) : rest;

  return { date, text: text.trim() };
}

// ── Release-focus marker extraction ─────────────────────────────────────────

/** The machine-extractable release-focus marker emitted by generate-changelog.js. */
export const RELEASE_FOCUS_MARKER = '> **Release focus:**';

/**
 * Extract the release-focus marker from a changelog section body.
 *
 * The marker (`> **Release focus:** …`) is generated by generate-changelog.js
 * and sits directly under the version heading. This returns the focus text and
 * the section body with the marker line removed, so the text is never
 * duplicated in the Discord embed (focus AC3).
 *
 * @param {string} sectionText - The changelog section body.
 * @returns {{ focus: string|null, body: string }}
 */
export function extractReleaseFocus(sectionText) {
  if (typeof sectionText !== 'string' || sectionText === '') {
    return { focus: null, body: typeof sectionText === 'string' ? sectionText : '' };
  }

  const lines = sectionText.split(/\r?\n/);
  let focus = null;
  const kept = [];
  for (const line of lines) {
    const match = line.match(/^>\s*\*\*Release focus:\*\*\s*(.*)$/);
    if (match && focus === null) {
      focus = match[1].trim() || null;
      continue; // drop the marker from the body — it renders as its own paragraph
    }
    kept.push(line);
  }

  return { focus, body: kept.join('\n').trim() };
}

// ── Truncation (AC4) ────────────────────────────────────────────────────────

/**
 * Truncate text to Discord's embed-description limit (≤ 4096 chars),
 * appending an ellipsis marker when truncation is needed.
 *
 * @param {string} text - Text to truncate (e.g. a changelog section).
 * @param {number} [maxLength=4096] - Maximum allowed length.
 * @returns {string} Text within the limit.
 */
export function truncateForDiscord(text, maxLength = DISCORD_DESCRIPTION_LIMIT) {
  if (typeof text !== 'string') return '';
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 1)}…`;
}

// ── Project pitch (README fallback + LLM) ───────────────────────────────────

/** Maximum number of sentences in the generated pitch (pitch AC2). */
export const PITCH_MAX_SENTENCES = 3;

/** Maximum number of README characters fed to the LLM (pitch AC2). */
export const README_PITCH_MAX_LENGTH = 1000;

/**
 * Clamp *text* to at most *max* sentences (sentence boundaries are
 * whitespace following `.`, `!` or `?`).
 *
 * @param {string} text
 * @param {number} [max=PITCH_MAX_SENTENCES]
 * @returns {string}
 */
function clampSentences(text, max = PITCH_MAX_SENTENCES) {
  const trimmed = (text || '').trim();
  if (!trimmed) return '';
  const sentences = trimmed.split(/(?<=[.!?])\s+/).filter(Boolean);
  return sentences.slice(0, max).join(' ').trim();
}

/**
 * Extract the leading prose block from README content: everything before the
 * first `##` heading, capped at *maxLength* characters (pitch AC2).
 *
 * @param {string} readmeContent - Raw README.md content.
 * @param {number} [maxLength=README_PITCH_MAX_LENGTH]
 * @returns {string} The leading prose, or '' when there is none.
 */
export function extractReadmePitch(readmeContent, maxLength = README_PITCH_MAX_LENGTH) {
  if (typeof readmeContent !== 'string' || readmeContent.trim() === '') return '';
  const headingIndex = readmeContent.search(/^##\s/m);
  const block = headingIndex >= 0 ? readmeContent.slice(0, headingIndex) : readmeContent;
  return block.trim().slice(0, maxLength).trim();
}

/**
 * Generate a 2–3 sentence project elevator pitch from README prose via the
 * shared LLM caller. Returns null when the prose is empty or the LLM is
 * unavailable (no API key, network error) so the caller omits the paragraph
 * (pitch AC2).
 *
 * @param {string} readmeProse - Leading README prose (see extractReadmePitch).
 * @param {{fetchFn?:Function, maxTokens?:number, temperature?:number}} [opts]
 *   Injectable LLM boundary and sampling overrides (used by unit tests).
 * @returns {Promise<string|null>}
 */
export async function generatePitch(readmeProse, opts = {}) {
  if (typeof readmeProse !== 'string' || readmeProse.trim() === '') return null;

  const content = await callLlm([
    {
      role: 'system',
      content: 'You are writing a brief project elevator pitch. Respond with a ' +
        'single plain-text paragraph of two to three sentences describing what ' +
        'the project is and who it is for. No markdown, no bullets, no quotes.',
    },
    {
      role: 'user',
      content: `Project README introduction:\n${readmeProse}\n\n` +
        'Elevator pitch (2-3 sentences):',
    },
  ], { maxTokens: 200, temperature: 0.3, ...opts });

  if (content === null) return null;

  const cleaned = clampSentences(
    content.trim().replace(/^"|"$/g, '').replace(/^'|'$/g, ''),
  );
  return cleaned || null;
}

/**
 * Resolve the project elevator pitch (pitch AC1/AC2): the configured
 * `projectDescription` always wins; otherwise the leading README prose is
 * summarised via the LLM; otherwise null (paragraph omitted).
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths/boundaries (used by unit tests).
 * @returns {Promise<string|null>}
 */
export async function resolveProjectPitch(projectRoot, options = {}) {
  const {
    privateConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.private.yaml'),
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
    readmePath = join(projectRoot || process.cwd(), 'README.md'),
    readmeContent,
    llmFetchFn,
    maxLength = README_PITCH_MAX_LENGTH,
  } = options;

  const configured = resolveProjectDescription(projectRoot, {
    privateConfigPath, projectConfigPath, globalConfigPath,
  });
  if (configured) return configured;

  let readme = readmeContent;
  if (readme === undefined) {
    try {
      readme = readFileSync(readmePath, 'utf-8');
    } catch {
      readme = '';
    }
  }

  const prose = extractReadmePitch(readme, maxLength);
  if (!prose) return null;

  return generatePitch(prose, { fetchFn: llmFetchFn });
}

// ── Payload builder (AC1) ───────────────────────────────────────────────────

/**
 * Build the Discord webhook embed payload for a release.
 *
 * Description paragraphs are composed in this order (composition AC5):
 * (a) the project elevator pitch, (b) the release focus, (c) the
 * project/version line, (d) the project call-to-action, and (e) the changelog
 * section — absent paragraphs are omitted. The composed description is
 * truncated to the Discord limit.
 *
 * @param {object} details
 * @param {string} [details.version] - Released semver version.
 * @param {string} [details.tag] - Git tag (vX.Y.Z).
 * @param {string} [details.date] - Release date (YYYY-MM-DD).
 * @param {string} [details.prUrl] - Release PR URL.
 * @param {string} [details.changelog] - Changelog section.
 * @param {string} [details.projectName] - Project name (read from worklog config).
 * @param {string|null} [details.pitch] - Project elevator pitch (2–3 sentences).
 * @param {string|null} [details.focus] - Release focus (1–3 sentences).
 * @param {string|null} [details.cta] - Project call-to-action (markdown preserved).
 * @returns {{ embeds: Array<object> }} Discord webhook payload.
 */
export function buildDiscordPayload({
  version, tag, date, prUrl, changelog, projectName, pitch, focus, cta,
} = {}) {
  const versionText = version || 'unknown';
  const tagText = tag || (version ? `v${version}` : 'unknown');

  // Build title with project name (AC1).
  const title = projectName
    ? `${projectName} Release v${versionText}`
    : `Release v${versionText}`; // fallback when projectName absent (AC3).

  // Compose paragraphs in the required order (composition AC5).
  const paragraphs = [];
  if (pitch) paragraphs.push(pitch);
  if (focus) paragraphs.push(focus);
  if (projectName) paragraphs.push(`**${projectName} v${versionText}**`);
  // CTA follows the intro block and sits immediately before the changelog.
  if (cta) paragraphs.push(cta);
  paragraphs.push(
    changelog ? changelog : `No changelog available for v${versionText}.`,
  );

  const description = truncateForDiscord(paragraphs.join('\n\n'));

  return {
    embeds: [
      {
        title,
        description,
        color: 0x2ecc71, // green — successful release
        fields: [
          { name: 'Version', value: versionText, inline: true },
          { name: 'Tag', value: tagText, inline: true },
          { name: 'Date', value: date || 'unknown', inline: true },
          { name: 'Pull Request', value: prUrl || 'n/a' },
        ],
      },
    ],
  };
}

// ── Orchestrator (AC1, AC2, AC3) ────────────────────────────────────────────

/** Format a Date as YYYY-MM-DD (local time, matching generate-changelog.js). */
function toISODate(d) {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

/**
 * Send the post-release Discord notification (non-blocking).
 *
 * Resolves the webhook URL (project → global), extracts the released
 * version's changelog section, builds the embed payload, and POSTs it via
 * built-in fetch with a bounded timeout. Every failure path logs a warning
 * and returns `{ success: true, notified: false }` so the release exit code
 * is never changed (AC3). When no webhook is configured the step is skipped
 * with an info log and the release completes normally (AC2).
 *
 * @param {object} release
 * @param {string} release.version - Released semver version (e.g. "1.2.3").
 * @param {string|null} [release.prUrl] - Release PR URL.
 * @param {string} [release.projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable boundaries (used by unit tests).
 * @param {Function} [options.fetchFn] - fetch implementation (default: global fetch).
 * @param {Function} [options.llmFetchFn] - LLM fetch boundary for pitch generation.
 * @param {string} [options.projectConfigPath] - Override project config path.
 * @param {string} [options.globalConfigPath] - Override global config path.
 * @param {string} [options.privateConfigPath] - Override private config path.
 * @param {string} [options.changelogPath] - Override CHANGELOG.md path.
 * @param {string} [options.readmePath] - Override README.md path (pitch fallback).
 * @param {string} [options.readmeContent] - Pre-read README content.
 * @param {string} [options.changelogContent] - Pre-read changelog content.
 * @param {() => Date} [options.now] - Date provider for the date fallback.
 * @param {number} [options.timeoutMs=10000] - Webhook POST timeout.
 * @returns {Promise<{success: boolean, notified: boolean, skipped?: boolean, reason?: string, error?: string}>}
 */
export async function sendReleaseNotification({ version, prUrl, projectRoot }, options = {}) {
  const {
    fetchFn = fetch,
    llmFetchFn,
    privateConfigPath,
    projectConfigPath,
    globalConfigPath,
    changelogPath = join(projectRoot || process.cwd(), 'CHANGELOG.md'),
    readmePath = join(projectRoot || process.cwd(), 'README.md'),
    readmeContent,
    changelogContent,
    now = () => new Date(),
    timeoutMs = 10000,
  } = options;

  const webhookUrl = resolveDiscordWebhookUrl(projectRoot, { privateConfigPath, projectConfigPath, globalConfigPath });
  if (!webhookUrl) {
    console.log(
      'Discord release notification skipped: no discord.webhook_url configured ' +
      '(checked <project>/.worklog/config.private.yaml, <project>/.worklog/config.yaml, and ~/.pi/agent/config.yaml).',
    );
    return { success: true, notified: false, skipped: true, reason: 'no webhook configured' };
  }

  // Resolve the project name (AC1, AC2, AC3 — same precedence as webhook URL).
  const projectName = resolveProjectName(projectRoot, { privateConfigPath, projectConfigPath, globalConfigPath });

  // Resolve the project call-to-action (same precedence as projectName).
  // Non-blocking: a missing/malformed/unreadable CTA is simply omitted.
  const cta = resolveCta(projectRoot, { privateConfigPath, projectConfigPath, globalConfigPath });

  // Resolve the project pitch: configured projectDescription wins; otherwise
  // generate it from README prose. Non-blocking — any failure omits it.
  let pitch = null;
  try {
    pitch = await resolveProjectPitch(projectRoot, {
      privateConfigPath, projectConfigPath, globalConfigPath,
      readmePath, readmeContent, llmFetchFn,
    });
    if (!pitch) {
      console.log('Discord release notification: no project pitch available — omitting pitch paragraph.');
    }
  } catch (err) {
    console.warn(
      `⚠ Discord release notification: pitch resolution failed: ${err.message} ` +
      '(non-blocking — release continues).',
    );
  }

  let changelog = changelogContent;
  if (changelog === undefined) {
    try {
      changelog = readFileSync(changelogPath, 'utf-8');
    } catch {
      changelog = '';
    }
  }

  const section = extractChangelogSection(changelog || '', version);

  // Extract (and remove) the release-focus marker so the focus is its own
  // paragraph and never duplicated in the changelog body (focus AC3).
  const { focus, body } = extractReleaseFocus(section ? section.text : '');
  if (!focus) {
    console.log('Discord release notification: no release-focus marker — omitting focus paragraph.');
  }

  const payload = buildDiscordPayload({
    version,
    tag: `v${version}`,
    date: (section && section.date) || toISODate(now()),
    prUrl,
    changelog: body,
    projectName,
    pitch,
    focus,
    cta,
  });

  try {
    const response = await fetchFn(webhookUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });

    if (!response.ok) {
      console.warn(
        `⚠ Discord release notification failed: HTTP ${response.status} ${response.statusText} ` +
        '(non-blocking — release continues).',
      );
      return { success: true, notified: false, error: `HTTP ${response.status} ${response.statusText}` };
    }

    console.log(`Discord release notification sent for v${version}.`);
    return { success: true, notified: true };
  } catch (err) {
    console.warn(
      `⚠ Discord release notification failed: ${err.message} (non-blocking — release continues).`,
    );
    return { success: true, notified: false, error: err.message };
  }
}
