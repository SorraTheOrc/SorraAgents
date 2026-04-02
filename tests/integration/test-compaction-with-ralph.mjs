import { test } from 'node:test';
import assert from 'node:assert/strict';

import RalphPlugin from '../../.opencode/plugins/ralph.js';

function makeUserMessage(text, created = 1) {
  return {
    info: {
      id: `u-${created}`,
      sessionID: 'session-integration',
      role: 'user',
      time: { created },
      agent: 'build',
      model: { providerID: 'provider', modelID: 'model' },
    },
    parts: [
      {
        id: `p-${created}`,
        sessionID: 'session-integration',
        messageID: `u-${created}`,
        type: 'text',
        text,
      },
    ],
  };
}

test('compaction integration includes plugin-provided prompt for implement sessions', async () => {
  const ctx = {
    client: {
      session: {
        messages: async () => ({
          data: [
            makeUserMessage('implement SA-42', 1),
            makeUserMessage('additional context', 3),
          ],
          error: undefined,
        }),
      },
    },
  };

  const hooks = await RalphPlugin(ctx);
  assert.equal(typeof hooks['experimental.session.compacting'], 'function');

  const output = { context: [] };
  await hooks['experimental.session.compacting'](
    { sessionID: 'session-integration' },
    output,
  );

  assert.ok(typeof output.prompt === 'string', 'expected plugin to provide prompt override');
  assert.ok(
    output.prompt.startsWith('audit SA-42 and address any issues the audit identifies'),
    `unexpected prompt: ${output.prompt}`,
  );
});

test('compaction integration includes plugin-provided context when no override applies', async () => {
  const ctx = {
    client: {
      session: {
        messages: async () => ({
          data: [makeUserMessage('triage flaky scheduler test', 1)],
          error: undefined,
        }),
      },
    },
  };

  const hooks = await RalphPlugin(ctx);
  const output = { context: [] };
  await hooks['experimental.session.compacting'](
    { sessionID: 'session-integration' },
    output,
  );

  assert.equal(output.prompt, undefined);
  assert.deepEqual(output.context, ['Original user prompt: triage flaky scheduler test']);
});
