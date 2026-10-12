/**
 * Tests for discord-notify.js (SA-0MSQ6K7Z1002H14Z).
 *
 * Covers: config precedence (AC2), skip-when-unset, changelog extraction,
 * truncation, payload shape, and non-blocking failure behaviour (AC5).
 *
 * Uses Node.js built-in `node --test` runner.
 */

import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';

const __filename = fileURLToPath(import.meta.url); // eslint-disable-line no-unused-vars
const __dirname = dirname(__filename); // eslint-disable-line no-unused-vars

// Import the module under test.
import {
  parseSimpleYaml,
  readWebhookUrlFromConfig,
  readProjectNameFromConfig,
  resolveDiscordWebhookUrl,
  resolveProjectName,
  readProjectDescriptionFromConfig,
  resolveProjectDescription,
  readCtaFromConfig,
  resolveCta,
  extractReadmePitch,
  generatePitch,
  resolveProjectPitch,
  extractChangelogSection,
  extractReleaseFocus,
  truncateForDiscord,
  buildDiscordPayload,
  sendReleaseNotification,
  DISCORD_DESCRIPTION_LIMIT,
} from '../scripts/discord-notify.js';

// ─── Test helpers ────────────────────────────────────────────────────────────

/** Create a temporary directory and return its path. */
function mkTmpDir(prefix = 'discord-test-') {
  const d = join(tmpdir(), prefix + Date.now() + '-' + Math.random().toString(36).slice(2, 6));
  mkdirSync(d, { recursive: true });
  return d;
}

/** Clean up a temporary directory. */
function rmTmpDir(dir) {
  rmSync(dir, { recursive: true, force: true });
}

const WEBHOOK_URL = 'https://discord.com/api/webhooks/test/secret-token';

/** Write a config.yaml file whose `discord.webhook_url` equals `WEBHOOK_URL`. */
function writeWebhookConfig(dir, filename = 'config.yaml', url = WEBHOOK_URL) {
  writeFileSync(join(dir, filename),
    'discord:\n  webhook_url: ' + url + '\n');
}

// ─── parseSimpleYaml ────────────────────────────────────────────────────────

describe('parseSimpleYaml', () => {
  it('parses a top-level key with a scalar value', () => {
    const yaml = 'projectName: TestRepo\n';
    const result = parseSimpleYaml(yaml);
    assert.deepStrictEqual(result, { projectName: 'TestRepo' });
  });

  it('parses a nested key (discord.webhook_url)', () => {
    const yaml =
      'discord:\n  webhook_url: https://discord.com/api/webhooks/abc/token\n';
    const result = parseSimpleYaml(yaml);
    assert.deepStrictEqual(result.discord.webhook_url, 'https://discord.com/api/webhooks/abc/token');
  });

  it('ignores comments', () => {
    const yaml =
      '# This is a comment\nprojectName: Test # inline comment\n';
    const result = parseSimpleYaml(yaml);
    assert.deepStrictEqual(result.projectName, 'Test');
  });

  it('strips surrounding quotes from values', () => {
    const yaml = 'webhook_url: "https://example.com/hook"\n';
    const result = parseSimpleYaml(yaml);
    assert.deepStrictEqual(result.webhook_url, 'https://example.com/hook');
  });

  it('returns an empty object for empty input', () => {
    assert.deepStrictEqual(parseSimpleYaml(''), {});
    assert.deepStrictEqual(parseSimpleYaml(null), {});
    assert.deepStrictEqual(parseSimpleYaml(undefined), {});
  });
});

// ─── readWebhookUrlFromConfig ────────────────────────────────────────────────

describe('readWebhookUrlFromConfig', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = mkTmpDir();
  });

  afterEach(() => {
    rmTmpDir(tmpDir);
  });

  it('returns null for a non-existent file', () => {
    assert.strictEqual(
      readWebhookUrlFromConfig(join(tmpDir, 'nonexistent.yaml')),
      null,
    );
  });

  it('returns null when discord.webhook_url is absent', () => {
    writeFileSync(join(tmpDir, 'config.yaml'), 'projectName: Test\n');
    assert.strictEqual(readWebhookUrlFromConfig(join(tmpDir, 'config.yaml')), null);
  });

  it('returns the webhook URL when present', () => {
    const yaml = 'discord:\n  webhook_url: https://discord.com/api/webhooks/123/abc\n';
    writeFileSync(join(tmpDir, 'config.yaml'), yaml);
    assert.strictEqual(
      readWebhookUrlFromConfig(join(tmpDir, 'config.yaml')),
      'https://discord.com/api/webhooks/123/abc',
    );
  });

  it('handles a corrupt YAML file gracefully', () => {
    writeFileSync(join(tmpDir, 'config.yaml'), '\x00\x01\x02');
    assert.strictEqual(readWebhookUrlFromConfig(join(tmpDir, 'config.yaml')), null);
  });
});

// ─── resolveDiscordWebhookUrl (AC2 — config precedence) ─────────────────────

describe('resolveDiscordWebhookUrl', () => {
  let projectDir, globalDir;

  beforeEach(() => {
    projectDir = mkTmpDir('project-');
    globalDir = mkTmpDir('global-');
  });

  afterEach(() => {
    rmTmpDir(projectDir);
    rmTmpDir(globalDir);
  });

  it('returns null when neither config has a webhook URL', () => {
    const result = resolveDiscordWebhookUrl(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, null);
  });

  it('prefers per-project config over global (AC2)', () => {
    const projectUrl = 'https://discord.com/api/webhooks/project/secret';
    const globalUrl = 'https://discord.com/api/webhooks/global/secret';

    writeFileSync(join(projectDir, 'config.yaml'),
      'discord:\n  webhook_url: ' + projectUrl + '\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'discord:\n  webhook_url: ' + globalUrl + '\n');

    const result = resolveDiscordWebhookUrl(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, projectUrl);
  });

  it('falls back to global config when per-project is unset', () => {
    const globalUrl = 'https://discord.com/api/webhooks/global/secret';
    writeFileSync(join(projectDir, 'config.yaml'), 'projectName: Test\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'discord:\n  webhook_url: ' + globalUrl + '\n');

    const result = resolveDiscordWebhookUrl(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, globalUrl);
  });

  it('prefers per-project even when global is present', () => {
    const projectUrl = 'https://hooks.discord.com/project';
    const globalUrl = 'https://hooks.discord.com/global';

    writeFileSync(join(projectDir, 'config.yaml'),
      'discord:\n  webhook_url: ' + projectUrl + '\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'discord:\n  webhook_url: ' + globalUrl + '\n');

    const result = resolveDiscordWebhookUrl(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, projectUrl);
  });
});

// ─── readProjectNameFromConfig ───────────────────────────────────────────────

describe('readProjectNameFromConfig', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = mkTmpDir();
  });

  afterEach(() => {
    rmTmpDir(tmpDir);
  });

  it('returns null for a non-existent file', () => {
    assert.strictEqual(
      readProjectNameFromConfig(join(tmpDir, 'nonexistent.yaml')),
      null,
    );
  });

  it('returns null when projectName is absent', () => {
    writeFileSync(join(tmpDir, 'config.yaml'), 'discord:\n  webhook_url: https://example.com\n');
    assert.strictEqual(readProjectNameFromConfig(join(tmpDir, 'config.yaml')), null);
  });

  it('returns the project name when present', () => {
    const yaml = 'projectName: TestRepo\n';
    writeFileSync(join(tmpDir, 'config.yaml'), yaml);
    assert.strictEqual(
      readProjectNameFromConfig(join(tmpDir, 'config.yaml')),
      'TestRepo',
    );
  });

  it('handles a corrupt YAML file gracefully', () => {
    writeFileSync(join(tmpDir, 'config.yaml'), '\x00\x01\x02');
    assert.strictEqual(readProjectNameFromConfig(join(tmpDir, 'config.yaml')), null);
  });
});

// ─── resolveProjectName (AC2 — config precedence) ────────────────────────────

describe('resolveProjectName', () => {
  let projectDir, globalDir;

  beforeEach(() => {
    projectDir = mkTmpDir('project-');
    globalDir = mkTmpDir('global-');
  });

  afterEach(() => {
    rmTmpDir(projectDir);
    rmTmpDir(globalDir);
  });

  it('returns null when neither config has a project name', () => {
    const result = resolveProjectName(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, null);
  });

  it('prefers per-project config over global (AC2)', () => {
    const projectTitle = 'MyProject';
    const globalTitle = 'GlobalProject';

    writeFileSync(join(projectDir, 'config.yaml'),
      'projectName: ' + projectTitle + '\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'projectName: ' + globalTitle + '\n');

    const result = resolveProjectName(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, projectTitle);
  });

  it('falls back to global config when per-project is unset', () => {
    const globalTitle = 'GlobalProject';
    writeFileSync(join(projectDir, 'config.yaml'), 'discord:\n  webhook_url: https://example.com\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'projectName: ' + globalTitle + '\n');

    const result = resolveProjectName(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, globalTitle);
  });

  it('prefers per-project even when global is present', () => {
    const projectTitle = 'LocalProject';
    const globalTitle = 'GlobalProject';

    writeFileSync(join(projectDir, 'config.yaml'),
      'projectName: ' + projectTitle + '\n');
    writeFileSync(join(globalDir, 'config.yaml'),
      'projectName: ' + globalTitle + '\n');

    const result = resolveProjectName(projectDir, {
      projectConfigPath: join(projectDir, 'config.yaml'),
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.strictEqual(result, projectTitle);
  });
});

// ─── extractChangelogSection (AC1) ──────────────────────────────────────────

describe('extractChangelogSection', () => {
  const fullChangelog =
`# Changelog

## v1.0.0 (2024-01-01)
### Features
- Initial release

## v1.1.0 (2024-02-15)
### Features
- New feature A
### Bug Fixes
- Fixed bug B

## v2.0.0 (2024-06-01)
### Breaking
- Breaking change
`;

  it('returns null when version section is not found', () => {
    const result = extractChangelogSection(fullChangelog, '9.9.9');
    assert.strictEqual(result, null);
  });

  it('returns the date and body for an existing version', () => {
    const result = extractChangelogSection(fullChangelog, '1.1.0');
    assert.ok(result);
    assert.strictEqual(result.date, '2024-02-15');
    assert.ok(result.text.includes('New feature A'));
    assert.ok(result.text.includes('Fixed bug B'));
  });

  it('handles a version at the end of the file (no next heading)', () => {
    const result = extractChangelogSection(fullChangelog, '2.0.0');
    assert.ok(result);
    assert.strictEqual(result.date, '2024-06-01');
    assert.ok(result.text.includes('Breaking change'));
  });

  it('handles an empty changelog', () => {
    assert.strictEqual(extractChangelogSection('', '1.0.0'), null);
    assert.strictEqual(extractChangelogSection(null, '1.0.0'), null);
  });

  it('handles a null version', () => {
    assert.strictEqual(extractChangelogSection(fullChangelog, null), null);
    assert.strictEqual(extractChangelogSection(fullChangelog, ''), null);
  });

  it('escapes special regex characters in version', () => {
    const changelog = '## v1.0.0-beta.1 (2024-03-01)\n### Features\n- Added\n';
    const result = extractChangelogSection(changelog, '1.0.0-beta.1');
    assert.ok(result);
    assert.strictEqual(result.date, '2024-03-01');
    assert.ok(result.text.includes('Added'));
  });
});

// ─── truncateForDiscord (AC4) ───────────────────────────────────────────────

describe('truncateForDiscord', () => {
  it('returns text as-is when under the limit', () => {
    const text = 'short text';
    assert.strictEqual(truncateForDiscord(text), text);
  });

  it('truncates text that exceeds the limit', () => {
    const text = 'x'.repeat(5000);
    const result = truncateForDiscord(text);
    assert.ok(result.length <= DISCORD_DESCRIPTION_LIMIT);
    assert.ok(result.endsWith('…'));
  });

  it('appends ellipsis when truncation occurs', () => {
    const text = 'a'.repeat(4096 + 10);
    const result = truncateForDiscord(text);
    assert.ok(result.endsWith('…'));
  });

  it('handles non-string input', () => {
    assert.strictEqual(truncateForDiscord(null), '');
    assert.strictEqual(truncateForDiscord(undefined), '');
    assert.strictEqual(truncateForDiscord(123), '');
  });

  it('respects a custom max length', () => {
    const text = 'hello world';
    // slice(0, maxLength-1) + ellipsis => 4 chars + ellipsis = 5 total.
    const result = truncateForDiscord(text, 5);
    assert.strictEqual(result, 'hell…');
  });
});

// ─── buildDiscordPayload (AC1) ──────────────────────────────────────────────

describe('buildDiscordPayload', () => {
  it('produces a valid embed payload with all fields', () => {
    const payload = buildDiscordPayload({
      version: '1.2.3',
      tag: 'v1.2.3',
      date: '2024-08-01',
      prUrl: 'https://github.com/example/repo/pull/42',
      changelog: '### Features\n- Added feature X\n',
      projectName: 'ContextHub',
    });
    assert.ok(Array.isArray(payload.embeds));
    assert.strictEqual(payload.embeds.length, 1);
    const embed = payload.embeds[0];
    assert.strictEqual(embed.title, 'ContextHub Release v1.2.3');
    assert.strictEqual(embed.color, 0x2ecc71); // green
    assert.ok(embed.description.includes('Added feature X'));
    assert.deepStrictEqual(embed.fields, [
      { name: 'Version', value: '1.2.3', inline: true },
      { name: 'Tag', value: 'v1.2.3', inline: true },
      { name: 'Date', value: '2024-08-01', inline: true },
      { name: 'Pull Request', value: 'https://github.com/example/repo/pull/42' },
    ]);
  });

  it('handles missing changelog gracefully', () => {
    const payload = buildDiscordPayload({ version: '1.2.3' });
    assert.ok(payload.embeds[0].description.includes('No changelog available'));
  });

  it('handles unknown version', () => {
    const payload = buildDiscordPayload({ projectName: 'TestProject' });
    assert.strictEqual(payload.embeds[0].title, 'TestProject Release vunknown');
    assert.strictEqual(payload.embeds[0].fields[0].value, 'unknown');
  });

  it('falls back to no-project-name title when projectName is absent (AC3)', () => {
    const payload = buildDiscordPayload({ version: '1.0.0' });
    assert.strictEqual(payload.embeds[0].title, 'Release v1.0.0');
  });

  it('includes the project name and version in the description (AC4)', () => {
    const payload = buildDiscordPayload({
      version: '1.2.3',
      changelog: '### Features\n- Added feature X\n',
      projectName: 'ContextHub',
    });
    const description = payload.embeds[0].description;
    assert.ok(description.includes('ContextHub v1.2.3'));
    assert.ok(description.includes('Added feature X'));
  });

  it('does not add a project header to the description when projectName is absent (AC4)', () => {
    const payload = buildDiscordPayload({ version: '1.2.3', changelog: '### Features\n- X\n' });
    assert.ok(!payload.embeds[0].description.includes('**'));
  });

  it('truncates long changelog in the payload', () => {
    const longChangelog = 'x'.repeat(5000);
    const payload = buildDiscordPayload({ version: '1.0.0', changelog: longChangelog });
    assert.ok(payload.embeds[0].description.length <= DISCORD_DESCRIPTION_LIMIT);
  });
});

// ─── sendReleaseNotification (AC1, AC2, AC3 — non-blocking) ─────────────────

describe('sendReleaseNotification', () => {
  let tmpDir;

  beforeEach(() => {
    tmpDir = mkTmpDir();
  });

  afterEach(() => {
    rmTmpDir(tmpDir);
  });

  it('returns {success:true, notified:false, skipped:true} when no webhook configured', async () => {
    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'global.yaml'),
        now: () => new Date('2024-08-01'),
      },
    );
    assert.deepStrictEqual(result, {
      success: true,
      notified: false,
      skipped: true,
      reason: 'no webhook configured',
    });
  });

  it('sends notification when webhook URL is configured', async () => {
    writeWebhookConfig(tmpDir);
    let capturedBody = null;
    const mockFetch = async (url, opts) => {
      capturedBody = opts.body;
      return { ok: true, status: 200 };
    };

    const changelogContent = '## v1.2.3 (2024-08-01)\n### Features\n- New feature\n';
    const result = await sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/example/repo/pull/42', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent,
        now: () => new Date('2024-08-01'),
      },
    );
    assert.deepStrictEqual(result, { success: true, notified: true });

    const payload = JSON.parse(capturedBody);
    // Without projectName in config, falls back to generic title (AC3).
    assert.strictEqual(payload.embeds[0].title, 'Release v1.2.3');
    assert.ok(payload.embeds[0].description.includes('New feature'));
  });

  it('includes project name in title when configured (AC1)', async () => {
    writeFileSync(join(tmpDir, 'config.yaml'),
      'projectName: ContextHub\ndiscord:\n  webhook_url: ' + WEBHOOK_URL + '\n');
    let capturedBody = null;
    const mockFetch = async (url, opts) => {
      capturedBody = opts.body;
      return { ok: true, status: 200 };
    };

    const changelogContent = '## v1.2.3 (2024-08-01)\n### Features\n- New feature\n';
    const result = await sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/example/repo/pull/42', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent,
        now: () => new Date('2024-08-01'),
      },
    );
    assert.deepStrictEqual(result, { success: true, notified: true });

    const payload = JSON.parse(capturedBody);
    assert.strictEqual(payload.embeds[0].title, 'ContextHub Release v1.2.3');
  });

  it('is non-blocking on HTTP error (AC3)', async () => {
    writeWebhookConfig(tmpDir);
    const mockFetch = async () => ({ ok: false, status: 500, statusText: 'Internal Server Error' });

    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent: '## v1.2.3 (2024-08-01)\n',
        now: () => new Date('2024-08-01'),
      },
    );
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.notified, false);
    assert.ok(result.error);
  });

  it('is non-blocking on fetch rejection / network error (AC3)', async () => {
    writeWebhookConfig(tmpDir);
    const mockFetch = async () => {
      throw new Error('Network unreachable');
    };

    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent: '## v1.2.3 (2024-08-01)\n',
        now: () => new Date('2024-08-01'),
      },
    );
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.notified, false);
    assert.ok(result.error);
  });

  it('is non-blocking when fetch throws AbortError (timeout-like, AC3)', async () => {
    writeWebhookConfig(tmpDir);
    const mockFetch = async () => {
      const error = new Error('This operation was aborted');
      error.name = 'AbortError';
      throw error;
    };

    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent: '## v1.2.3 (2024-08-01)\n',
        now: () => new Date('2024-08-01'),
      },
    );
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.notified, false);
    assert.ok(result.error);
  });

  it('reads CHANGELOG.md from disk when changelogContent not provided', async () => {
    writeWebhookConfig(tmpDir);
    const changelogText = '## v1.2.3 (2024-08-01)\n### Features\n- Feature from file\n';
    writeFileSync(join(tmpDir, 'CHANGELOG.md'), changelogText);

    let capturedBody = null;
    const mockFetch = async (url, opts) => {
      capturedBody = opts.body;
      return { ok: true };
    };

    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        now: () => new Date('2024-08-01'),
      },
    );
    assert.strictEqual(result.notified, true);
    const payload = JSON.parse(capturedBody);
    assert.ok(payload.embeds[0].description.includes('Feature from file'));
  });

  it('uses section date when available', async () => {
    writeWebhookConfig(tmpDir);
    const changelogContent = '## v1.2.3 (2024-08-01)\n### Features\n- Feature\n';
    let capturedBody = null;
    const mockFetch = async (url, opts) => {
      capturedBody = opts.body;
      return { ok: true };
    };

    await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogContent,
      },
    );

    const payload = JSON.parse(capturedBody);
    const dateField = payload.embeds[0].fields.find((f) => f.name === 'Date');
    assert.strictEqual(dateField.value, '2024-08-01');
  });

  it('handles missing CHANGELOG.md gracefully (notified true, fallback date)', async () => {
    writeWebhookConfig(tmpDir);
    const mockFetch = async () => ({ ok: true });

    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: tmpDir },
      {
        fetchFn: mockFetch,
        projectConfigPath: join(tmpDir, 'config.yaml'),
        globalConfigPath: join(tmpDir, 'config.yaml'),
        changelogPath: join(tmpDir, 'CHANGELOG.md'),
        now: () => new Date('2024-08-01'),
      },
    );
    assert.strictEqual(result.success, true);
    assert.strictEqual(result.notified, true);
  });
});

// ---------------------------------------------------------------------------
// SA-0MUVWL09T001N5P4 — project pitch + release focus composition
// ---------------------------------------------------------------------------

/** Create a temp project dir with a .worklog/ folder. */
function mkProjectDir() {
  const d = mkTmpDir('discord-compose-');
  mkdirSync(join(d, '.worklog'), { recursive: true });
  return d;
}

/** A fetch stub returning a fixed assistant message (injected LLM boundary). */
function stubLlmFetch(content) {
  return async () => ({
    ok: true,
    json: async () => ({ choices: [{ message: { content } }] }),
  });
}

/** Write a config with a webhook and an optional projectDescription. */
function writeCompositionConfig(path, { webhookUrl = WEBHOOK_URL, description } = {}) {
  mkdirSync(dirname(path), { recursive: true });
  let yaml = `projectName: Test Project\nprefix: TP\ndiscord:\n  webhook_url: ${webhookUrl}\n`;
  if (description) yaml += `projectDescription: ${description}\n`;
  writeFileSync(path, yaml);
}

const COMPOSE_CHANGELOG = `# Changelog

## v1.2.3 (2026-01-15)

### Features

- Added something (SA-ABC1)

## v1.2.2 (2025-12-20)

### Features

- Older feature (SA-OLD)
`;

// ---------------------------------------------------------------------------
// SA-0MUWCIFU2006K39K — project call-to-action (CTA) in the release report
// ---------------------------------------------------------------------------

/** Reproduces AI_Hell/.worklog/config.yaml (WL-0MUWCGF670087GAS). */
const AI_HELL_CTA =
  '[Play the alpha release](https://sorratheorc.github.io/AI_Hell/). ' +
  'Provide feedback in [Discord](https://discord.gg/gUKQTFkzQ4)';

/** Write a config with a webhook and an optional CTA. */
function writeCtaConfig(path, { webhookUrl = WEBHOOK_URL, cta } = {}) {
  mkdirSync(dirname(path), { recursive: true });
  let yaml = `projectName: Test Project\nprefix: TP\ndiscord:\n  webhook_url: ${webhookUrl}\n`;
  if (cta !== undefined) yaml += `cta: "${cta}"\n`;
  writeFileSync(path, yaml);
}

describe('discord-notify: CTA config resolution (SA-0MUWCIFU2006K39K AC1)', () => {
  it('exports readCtaFromConfig and resolveCta', () => {
    assert.strictEqual(typeof readCtaFromConfig, 'function');
    assert.strictEqual(typeof resolveCta, 'function');
  });

  it('readCtaFromConfig returns the top-level cta scalar verbatim (markdown preserved)', () => {
    const dir = mkTmpDir('discord-cta-');
    const path = join(dir, 'config.yaml');
    writeCtaConfig(path, { cta: AI_HELL_CTA });
    assert.strictEqual(readCtaFromConfig(path), AI_HELL_CTA);
    rmTmpDir(dir);
  });

  it('readCtaFromConfig returns null for missing file, absent/empty key and unreadable path', () => {
    const dir = mkTmpDir('discord-cta-');
    assert.strictEqual(readCtaFromConfig(join(dir, 'missing.yaml')), null);

    const path = join(dir, 'config.yaml');
    writeFileSync(path, 'projectName: Test Project\n');
    assert.strictEqual(readCtaFromConfig(path), null);

    writeFileSync(path, 'cta: "   "\n');
    assert.strictEqual(readCtaFromConfig(path), null);

    assert.strictEqual(readCtaFromConfig(dir), null);
    rmTmpDir(dir);
  });

  it('resolveCta uses the same three-layer precedence as projectName', () => {
    const root = mkTmpDir('discord-cta-');
    const globalDir = mkTmpDir('discord-cta-global-');
    writeCtaConfig(join(globalDir, 'config.yaml'), { cta: 'global cta' });
    const globalPath = join(globalDir, 'config.yaml');
    mkdirSync(join(root, '.worklog'), { recursive: true });

    assert.strictEqual(resolveCta(root, { globalConfigPath: globalPath }), 'global cta');

    writeCtaConfig(join(root, '.worklog', 'config.yaml'), { cta: 'project cta' });
    assert.strictEqual(resolveCta(root, { globalConfigPath: globalPath }), 'project cta');

    writeCtaConfig(join(root, '.worklog', 'config.private.yaml'), { cta: 'private cta' });
    assert.strictEqual(resolveCta(root, { globalConfigPath: globalPath }), 'private cta');
    rmTmpDir(root);
    rmTmpDir(globalDir);
  });

  it('resolveCta returns null when unset everywhere and for a malformed cta key', () => {
    const root = mkTmpDir('discord-cta-');
    mkdirSync(join(root, '.worklog'), { recursive: true });
    assert.strictEqual(resolveCta(root, { globalConfigPath: join(root, 'none.yaml') }), null);

    writeFileSync(join(root, '.worklog', 'config.yaml'), 'cta:\n  nested: value\n');
    assert.strictEqual(resolveCta(root, { globalConfigPath: join(root, 'none.yaml') }), null);
    rmTmpDir(root);
  });
});

describe('discord-notify: CTA embed composition (SA-0MUWCIFU2006K39K AC2/AC4)', () => {
  it('places the CTA after pitch/focus/version and immediately before the changelog', () => {
    const d = buildDiscordPayload({
      version: '1.2.3', projectName: 'AI Hell',
      pitch: 'Pitch line.', focus: 'Focus line.',
      cta: AI_HELL_CTA,
      changelog: '### Features\n- A',
    }).embeds[0].description;

    assert.ok(d.indexOf('Pitch line.') < d.indexOf('Focus line.'));
    assert.ok(d.indexOf('Focus line.') < d.indexOf('**AI Hell v1.2.3**'));
    assert.ok(d.indexOf('**AI Hell v1.2.3**') < d.indexOf(AI_HELL_CTA));
    assert.ok(d.indexOf(AI_HELL_CTA) < d.indexOf('### Features'));
  });

  it('preserves the markdown link syntax of the configured CTA verbatim', () => {
    const d = buildDiscordPayload({ version: '1.2.3', cta: AI_HELL_CTA, changelog: 'x' })
      .embeds[0].description;
    assert.ok(d.includes('[Play the alpha release](https://sorratheorc.github.io/AI_Hell/)'));
    assert.ok(d.includes(AI_HELL_CTA));
  });

  it('omits the CTA paragraph when absent, leaving the embed unchanged', () => {
    const base = buildDiscordPayload({
      version: '1.2.3', projectName: 'P', pitch: 'pitch', focus: 'focus', changelog: '### Features',
    }).embeds[0].description;
    const withNull = buildDiscordPayload({
      version: '1.2.3', projectName: 'P', pitch: 'pitch', focus: 'focus', changelog: '### Features', cta: null,
    }).embeds[0].description;

    assert.strictEqual(withNull, base);
    assert.ok(!base.includes('Play the alpha release'));
  });

  it('truncates the composed description including a long CTA to ≤ 4096 characters', () => {
    const d = buildDiscordPayload({
      version: '1.2.3', projectName: 'P',
      cta: 'cta '.repeat(2000),
      changelog: 'x'.repeat(9000),
    }).embeds[0].description;
    assert.ok(d.length <= 4096, 'composed description must respect the Discord limit');
    assert.ok(d.endsWith('…'), 'truncated description uses the ellipsis marker');
  });
});

describe('discord-notify: CTA notification integration (SA-0MUWCIFU2006K39K AC3/AC6)', () => {
  it('posts the AI_Hell-configured CTA in the composed embed description (AC6)', async () => {
    const dir = mkProjectDir();
    // Reproduces AI_Hell/.worklog/config.yaml verbatim.
    writeFileSync(
      join(dir, '.worklog', 'config.yaml'),
      `projectName: AI Hell\nprefix: AH\nautoSync: false\ncta: "${AI_HELL_CTA}"\n`,
    );
    writeFileSync(
      join(dir, 'CHANGELOG.md'),
      '# Changelog\n\n## v1.0.0 (2026-01-15)\n### Features\n- Added something\n',
    );
    // AI_Hell keeps its webhook outside the tracked project config; inject one
    // so the simulated release actually sends (the CTA still comes from the
    // reproduced project config above).
    const globalPath = join(dir, 'global-config.yaml');
    writeFileSync(globalPath, `discord:\n  webhook_url: ${WEBHOOK_URL}\n`);

    let body = null;
    const result = await sendReleaseNotification(
      { version: '1.0.0', projectRoot: dir },
      {
        fetchFn: async (_url, opts) => { body = JSON.parse(opts.body); return { ok: true }; },
        globalConfigPath: globalPath,
        readmePath: join(dir, 'none.md'),
      },
    );

    assert.strictEqual(result.notified, true);
    const d = body.embeds[0].description;
    assert.ok(d.includes('[Play the alpha release](https://sorratheorc.github.io/AI_Hell/)'));
    assert.ok(d.includes(AI_HELL_CTA), 'the full AI_Hell CTA is present verbatim');
    assert.ok(d.indexOf('**AI Hell v1.0.0**') < d.indexOf(AI_HELL_CTA));
    assert.ok(d.indexOf(AI_HELL_CTA) < d.indexOf('Added something'));
    rmTmpDir(dir);
  });

  it('still sends and omits the CTA when the config is malformed (non-blocking, AC3)', async () => {
    const dir = mkProjectDir();
    writeFileSync(
      join(dir, '.worklog', 'config.yaml'),
      `discord:\n  webhook_url: ${WEBHOOK_URL}\ncta:\n  nested: value\n`,
    );
    writeFileSync(join(dir, 'CHANGELOG.md'), COMPOSE_CHANGELOG);

    let body = null;
    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: dir },
      {
        fetchFn: async (_url, opts) => { body = JSON.parse(opts.body); return { ok: true }; },
        globalConfigPath: join(dir, 'none.yaml'),
        readmePath: join(dir, 'none.md'),
      },
    );

    assert.strictEqual(result.success, true);
    assert.strictEqual(result.notified, true);
    assert.ok(!body.embeds[0].description.includes('nested'));
    rmTmpDir(dir);
  });
});

describe('discord-notify: project description + pitch (SA-0MUVWL09T001N5P4)', () => {
  let savedKey;
  beforeEach(() => { savedKey = process.env.DEEPSEEK_API_KEY; });
  afterEach(() => {
    if (savedKey === undefined) delete process.env.DEEPSEEK_API_KEY;
    else process.env.DEEPSEEK_API_KEY = savedKey;
  });

  it('extractReadmePitch returns the leading prose before the first ## heading', () => {
    const readme = '# Project\n\nA great tool for testing.\n\n## Install\n\nnpm i\n';
    assert.strictEqual(extractReadmePitch(readme), '# Project\n\nA great tool for testing.');
  });

  it('extractReadmePitch caps the prose at ~1000 characters', () => {
    assert.strictEqual(extractReadmePitch('a'.repeat(5000)).length, 1000);
  });

  it('readProjectDescriptionFromConfig reads the top-level scalar', () => {
    const dir = mkTmpDir('discord-desc-');
    const path = join(dir, 'config.yaml');
    writeFileSync(path, 'projectDescription: A tool for testing.\n');
    assert.strictEqual(readProjectDescriptionFromConfig(path), 'A tool for testing.');
    assert.strictEqual(readProjectDescriptionFromConfig(join(dir, 'missing.yaml')), null);
    rmTmpDir(dir);
  });

  it('resolveProjectDescription uses the same precedence as projectName', () => {
    const dir = mkProjectDir();
    const globalDir = mkTmpDir('discord-global-');
    writeFileSync(join(globalDir, 'config.yaml'), 'projectDescription: global desc\n');
    const globalPath = join(globalDir, 'config.yaml');

    assert.strictEqual(resolveProjectDescription(dir, { globalConfigPath: globalPath }), 'global desc');

    writeFileSync(join(dir, '.worklog', 'config.yaml'), 'projectDescription: project desc\n');
    assert.strictEqual(resolveProjectDescription(dir, { globalConfigPath: globalPath }), 'project desc');

    writeFileSync(join(dir, '.worklog', 'config.private.yaml'), 'projectDescription: private desc\n');
    assert.strictEqual(resolveProjectDescription(dir, { globalConfigPath: globalPath }), 'private desc');
    rmTmpDir(dir);
    rmTmpDir(globalDir);
  });

  it('resolveProjectPitch prefers the configured description over README generation', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const dir = mkProjectDir();
    writeFileSync(join(dir, '.worklog', 'config.yaml'), 'projectDescription: A configured pitch.\n');
    const pitch = await resolveProjectPitch(dir, {
      globalConfigPath: join(dir, 'none.yaml'),
      readmeContent: '# X\n\nsome readme prose',
      llmFetchFn: stubLlmFetch('Should not be used.'),
    });
    assert.strictEqual(pitch, 'A configured pitch.');
    rmTmpDir(dir);
  });

  it('resolveProjectPitch generates the pitch from README prose when unset', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const dir = mkProjectDir();
    const pitch = await resolveProjectPitch(dir, {
      globalConfigPath: join(dir, 'none.yaml'),
      readmeContent: '# Project\n\nA tool for testing releases.',
      llmFetchFn: stubLlmFetch('Project is a tool for testing releases.'),
    });
    assert.strictEqual(pitch, 'Project is a tool for testing releases.');
    rmTmpDir(dir);
  });

  it('resolveProjectPitch clamps an over-long generated pitch to three sentences', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const dir = mkProjectDir();
    const pitch = await resolveProjectPitch(dir, {
      globalConfigPath: join(dir, 'none.yaml'),
      readmeContent: '# Project\n\nProse.',
      llmFetchFn: stubLlmFetch('One. Two. Three. Four.'),
    });
    assert.strictEqual(pitch, 'One. Two. Three.');
    rmTmpDir(dir);
  });

  it('resolveProjectPitch omits the pitch when generation is unavailable', async () => {
    delete process.env.DEEPSEEK_API_KEY;
    const dir = mkProjectDir();
    const pitch = await resolveProjectPitch(dir, {
      globalConfigPath: join(dir, 'none.yaml'),
      readmeContent: '# Project\n\nA tool.',
      llmFetchFn: stubLlmFetch('never'),
    });
    assert.strictEqual(pitch, null);
    rmTmpDir(dir);
  });

  it('resolveProjectPitch omits the pitch when there is no README prose', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const dir = mkProjectDir();
    const pitch = await resolveProjectPitch(dir, {
      globalConfigPath: join(dir, 'none.yaml'),
      readmePath: join(dir, 'missing.md'),
    });
    assert.strictEqual(pitch, null);
    rmTmpDir(dir);
  });

  it('generatePitch returns null for empty prose', async () => {
    assert.strictEqual(await generatePitch('', { fetchFn: stubLlmFetch('x') }), null);
  });
});

describe('discord-notify: release focus extraction (SA-0MUVWL09T001N5P4)', () => {
  it('extracts the marker and removes it from the body', () => {
    const text = '> **Release focus:** Co-op mode lands.\n### Features\n- A\n';
    const { focus, body } = extractReleaseFocus(text);
    assert.strictEqual(focus, 'Co-op mode lands.');
    assert.ok(!body.includes('Release focus'), 'marker must be removed from the body');
    assert.ok(body.includes('### Features'), 'the rest of the section is preserved');
  });

  it('returns null focus and an unchanged body when the marker is absent', () => {
    const { focus, body } = extractReleaseFocus('### Features\n- A\n');
    assert.strictEqual(focus, null);
    assert.strictEqual(body, '### Features\n- A');
  });
});

describe('discord-notify: ordered description composition (SA-0MUVWL09T001N5P4)', () => {
  it('orders paragraphs pitch → focus → project/version line → changelog', () => {
    const payload = buildDiscordPayload({
      version: '1.2.3', projectName: 'TestProject',
      pitch: 'Pitch line.', focus: 'Focus line.',
      changelog: '### Features\n- A',
    });
    const d = payload.embeds[0].description;
    assert.ok(d.indexOf('Pitch line.') < d.indexOf('Focus line.'));
    assert.ok(d.indexOf('Focus line.') < d.indexOf('**TestProject v1.2.3**'));
    assert.ok(d.indexOf('**TestProject v1.2.3**') < d.indexOf('### Features'));
  });

  it('omits pitch and focus paragraphs when absent', () => {
    const d = buildDiscordPayload({ version: '1.2.3', projectName: 'P', changelog: '### Features' })
      .embeds[0].description;
    assert.ok(d.startsWith('**P v1.2.3**'), 'no leading paragraphs when pitch/focus are absent');
  });

  it('truncates the composed description to ≤ 4096 characters', () => {
    const d = buildDiscordPayload({
      version: '1.2.3', projectName: 'P', pitch: 'pitch', focus: 'focus',
      changelog: 'x'.repeat(9000),
    }).embeds[0].description;
    assert.ok(d.length <= 4096, 'composed description must respect the Discord limit');
  });
});

describe('discord-notify: notification composition integration (SA-0MUVWL09T001N5P4)', () => {
  it('includes the configured pitch and extracts the focus from the changelog section', async () => {
    const dir = mkProjectDir();
    writeCompositionConfig(join(dir, '.worklog', 'config.yaml'), { description: 'A config pitch.' });
    writeFileSync(
      join(dir, 'CHANGELOG.md'),
      '# Changelog\n\n## v1.2.3 (2026-01-15)\n> **Release focus:** Co-op mode lands.\n### Features\n- Added something\n',
    );

    let body = null;
    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: dir },
      {
        fetchFn: async (_url, opts) => { body = JSON.parse(opts.body); return { ok: true }; },
        globalConfigPath: join(dir, 'none.yaml'),
        readmePath: join(dir, 'none.md'),
      },
    );

    assert.strictEqual(result.notified, true);
    const d = body.embeds[0].description;
    assert.ok(d.includes('A config pitch.'), 'configured pitch included');
    assert.ok(d.includes('Co-op mode lands.'), 'focus included');
    assert.ok(!d.includes('Release focus'), 'marker must not be duplicated');
    assert.ok(d.indexOf('A config pitch.') < d.indexOf('Co-op mode lands.'));
    assert.ok(d.indexOf('Co-op mode lands.') < d.indexOf('Added something'));
    rmTmpDir(dir);
  });

  it('omits pitch and focus when unavailable and still sends the changelog', async () => {
    const dir = mkProjectDir();
    writeCompositionConfig(join(dir, '.worklog', 'config.yaml'));
    writeFileSync(join(dir, 'CHANGELOG.md'), COMPOSE_CHANGELOG);

    let body = null;
    const result = await sendReleaseNotification(
      { version: '1.2.3', projectRoot: dir },
      {
        fetchFn: async (_url, opts) => { body = JSON.parse(opts.body); return { ok: true }; },
        globalConfigPath: join(dir, 'none.yaml'),
        readmePath: join(dir, 'none.md'),
      },
    );

    assert.strictEqual(result.notified, true);
    const d = body.embeds[0].description;
    assert.ok(d.includes('Added something (SA-ABC1)'));
    assert.ok(!d.includes('Release focus'));
    rmTmpDir(dir);
  });
});
