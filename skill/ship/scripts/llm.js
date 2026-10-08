#!/usr/bin/env node

/**
 * llm.js — shared LLM chat-completion caller for the ship skill.
 *
 * Extracted from generate-changelog.js (SA-0MUVV6DK0002ISW4) so that other
 * release-time modules (e.g. discord-notify.js) can reuse the exact same
 * DeepSeek call without importing generate-changelog.js — that module runs
 * `git rev-parse` at import time, which would make it awkward to import from
 * a notification helper.
 *
 * The caller talks to the DeepSeek API (OpenAI-compatible) using the
 * `DEEPSEEK_API_KEY` environment variable. It always degrades gracefully:
 * when the key is absent, the request fails, or the response is malformed it
 * returns `null`, letting callers fall back to their non-LLM behaviour. The
 * release path is therefore never blocked by an LLM failure.
 *
 * No runtime dependencies beyond Node.js 18+ built-ins (`fetch`,
 * `AbortSignal`).
 */

/**
 * Shared LLM chat-completion caller (DeepSeek, OpenAI-compatible API).
 *
 * Returns the assistant message content, or null when no API key is
 * configured or the call fails. Callers fall back to their non-LLM
 * behaviour in that case, so the release stays backward-compatible when no
 * key is present.
 *
 * @param {Array<{role:string, content:string}>} messages - Chat messages.
 * @param {{maxTokens?:number, temperature?:number, fetchFn?:Function}} opts
 *   Optional overrides. `fetchFn` is an injectable fetch boundary (defaults
 *   to the global `fetch`) used by unit tests to avoid live network calls.
 * @returns {Promise<string|null>} Assistant content, or null on fallback.
 */
export async function callLlm(messages, opts = {}) {
  const apiKey = process.env.DEEPSEEK_API_KEY;
  if (!apiKey) return null;

  const fetchFn = opts.fetchFn ?? fetch;

  try {
    const response = await fetchFn('https://api.deepseek.com/chat/completions', {
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
