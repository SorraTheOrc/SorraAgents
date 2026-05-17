// Ralph plugin.
//
// The Ralph plugin focuses on ensuring that an agent can continue work
// for long periods of time. This is an experimental plugin that will
// likely evolve or be dropped in the future, depending on how effective
// it proves to be in practice.
//
// Experimenatl Features:
// - Enhance the compaction process in order to augment the resulting prompt/context with worklog information.
//

const DEFAULT_OVERRIDES = [
  {
    // Match any capitalization of: implement <work-item-id> ...
    pattern: '^implement\\s+(\\S+)[\\s\\S]*$',
    // Convert compaction prompt into an audit-driven follow-up.
    template: 'audit {1} and address any issues the audit identifies',
    mode: 'replace',
    flags: 'i',
  },
];

/**
 * Compile override specs into runtime objects.
 * 
 * Each input item should be an object with 
 * - `pattern` compiled into a RegExp and tested against the original user prompt.
 * - `template` used to render the new prompt/context if the regex matches, with `{0}`, `{1}`, ... placeholders for capture groups.
 * - `flags` [optional] string of RegExp flags, e.g. 'i' for case-insensitive matching.
 * - `mode` [optional] defaults to `'replace'` and may be set to `'prepend'`.
 * 
 * If a `pattern` match is found, the `template` is rendered by replacing `{0}`, `{1}`, ... with
 * the corresponding capture groups from the regex match. The rendered string is then
 * either used to replace the original prompt (mode: 'replace') or prepended into the
 * context (mode: 'prepend').
 * 
 * Returns an array of objects: `{ regex: RegExp, template: string, mode: string }`.
 *
 * @param {Array<Object>} raw - Raw override specifications.
 * @returns {Array<{regex:RegExp,template:string,mode:string}>}
 */
function compileOverrides(raw = []) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const pattern = typeof item.pattern === 'string' ? item.pattern : null;
    const template = typeof item.template === 'string' ? item.template : '';
    const flags = typeof item.flags === 'string' ? item.flags : '';
    const mode = item.mode === 'prepend' ? 'prepend' : 'replace';
    if (!pattern || !template) continue;
    try {
      out.push({ regex: new RegExp(pattern, flags), template, mode });
    } catch (e) {
      // skip invalid regex
      continue;
    }
  }
  return out;
}

/**
 * Render a template string by replacing `{0}`, `{1}`, ... with capture groups
 * from a RegExp match array.
 *
 * @param {string} template - Template containing `{n}` placeholders.
 * @param {Array<string>} match - RegExp match array (group 0..n).
 * @returns {string} Rendered string with placeholders replaced.
 */
function renderTemplate(template, match) {
  return template.replace(/\{(\d+)\}/g, (_m, idx) => (match && match[Number(idx)] ? match[Number(idx)] : '')).trim();
}

/**
 * Extract the first user prompt text from an array of session messages.
 * Searches messages in chronological order and returns the first message
 * where `info.role === 'user'` and any `parts[].text` contains non-empty text.
 *
 * @param {Array<Object>} messages - Session messages from the API.
 * @returns {string} The first user prompt, or empty string if none found.
 */
function extractUserPrompt(messages) {
  if (!Array.isArray(messages)) return '';
  // Sort by timestamp to pick the chronologically earliest user message with text.
  const sorted = messages
    .filter((e) => e && e.info && e.info.role === 'user')
    .sort((a, b) => {
      const ta = a.info.time && typeof a.info.time.created === 'number' ? a.info.time.created : 0;
      const tb = b.info.time && typeof b.info.time.created === 'number' ? b.info.time.created : 0;
      return ta - tb;
    });
  for (const entry of sorted) {
    const parts = Array.isArray(entry.parts) ? entry.parts : [];
    const text = parts.map(p => (p && typeof p.text === 'string' ? p.text : '')).join('').trim();
    if (text) return text;
  }
  return '';
}

/**
 * Safe logging helper. Calls `ctx.client.app.log` when available,
 * otherwise falls back to `console.log` so diagnostics are visible
 * during development.
 *
 * @param {Object} ctx - Plugin context provided when the plugin runs.
 * @param {...any} args - Values to log.
 */
function safeLog(ctx, ...args) {
  try {
    if (ctx && ctx.client && ctx.client.app && typeof ctx.client.app.log === 'function') {
      ctx.client.app.log(...args);
      return;
    }
  } catch (e) {
    // ignore errors from client logging
  }

  if (typeof console !== 'undefined' && typeof console.log === 'function') {
    try {
      console.log('[RalphPlugin]', ...args);
    } catch (e) {
      // swallow
    }
  }
}

/**
 * Create a Ralph plugin factory. Registering with appropriate hooks.
 *
 * @param {Object} deps - Optional dependency injection object.
 * @param {Function} [deps.getMessages] - Optional custom message fetcher: ({sessionID,ctx}) => Promise<Array>.
 * @param {Object} [deps.options] - Default options to merge into plugin options.
 * @returns {Function} Async plugin factory: `async function RalphPlugin(ctx, options)`.
 */
export function createRalphPlugin(deps = {}) {
  const injectedGetMessages = typeof deps.getMessages === 'function' ? deps.getMessages : null;
  const defaultOptions = deps && typeof deps.options === 'object' ? deps.options : {};

  return async function RalphPlugin(ctx, options = {}) {
    const opts = { ...defaultOptions, ...(options && typeof options === 'object' ? options : {}) };
    const builtinOverrides = opts.disableDefaultOverrides ? [] : DEFAULT_OVERRIDES;
    const configuredOverrides = Array.isArray(opts.overrides) ? opts.overrides : [];
    const overrides = compileOverrides([...builtinOverrides, ...configuredOverrides]);
    // Log plugin initialization and the number of compiled overrides.
    safeLog(ctx, 'RalphPlugin: initialized', { overridesCount: overrides.length });

    /**
     * Retrieve session messages for a given session ID.
     * Will call the injected `getMessages` if provided, otherwise uses `ctx.client.session.messages`.
     *
     * @param {string} sessionID - Session identifier.
     * @returns {Promise<Array<Object>>} Array of session messages.
     */
    async function getMessages(sessionID) {
      safeLog(ctx, 'RalphPlugin:getMessages: fetching', { sessionID });
      if (injectedGetMessages) {
        try {
          const injected = await injectedGetMessages({ sessionID, ctx });
          safeLog(ctx, 'RalphPlugin:getMessages: injected fetch returned', { length: Array.isArray(injected) ? injected.length : 0 });
          return Array.isArray(injected) ? injected : [];
        } catch (err) {
          safeLog(ctx, 'RalphPlugin:getMessages: injected fetch error', err && err.message ? err.message : err);
          return [];
        }
      }

      try {
        const res = await ctx.client.session.messages({ path: { id: sessionID }, query: { limit: 200 } });
        const count = res && Array.isArray(res.data) ? res.data.length : 0;
        safeLog(ctx, 'RalphPlugin:getMessages: fetched from session API', { count });
        if (!res || res.error || !Array.isArray(res.data)) return [];
        return res.data;
      } catch (err) {
        safeLog(ctx, 'RalphPlugin:getMessages: session API error', err && err.message ? err.message : err);
        return [];
      }
    }

    return {
      /**
       * Compaction hook called by the runtime to produce a compact prompt/context.
       * This handler inspects the original user prompt, matches it against configured
       * overrides and either replaces `output.prompt` (mode: 'replace') or prepends
       * rendered content into `output.context` (mode: 'prepend').
       *
       * @param {Object} input - Hook input (expected to contain `sessionID`).
       * @param {Object} output - Hook output to be mutated (`prompt`, `context`).
       * @returns {Promise<void>}
       */
      'experimental.session.compacting': async (input, output) => {
        try {
          const sessionID = typeof input?.sessionID === 'string' ? input.sessionID : '';
          if (!sessionID || !output) return;

          safeLog(ctx, 'RalphPlugin:compacting:start', { sessionID });

          const messages = await getMessages(sessionID);
          const original = extractUserPrompt(messages);
          safeLog(ctx, 'RalphPlugin:compacting:extractedOriginal', { original: original ? original.slice(0, 1000) : '' });
          if (!original) {
            safeLog(ctx, 'RalphPlugin:compacting: no original prompt found');
            return;
          }

          for (let i = 0; i < overrides.length; i++) {
            const ov = overrides[i];
            ov.regex.lastIndex = 0;
            safeLog(ctx, 'RalphPlugin:compacting:tryingOverride', { index: i, pattern: ov.regex.toString(), mode: ov.mode });
            const match = ov.regex.exec(original);
            safeLog(ctx, 'RalphPlugin:compacting:matchResult', { index: i, matched: !!match });
            if (!match) continue;
            const rendered = renderTemplate(ov.template, match);
            safeLog(ctx, 'RalphPlugin:compacting:rendered', { index: i, rendered: rendered ? rendered.slice(0, 1000) : '' });
            if (!rendered) continue;

            if (ov.mode === 'replace') {
              output.prompt = `${rendered}\n\nOriginal user prompt: ${original}`;
              safeLog(ctx, 'RalphPlugin:compacting:output.prompt set', { rendered: output.prompt.slice(0, 200) });
            } else {
              if (!Array.isArray(output.context)) output.context = [];
              output.context.unshift(rendered);
              safeLog(ctx, 'RalphPlugin:compacting:prepended to output.context', { rendered: rendered.slice(0, 200) });
            }
            return;
          }

          // No override matched — append the original prompt to output.context.
          // The compaction pipeline may merge context entries into downstream prompts.
          safeLog(ctx, 'RalphPlugin:compacting:no override matched; appending original to context');
          if (!Array.isArray(output.context)) output.context = [];
          output.context.push(`Original user prompt: ${original}`);
        } catch (err) {
          safeLog(ctx, 'RalphPlugin:compacting:error', err && err.message ? err.message : err);
          return;
        }
      },
    };
  };
}

export const RalphPlugin = createRalphPlugin();
export default RalphPlugin;
