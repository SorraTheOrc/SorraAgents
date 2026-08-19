/**
 * Unit tests for skill/ship/scripts/discord-notify.js
 *
 * Covers AC1–AC6 of SA-0MSQ6K7Z1002H14Z:
 *  - AC1/AC4: changelog extraction, embed payload shape, 4096-char truncation
 *  - AC2: config precedence (project → global) + skip-when-unset
 *  - AC3: non-blocking failure behaviour (release exit code unchanged)
 *  - run-release.js hook placement (post merge-verification, never on dry-run)
 *  - AC6: SKILL.md / reference docs document the feature
 *
 * All config reads use injected temp paths and every send uses an injected
 * fetchFn — the suite never touches the live home config or the network.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const REPO_ROOT = join(dirname(__filename), '..', '..');
const DISCORD_NOTIFY_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'discord-notify.js');
const RUN_RELEASE_PATH = join(REPO_ROOT, 'skill', 'ship', 'scripts', 'run-release.js');
const SKILL_MD_PATH = join(REPO_ROOT, 'skill', 'ship', 'SKILL.md');
const REFERENCE_PATH = join(REPO_ROOT, 'docs', 'dev', 'ship-skill-reference.md');

const WEBHOOK_PROJECT = 'https://discord.com/api/webhooks/PROJECT/token';
const WEBHOOK_GLOBAL = 'https://discord.com/api/webhooks/GLOBAL/token';

const SAMPLE_CHANGELOG = `# Changelog

## v1.2.3 (2026-01-15)

### Features

- Added something (SA-ABC1)

### Bug Fixes

- Fixed a bug (SA-ABC2)

## v1.2.2 (2025-12-20)

### Features

- Older feature (SA-OLD)
`;

/** Create a temp project dir with a .worklog/ folder. */
function makeTempProject() {
  const dir = mkdtempSync(join(tmpdir(), 'discord-notify-test-'));
  mkdirSync(join(dir, '.worklog'), { recursive: true });
  return dir;
}

/** Write a config file with a discord.webhook_url at a given path. */
function writeWebhookConfig(path, webhookUrl) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(
    path,
    `projectName: Test Project\nprefix: TP\ndiscord:\n  webhook_url: ${webhookUrl}\n`,
  );
}

// ---------------------------------------------------------------------------
// Module shape
// ---------------------------------------------------------------------------
describe('discord-notify: module exports', () => {
  test('discord-notify.js exists and exports the expected functions', async () => {
    assert.ok(
      existsSync(DISCORD_NOTIFY_PATH),
      'skill/ship/scripts/discord-notify.js should exist',
    );
    const mod = await import(DISCORD_NOTIFY_PATH);
    for (const fn of [
      'resolveDiscordWebhookUrl',
      'extractChangelogSection',
      'truncateForDiscord',
      'buildDiscordPayload',
      'sendReleaseNotification',
    ]) {
      assert.equal(typeof mod[fn], 'function', `discord-notify.js should export ${fn}`);
    }
  });
});

// ---------------------------------------------------------------------------
// AC2 — config precedence (project → global) and skip-when-unset
// ---------------------------------------------------------------------------
describe('discord-notify: config resolution (AC2)', () => {
  test('project config takes precedence over the global fallback', async () => {
    const dir = makeTempProject();
    writeWebhookConfig(join(dir, '.worklog', 'config.yaml'), WEBHOOK_PROJECT);
    const globalDir = makeTempProject();
    writeWebhookConfig(join(globalDir, 'config.yaml'), WEBHOOK_GLOBAL);

    const mod = await import(DISCORD_NOTIFY_PATH);
    const url = mod.resolveDiscordWebhookUrl(dir, {
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.equal(url, WEBHOOK_PROJECT);
  });

  test('falls back to the global config when the project config has no discord.webhook_url', async () => {
    const dir = makeTempProject();
    writeFileSync(join(dir, '.worklog', 'config.yaml'), 'projectName: Test\nprefix: TP\n');
    const globalDir = makeTempProject();
    writeWebhookConfig(join(globalDir, 'config.yaml'), WEBHOOK_GLOBAL);

    const mod = await import(DISCORD_NOTIFY_PATH);
    const url = mod.resolveDiscordWebhookUrl(dir, {
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.equal(url, WEBHOOK_GLOBAL);
  });

  test('returns null when neither config sets discord.webhook_url', async () => {
    const dir = makeTempProject();
    writeFileSync(join(dir, '.worklog', 'config.yaml'), 'projectName: Test\nprefix: TP\n');
    const globalDir = makeTempProject();
    writeFileSync(join(globalDir, 'config.yaml'), 'someOtherKey: value\n');

    const mod = await import(DISCORD_NOTIFY_PATH);
    const url = mod.resolveDiscordWebhookUrl(dir, {
      globalConfigPath: join(globalDir, 'config.yaml'),
    });
    assert.equal(url, null);
  });

  test('tolerates missing config files (returns null)', async () => {
    const dir = makeTempProject();
    const mod = await import(DISCORD_NOTIFY_PATH);
    const url = mod.resolveDiscordWebhookUrl(dir, {
      globalConfigPath: join(dir, 'no-such-config.yaml'),
    });
    assert.equal(url, null);
  });
});

// ---------------------------------------------------------------------------
// AC1 — changelog section extraction
// ---------------------------------------------------------------------------
describe('discord-notify: changelog extraction (AC1)', () => {
  test('extracts the requested version section with its date, stopping at the next section', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    const section = mod.extractChangelogSection(SAMPLE_CHANGELOG, '1.2.3');

    assert.ok(section, 'the v1.2.3 section should be found');
    assert.equal(section.date, '2026-01-15');
    assert.ok(section.text.includes('Added something (SA-ABC1)'));
    assert.ok(section.text.includes('Fixed a bug (SA-ABC2)'));
    assert.ok(
      !section.text.includes('Older feature'),
      'the section must not bleed into the next release section',
    );
  });

  test('returns null when the version section is absent', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    assert.equal(mod.extractChangelogSection(SAMPLE_CHANGELOG, '9.9.9'), null);
  });

  test('handles an empty changelog gracefully', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    assert.equal(mod.extractChangelogSection('', '1.2.3'), null);
  });
});

// ---------------------------------------------------------------------------
// AC4 — embed description truncation (Discord 4096-char limit)
// ---------------------------------------------------------------------------
describe('discord-notify: description truncation (AC4)', () => {
  test('leaves short text unchanged', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    const text = '### Features\n- Short changelog entry';
    assert.equal(mod.truncateForDiscord(text), text);
  });

  test('truncates over-long text to ≤ 4096 chars with an ellipsis marker', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    const long = 'x'.repeat(5000);
    const truncated = mod.truncateForDiscord(long);

    assert.ok(truncated.length <= 4096, 'truncated text must respect the Discord limit');
    assert.ok(
      truncated.includes('…') || truncated.includes('...'),
      'truncation should leave an ellipsis marker',
    );
    assert.ok(!truncated.includes('x'.repeat(4097)), 'over-long tail must be removed');
  });
});

// ---------------------------------------------------------------------------
// AC1 — embed payload shape
// ---------------------------------------------------------------------------
describe('discord-notify: embed payload shape (AC1)', () => {
  test('buildDiscordPayload includes version, tag, date, PR URL and a bounded description', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    const longChangelog = '### Features\n- entry\n'.repeat(800); // well over 4096 chars

    const payload = mod.buildDiscordPayload({
      version: '1.2.3',
      tag: 'v1.2.3',
      date: '2026-01-15',
      prUrl: 'https://github.com/org/repo/pull/42',
      changelog: longChangelog,
    });

    assert.ok(Array.isArray(payload.embeds) && payload.embeds.length === 1);
    const embed = payload.embeds[0];
    assert.equal(embed.title, 'Release v1.2.3');
    assert.ok(
      embed.description.length <= 4096,
      'embed description must respect Discord limits',
    );

    const fields = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
    assert.equal(fields.Version, '1.2.3');
    assert.equal(fields.Tag, 'v1.2.3');
    assert.equal(fields.Date, '2026-01-15');
    assert.equal(fields['Pull Request'], 'https://github.com/org/repo/pull/42');
  });

  test('buildDiscordPayload tolerates missing inputs without throwing', async () => {
    const mod = await import(DISCORD_NOTIFY_PATH);
    const payload = mod.buildDiscordPayload({});
    const embed = payload.embeds[0];
    assert.ok(embed.title, 'payload should still have a title');
    assert.ok(embed.description, 'payload should still have a description');
  });
});

// ---------------------------------------------------------------------------
// AC3 — non-blocking notification (release exit code unchanged)
// ---------------------------------------------------------------------------
describe('discord-notify: non-blocking notification (AC3)', () => {
  test('skips without calling fetch when no webhook is configured (AC2)', async () => {
    const dir = makeTempProject();
    writeFileSync(join(dir, '.worklog', 'config.yaml'), 'projectName: Test\nprefix: TP\n');
    const mod = await import(DISCORD_NOTIFY_PATH);

    let fetchCalls = 0;
    const result = await mod.sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/o/r/pull/1', projectRoot: dir },
      {
        globalConfigPath: join(dir, 'no-global.yaml'),
        fetchFn: async () => { fetchCalls += 1; return { ok: true }; },
      },
    );

    assert.equal(result.success, true, 'a skipped notification is not a failure');
    assert.equal(result.notified, false);
    assert.equal(result.skipped, true);
    assert.equal(fetchCalls, 0, 'fetch must not be called when no webhook is configured');
  });

  test('posts the payload to the resolved webhook and reports success', async () => {
    const dir = makeTempProject();
    writeWebhookConfig(join(dir, '.worklog', 'config.yaml'), WEBHOOK_PROJECT);
    writeFileSync(join(dir, 'CHANGELOG.md'), SAMPLE_CHANGELOG);
    const mod = await import(DISCORD_NOTIFY_PATH);

    let postedUrl = null;
    let body = null;
    const fetchFn = async (url, opts) => {
      postedUrl = url;
      body = JSON.parse(opts.body);
      return { ok: true, status: 204, statusText: 'No Content' };
    };

    const result = await mod.sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/o/r/pull/1', projectRoot: dir },
      { fetchFn },
    );

    assert.equal(result.success, true);
    assert.equal(result.notified, true);
    assert.equal(postedUrl, WEBHOOK_PROJECT);
    assert.equal(body.embeds[0].title, 'Release v1.2.3');
    assert.ok(
      body.embeds[0].description.includes('Added something (SA-ABC1)'),
      'the posted changelog should be the released version section',
    );
  });

  test('a rejected fetch is a logged warning, not a release failure', async () => {
    const dir = makeTempProject();
    writeWebhookConfig(join(dir, '.worklog', 'config.yaml'), WEBHOOK_PROJECT);
    const mod = await import(DISCORD_NOTIFY_PATH);

    const result = await mod.sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/o/r/pull/1', projectRoot: dir },
      { fetchFn: async () => { throw new Error('network down'); } },
    );

    assert.equal(result.success, true, 'a notification failure must never fail the release');
    assert.equal(result.notified, false);
    assert.ok(result.error.includes('network down'));
  });

  test('an HTTP error status is a logged warning, not a release failure', async () => {
    const dir = makeTempProject();
    writeWebhookConfig(join(dir, '.worklog', 'config.yaml'), WEBHOOK_PROJECT);
    const mod = await import(DISCORD_NOTIFY_PATH);

    const result = await mod.sendReleaseNotification(
      { version: '1.2.3', prUrl: null, projectRoot: dir },
      { fetchFn: async () => ({ ok: false, status: 429, statusText: 'Too Many Requests' }) },
    );

    assert.equal(result.success, true);
    assert.equal(result.notified, false);
    assert.ok(result.error.includes('429'));
  });

  test('falls back to the current date and sends without a changelog when the section is missing', async () => {
    const dir = makeTempProject();
    writeWebhookConfig(join(dir, '.worklog', 'config.yaml'), WEBHOOK_PROJECT);
    writeFileSync(
      join(dir, 'CHANGELOG.md'),
      '# Changelog\n\n## v9.9.9 (2030-01-01)\n\n### Features\n- other\n',
    );
    const mod = await import(DISCORD_NOTIFY_PATH);

    let body = null;
    const result = await mod.sendReleaseNotification(
      { version: '1.2.3', prUrl: 'https://github.com/o/r/pull/9', projectRoot: dir },
      {
        fetchFn: async (url, opts) => { body = JSON.parse(opts.body); return { ok: true }; },
        now: () => new Date('2026-02-02T00:00:00Z'),
      },
    );

    assert.equal(result.notified, true);
    const dateField = body.embeds[0].fields.find((f) => f.name === 'Date');
    assert.equal(dateField.value, '2026-02-02');
  });
});

// ---------------------------------------------------------------------------
// Hook placement in run-release.js — post-verification, never on dry-run
// ---------------------------------------------------------------------------
describe('discord-notify: run-release.js hook', () => {
  test('run-release.js imports sendReleaseNotification from discord-notify.js', () => {
    const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
    assert.ok(
      content.includes("import { sendReleaseNotification } from './discord-notify.js'"),
      'run-release.js should import sendReleaseNotification from discord-notify.js',
    );
  });

  test('the notification call site sits after merge verification succeeds', () => {
    const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
    const verifyIdx = content.indexOf('const mergeVerification = verifyReleaseMerge(version)');
    const notifyIdx = content.indexOf('sendReleaseNotification(');
    assert.ok(verifyIdx >= 0, 'run-release.js should call verifyReleaseMerge');
    assert.ok(
      notifyIdx > verifyIdx,
      'the notification must run only after the release merge is verified',
    );
  });

  test('the notification call site sits after the dry-run early return', () => {
    const content = readFileSync(RUN_RELEASE_PATH, 'utf-8');
    const dryRunReturnIdx = content.indexOf('Dry-run complete');
    const notifyIdx = content.indexOf('sendReleaseNotification(');
    assert.ok(dryRunReturnIdx >= 0, 'run-release.js should have a dry-run early return');
    assert.ok(
      notifyIdx > dryRunReturnIdx,
      'a dry-run must never trigger a notification',
    );
  });
});

// ---------------------------------------------------------------------------
// AC6 — documentation
// ---------------------------------------------------------------------------
describe('discord-notify: documentation (AC6)', () => {
  test('SKILL.md documents the Discord notification and its config schema', () => {
    const content = readFileSync(SKILL_MD_PATH, 'utf-8');
    assert.ok(
      content.includes('discord.webhook_url'),
      'SKILL.md should document the discord.webhook_url config key',
    );
    assert.ok(
      /discord/i.test(content),
      'SKILL.md should mention Discord',
    );
    assert.ok(
      content.includes('non-blocking'),
      'SKILL.md should document the non-blocking notification semantics',
    );
  });

  test('ship-skill-reference.md documents the module, hook point and config schema', () => {
    const content = readFileSync(REFERENCE_PATH, 'utf-8');
    assert.ok(
      content.includes('discord-notify.js'),
      'the reference doc should list the new discord-notify.js module',
    );
    assert.ok(
      content.includes('discord.webhook_url'),
      'the reference doc should document the discord.webhook_url config key',
    );
  });
});
