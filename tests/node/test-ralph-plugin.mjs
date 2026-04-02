import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createRalphPlugin } from '../../.opencode/plugins/ralph.js';

function buildCtx(messagesResult) {
  return {
    client: {
      session: {
        messages: async () => messagesResult,
      },
    },
  };
}

function userTextMessage(text, created = 1) {
  return {
    info: {
      id: `u-${created}`,
      sessionID: 'session-1',
      role: 'user',
      time: { created },
      agent: 'build',
      model: { providerID: 'x', modelID: 'y' },
    },
    parts: [
      {
        id: `p-${created}`,
        sessionID: 'session-1',
        messageID: `u-${created}`,
        type: 'text',
        text,
      },
    ],
  };
}

test('ralph plugin applies override template when pattern matches', async () => {
  const messages = {
    data: [userTextMessage('implement SA-123')],
    error: undefined,
  };

  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx(messages), {
    overrides: [
      {
        pattern: '^implement (\\S+)$',
        template: 'audit {1} and address any issues the audit identifies',
      },
    ],
  });

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.ok(typeof output.prompt === 'string', 'expected prompt override');
  assert.ok(
    output.prompt.startsWith('audit SA-123 and address any issues the audit identifies'),
    `unexpected prompt: ${output.prompt}`,
  );
  assert.ok(output.prompt.includes('Original user prompt: implement SA-123'));
  assert.deepEqual(output.context, []);
});

test('ralph plugin appends original prompt to context when no override matches', async () => {
  const messages = {
    data: [userTextMessage('investigate scheduler timeout')],
    error: undefined,
  };

  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx(messages), {
    overrides: [
      {
        pattern: '^implement (\\S+)$',
        template: 'audit {1} and address any issues the audit identifies',
      },
    ],
  });

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.equal(output.prompt, undefined);
  assert.deepEqual(output.context, ['Original user prompt: investigate scheduler timeout']);
});

test('ralph plugin returns safe defaults on malformed input', async () => {
  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx({ data: undefined, error: { message: 'fail' } }), {
    overrides: [
      {
        pattern: '([',
        template: 'broken regex ignored',
      },
      {
        pattern: '^implement (\\S+)$',
      },
      null,
    ],
  });

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.equal(output.prompt, undefined);
  assert.deepEqual(output.context, []);
});

test('ralph plugin integration: original prompt selected from earliest user message', async () => {
  const messages = {
    data: [
      userTextMessage('follow-up note', 3),
      userTextMessage('implement SA-777', 1),
      {
        info: {
          id: 'a-2',
          sessionID: 'session-1',
          role: 'assistant',
          time: { created: 2 },
          parentID: 'u-1',
          modelID: 'm',
          providerID: 'p',
          mode: 'chat',
          path: { cwd: '/tmp', root: '/tmp' },
          cost: 0,
          tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        },
        parts: [],
      },
    ],
    error: undefined,
  };

  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx(messages), {
    overrides: [
      {
        pattern: '^implement (\\S+)$',
        template: 'audit {1} and address any issues the audit identifies',
      },
    ],
  });

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.ok(
    output.prompt.startsWith('audit SA-777 and address any issues the audit identifies'),
    `unexpected prompt: ${output.prompt}`,
  );
  assert.ok(output.prompt.includes('Original user prompt: implement SA-777'));
});

test('ralph plugin applies default implement override without explicit config', async () => {
  const messages = {
    data: [userTextMessage('implement SA-888')],
    error: undefined,
  };

  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx(messages));

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.ok(
    output.prompt.startsWith('audit SA-888 and address any issues the audit identifies'),
    `unexpected prompt: ${output.prompt}`,
  );
});

test('ralph plugin supports disableDefaultOverrides option', async () => {
  const messages = {
    data: [userTextMessage('implement SA-999')],
    error: undefined,
  };

  const pluginFactory = createRalphPlugin();
  const hooks = await pluginFactory(buildCtx(messages), { disableDefaultOverrides: true });

  const output = { context: [] };
  await hooks['experimental.session.compacting']({ sessionID: 'session-1' }, output);

  assert.equal(output.prompt, undefined);
  assert.deepEqual(output.context, ['Original user prompt: implement SA-999']);
});
