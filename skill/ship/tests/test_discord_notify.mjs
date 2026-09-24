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
  resolveDiscordWebhookUrl,
  extractChangelogSection,
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
    });
    assert.ok(Array.isArray(payload.embeds));
    assert.strictEqual(payload.embeds.length, 1);
    const embed = payload.embeds[0];
    assert.strictEqual(embed.title, 'Release v1.2.3');
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
    const payload = buildDiscordPayload({});
    assert.strictEqual(payload.embeds[0].title, 'Release vunknown');
    assert.strictEqual(payload.embeds[0].fields[0].value, 'unknown');
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
    assert.strictEqual(payload.embeds[0].title, 'Release v1.2.3');
    assert.ok(payload.embeds[0].description.includes('New feature'));
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
