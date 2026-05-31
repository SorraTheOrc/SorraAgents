// Simple runtime test for the Ralph compaction hook.
// This script imports the plugin factory and invokes the
// `experimental.session.compacting` hook with a fake context and
// verifies that output.prompt is set when no overrides match.

import assert from 'assert';
import { createRalphPlugin } from '../ralph.js';

async function run() {
  // Create a fake ctx with a minimal client implementing session.messages
  // and app.log used by safeLog.
  const fakeMessages = [
    { info: { role: 'system' }, parts: [{ text: 'system init' }] },
    { info: { role: 'user' }, parts: [{ text: 'This is the original user prompt that must be preserved.' }] },
  ];

  const ctx = {
    client: {
      session: {
        messages: async ({ path, query }) => ({ data: fakeMessages }),
      },
      app: {
        log: async (body) => {
          // No-op for tests, but print to stdout for visibility
          console.log('[fake app.log]', JSON.stringify(body || body === undefined ? body : null));
        },
      },
    },
  };

  const RalphPlugin = await createRalphPlugin()(ctx, {});
  const hook = RalphPlugin['experimental.session.compacting'];
  assert.strictEqual(typeof hook, 'function', 'compacting hook should be a function');

  const input = { sessionID: 'fake-session-1' };
  const output = { context: [] };

  await hook(input, output);

  // When no override matches, the original prompt should be appended to output.context
  assert.ok(
    Array.isArray(output.context) &&
    output.context.some(c => typeof c === 'string' && c.includes('This is the original user prompt')),
    'output.context must contain the original user prompt when no override matches'
  );

  console.log('Ralph compaction test: OK');
}

if (import.meta.url === `file://${process.argv[1]}`) {
  run().catch(err => {
    console.error('Ralph compaction test: FAILED', err);
    process.exit(1);
  });
}

export default run;
