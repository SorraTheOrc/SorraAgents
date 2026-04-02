const DEFAULT_OVERRIDES = [
  {
    pattern: '^implement\\s+(\\S+)$',
    template: 'audit {1} and address any issues the audit identifies',
  },
];

function parseOverrides(rawOverrides) {
  if (!Array.isArray(rawOverrides)) {
    return [];
  }

  const compiled = [];

  for (const item of rawOverrides) {
    if (!item || typeof item !== 'object') {
      continue;
    }

    const pattern = typeof item.pattern === 'string' ? item.pattern : '';
    const template = typeof item.template === 'string' ? item.template : '';
    const flags = typeof item.flags === 'string' ? item.flags : '';

    if (!pattern || !template) {
      continue;
    }

    try {
      compiled.push({
        regex: new RegExp(pattern, flags),
        template,
      });
    } catch (_error) {
      continue;
    }
  }

  return compiled;
}

function collectText(parts) {
  if (!Array.isArray(parts)) {
    return '';
  }

  const text = [];
  for (const part of parts) {
    if (part && part.type === 'text' && typeof part.text === 'string') {
      const trimmed = part.text.trim();
      if (trimmed) {
        text.push(trimmed);
      }
    }
  }
  return text.join('\n').trim();
}

function getOriginalPrompt(messages) {
  if (!Array.isArray(messages)) {
    return '';
  }

  const users = messages
    .filter((entry) => entry && entry.info && entry.info.role === 'user')
    .sort((a, b) => {
      const left = Number(a?.info?.time?.created ?? Number.MAX_SAFE_INTEGER);
      const right = Number(b?.info?.time?.created ?? Number.MAX_SAFE_INTEGER);
      return left - right;
    });

  for (const entry of users) {
    const text = collectText(entry.parts);
    if (text) {
      return text;
    }
  }

  return '';
}

function applyTemplate(template, match) {
  return template
    .replace(/\{(\d+)\}/g, (_token, idx) => {
      const value = match?.[Number(idx)];
      return typeof value === 'string' ? value : '';
    })
    .trim();
}

function renderOverride(prompt, overrides) {
  for (const override of overrides) {
    override.regex.lastIndex = 0;
    const match = override.regex.exec(prompt);
    if (!match) {
      continue;
    }
    const rendered = applyTemplate(override.template, match);
    if (rendered) {
      return rendered;
    }
  }
  return '';
}

function buildPrompt(derivedPrompt, originalPrompt) {
  const lines = [
    derivedPrompt,
    '',
    'Preserve intent and actionable state for seamless continuation.',
    'Include key decisions, changed files, blockers, and the next concrete steps.',
  ];

  if (originalPrompt && originalPrompt !== derivedPrompt) {
    lines.push('', `Original user prompt: ${originalPrompt}`);
  }

  return lines.join('\n');
}

export function createRalphPlugin(deps = {}) {
  const injectedGetMessages = typeof deps.getMessages === 'function' ? deps.getMessages : null;
  const defaultOptions = deps && typeof deps.options === 'object' ? deps.options : {};

  return async function RalphPlugin(ctx, options = {}) {
    const mergedOptions = {
      ...defaultOptions,
      ...(options && typeof options === 'object' ? options : {}),
    };

    const disableDefaultOverrides = mergedOptions.disableDefaultOverrides === true;
    const customOverrides = Array.isArray(mergedOptions.overrides) ? mergedOptions.overrides : [];
    const overrides = parseOverrides([
      ...customOverrides,
      ...(disableDefaultOverrides ? [] : DEFAULT_OVERRIDES),
    ]);

    async function getMessages(sessionID) {
      if (injectedGetMessages) {
        return injectedGetMessages({ sessionID, ctx });
      }

      const result = await ctx.client.session.messages({
        path: { id: sessionID },
        query: { limit: 200 },
      });

      if (!result || result.error || !Array.isArray(result.data)) {
        return [];
      }

      return result.data;
    }

    return {
      'experimental.session.compacting': async (input, output) => {
        try {
          const sessionID = typeof input?.sessionID === 'string' ? input.sessionID : '';

          if (!sessionID || !output || !Array.isArray(output.context)) {
            return;
          }

          const messages = await getMessages(sessionID);
          const originalPrompt = getOriginalPrompt(messages);
          if (!originalPrompt) {
            return;
          }

          const overridePrompt = renderOverride(originalPrompt, overrides);
          if (overridePrompt) {
            output.prompt = buildPrompt(overridePrompt, originalPrompt);
            return;
          }

          output.context.push(`Original user prompt: ${originalPrompt}`);
        } catch (_error) {
          return;
        }
      },
    };
  };
}

export const RalphPlugin = createRalphPlugin();
export default RalphPlugin;
