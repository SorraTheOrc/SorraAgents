/**
 * Tests for llm.js (SA-0MUVV6DK0002ISW4) — mirrored copy of
 * tests/unit/test-llm.mjs under the ship skill's own test directory.
 *
 * Verifies the extracted shared LLM caller contract:
 *   - null when DEEPSEEK_API_KEY is absent (no fetch attempted)
 *   - null on HTTP error and on network rejection
 *   - assistant content on the success path
 *   - request shape (endpoint, auth, model, defaults, opts overrides)
 *
 * The network boundary is injected via opts.fetchFn, so the suite never
 * touches the live DeepSeek API.
 */

import { describe, test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const SCRIPT_DIR = join(dirname(__filename), '..', 'scripts');
const MODULE_PATH = join(SCRIPT_DIR, 'llm.js');

/** Save/restore DEEPSEEK_API_KEY around each test. */
let savedKey;
beforeEach(() => { savedKey = process.env.DEEPSEEK_API_KEY; });
afterEach(() => {
  if (savedKey === undefined) delete process.env.DEEPSEEK_API_KEY;
  else process.env.DEEPSEEK_API_KEY = savedKey;
});

describe('callLlm - graceful fallback', () => {
  test('returns null and does not call fetch when the API key is absent', async () => {
    delete process.env.DEEPSEEK_API_KEY;
    let fetchCalls = 0;
    const { callLlm } = await import(MODULE_PATH);

    const result = await callLlm([{ role: 'user', content: 'hi' }], {
      fetchFn: async () => { fetchCalls += 1; return { ok: true, json: async () => ({}) }; },
    });

    assert.equal(result, null, 'missing key should yield null');
    assert.equal(fetchCalls, 0, 'fetch must not be attempted without a key');
  });

  test('returns null on an HTTP error response', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { callLlm } = await import(MODULE_PATH);

    const result = await callLlm([{ role: 'user', content: 'hi' }], {
      fetchFn: async () => ({ ok: false, status: 429, statusText: 'Too Many Requests' }),
    });

    assert.equal(result, null, 'non-ok response should yield null');
  });

  test('returns null when the network call rejects', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { callLlm } = await import(MODULE_PATH);

    const result = await callLlm([{ role: 'user', content: 'hi' }], {
      fetchFn: async () => { throw new Error('network down'); },
    });

    assert.equal(result, null, 'rejected fetch should yield null');
  });

  test('returns null when the response body has no choices', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { callLlm } = await import(MODULE_PATH);

    const result = await callLlm([{ role: 'user', content: 'hi' }], {
      fetchFn: async () => ({ ok: true, json: async () => ({}) }),
    });

    assert.equal(result, null, 'malformed response should yield null');
  });
});

describe('callLlm - success path and request shape', () => {
  test('returns the assistant message content', async () => {
    process.env.DEEPSEEK_API_KEY = 'test-key';
    const { callLlm } = await import(MODULE_PATH);

    const result = await callLlm([{ role: 'user', content: 'hi' }], {
      fetchFn: async () => ({
        ok: true,
        json: async () => ({ choices: [{ message: { content: 'Hello there' } }] }),
      }),
    });

    assert.equal(result, 'Hello there', 'should return the raw assistant content');
  });

  test('POSTs to the DeepSeek endpoint with auth, model and default sampling opts', async () => {
    process.env.DEEPSEEK_API_KEY = 'secret-key';
    const { callLlm } = await import(MODULE_PATH);
    const messages = [{ role: 'system', content: 'sys' }, { role: 'user', content: 'hi' }];

    let captured;
    const result = await callLlm(messages, {
      fetchFn: async (url, options) => {
        captured = { url, options };
        return { ok: true, json: async () => ({ choices: [{ message: { content: 'ok' } }] }) };
      },
    });

    assert.equal(result, 'ok');
    assert.equal(captured.url, 'https://api.deepseek.com/chat/completions');
    assert.equal(captured.options.method, 'POST');
    assert.equal(captured.options.headers.Authorization, 'Bearer secret-key');
    assert.equal(captured.options.headers['Content-Type'], 'application/json');

    const body = JSON.parse(captured.options.body);
    assert.equal(body.model, 'deepseek-chat');
    assert.deepEqual(body.messages, messages, 'messages should be passed through unchanged');
    assert.equal(body.max_tokens, 120, 'default max_tokens should be 120');
    assert.equal(body.temperature, 0.3, 'default temperature should be 0.3');
  });

  test('honours maxTokens and temperature overrides', async () => {
    process.env.DEEPSEEK_API_KEY = 'secret-key';
    const { callLlm } = await import(MODULE_PATH);

    let body;
    await callLlm([{ role: 'user', content: 'hi' }], {
      maxTokens: 10,
      temperature: 0.1,
      fetchFn: async (_url, options) => {
        body = JSON.parse(options.body);
        return { ok: true, json: async () => ({ choices: [{ message: { content: 'ok' } }] }) };
      },
    });

    assert.equal(body.max_tokens, 10);
    assert.equal(body.temperature, 0.1);
  });
});
