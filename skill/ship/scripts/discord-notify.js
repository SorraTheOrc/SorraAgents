#!/usr/bin/env node

/**
 * discord-notify.js — post-release Discord notification for the ship skill.
 *
 * After a successful, non-dry-run release has been verified on origin/main,
 * run-release.js calls sendReleaseNotification() to post release details
 * (version, git tag, release date, PR URL, and the new version's changelog
 * section) to a configured Discord channel via a webhook.
 *
 * Configuration (AC2 — precedence: per-project first, then global):
 *   1. <project>/.worklog/config.yaml  →  discord.webhook_url
 *   2. ~/.pi/agent/config.yaml         →  discord.webhook_url  (global fallback)
 * If neither is set, the notification is skipped with an info log — the
 * release completes normally.
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
 * Resolve the Discord webhook URL with per-project precedence over the
 * global fallback (AC2).
 *
 * @param {string} [projectRoot] - Project root (default: process.cwd()).
 * @param {object} [options] - Injectable paths (used by unit tests).
 * @param {string} [options.projectConfigPath] - Default <root>/.worklog/config.yaml.
 * @param {string} [options.globalConfigPath] - Default ~/.pi/agent/config.yaml.
 * @returns {string|null} The resolved webhook URL, or null when unset.
 */
export function resolveDiscordWebhookUrl(projectRoot, options = {}) {
  const {
    projectConfigPath = join(projectRoot || process.cwd(), '.worklog', 'config.yaml'),
    globalConfigPath = join(homedir(), '.pi', 'agent', 'config.yaml'),
  } = options;

  const projectUrl = readWebhookUrlFromConfig(projectConfigPath);
  if (projectUrl) return projectUrl;

  return readWebhookUrlFromConfig(globalConfigPath);
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

// ── Payload builder (AC1) ───────────────────────────────────────────────────

/**
 * Build the Discord webhook embed payload for a release.
 *
 * @param {object} details
 * @param {string} [details.version] - Released semver version.
 * @param {string} [details.tag] - Git tag (vX.Y.Z).
 * @param {string} [details.date] - Release date (YYYY-MM-DD).
 * @param {string} [details.prUrl] - Release PR URL.
 * @param {string} [details.changelog] - Changelog section (truncated to 4096).
 * @returns {{ embeds: Array<object> }} Discord webhook payload.
 */
export function buildDiscordPayload({ version, tag, date, prUrl, changelog } = {}) {
  const versionText = version || 'unknown';
  const tagText = tag || (version ? `v${version}` : 'unknown');
  const truncated = truncateForDiscord(changelog);
  const description = truncated || `No changelog available for v${versionText}.`;

  return {
    embeds: [
      {
        title: `Release v${versionText}`,
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
 * @param {string} [options.projectConfigPath] - Override project config path.
 * @param {string} [options.globalConfigPath] - Override global config path.
 * @param {string} [options.changelogPath] - Override CHANGELOG.md path.
 * @param {string} [options.changelogContent] - Pre-read changelog content.
 * @param {() => Date} [options.now] - Date provider for the date fallback.
 * @param {number} [options.timeoutMs=10000] - Webhook POST timeout.
 * @returns {Promise<{success: boolean, notified: boolean, skipped?: boolean, reason?: string, error?: string}>}
 */
export async function sendReleaseNotification({ version, prUrl, projectRoot }, options = {}) {
  const {
    fetchFn = fetch,
    projectConfigPath,
    globalConfigPath,
    changelogPath = join(projectRoot || process.cwd(), 'CHANGELOG.md'),
    changelogContent,
    now = () => new Date(),
    timeoutMs = 10000,
  } = options;

  const webhookUrl = resolveDiscordWebhookUrl(projectRoot, { projectConfigPath, globalConfigPath });
  if (!webhookUrl) {
    console.log(
      'Discord release notification skipped: no discord.webhook_url configured ' +
      '(checked <project>/.worklog/config.yaml and ~/.pi/agent/config.yaml).',
    );
    return { success: true, notified: false, skipped: true, reason: 'no webhook configured' };
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
  const payload = buildDiscordPayload({
    version,
    tag: `v${version}`,
    date: (section && section.date) || toISODate(now()),
    prUrl,
    changelog: section ? section.text : '',
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
