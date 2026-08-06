# Surfacing proxy SSE signal markers in the pi client (SA-0MSHAKSEA001LQ6T)

## Summary

The llama-proxy emits OpenAI/SSE-standard **comment lines** (`: ...`) during
fallback activity to signal live re-routes and chain-hold waits:

- `: re-route provider=<from>-><to> reason=<reason>` (existing, from
  LP-0MSG45I8Q0020N1F)
- `: chain exhausted (<diagnostics>); retrying from <model> in <Ns>`
  (upcoming, from LP-0MSH94Z7K007VKC9)

The pi client's SSE parser (the OpenAI SDK used by
`@earendil-works/pi-ai`'s `openai-completions` API provider) discards comment
lines by design, so operators never saw them. This work surfaces those
signals as a **dimmed footer status line** in the pi TUI.

## Where the client swallowed the comments

- pi connects to the proxy via the `Local Proxy` provider
  (`api: "openai-completions"`, `baseUrl http://192.168.0.199:8000/v1`).
- The `openai-completions` API provider builds an OpenAI SDK client
  (`dist/api/openai-completions.js` → `createClient`) and streams via
  `client.chat.completions.create(...).withResponse()`.
- The OpenAI SDK's internal SSE parser reads the raw body and yields only
  `data:` events; `: ` comment lines are dropped.

## Approach: a pi client extension

Modifying the distributed pi package is not viable (global npm install, no
local source), so the change ships as a **pi client extension** (the
sanctioned customization mechanism) in this repo.

### Signal capture

`pi.registerProvider("Local Proxy", { api: "openai-completions", streamSimple })`
overrides the proxy provider's streaming function. The custom `streamSimple`
(only for `model.provider === "Local Proxy"`; everything else delegates
untouched) re-invokes the built-in `openai-completions` `streamSimple` with a
**capturing fetch** (`StreamOptions.fetch`, supported by the built-in
provider).

The capturing fetch:

1. Calls the real `globalThis.fetch`.
2. For `text/event-stream` responses, wraps the body in a `TransformStream`
   that forwards **every byte unchanged** (the SDK sees an identical body →
   zero content/tool-call impact) while scanning the decoded text for `: `
   comment lines.
3. Forwards each detected signal to the renderer. Non-SSE responses pass
   through untouched.

### Rendering

Signals are metadata — they are **never** injected into assistant `content`
or the conversation history sent back to the provider (the byte stream is
unchanged). They are displayed as a dimmed footer status:

```
ctx.ui.setStatus("proxy-signals", ctx.ui.theme.fg("dim", "re-route: opencode-go → opencode-go-2 · stall_after_reasoning"))
```

- The UI handle is captured at `turn_start` so signals arriving while the
  response stream is idle (a chain-hold pause before any `data:` event) still
  render immediately.
- The status is cleared at `turn_end`.

### Signal formatting

| Proxy comment | Rendered status |
|---|---|
| `re-route provider=opencode-go->opencode-go-2 reason=stall_after_reasoning` | `re-route: opencode-go → opencode-go-2 · stall_after_reasoning` |
| `chain exhausted (all local slots busy); retrying from local-qwen3 in 240s` | `chain exhausted — retrying from local-qwen3 in 240s` |
| anything else | surfaced verbatim (never swallowed) |

## Files

- `pi-client/proxy-sse-signals/index.ts` — extension entry (provider override,
  turn lifecycle, footer rendering)
- `pi-client/proxy-sse-signals/sse-signals.js` — pure, dependency-free
  detection/formatting module (byte passthrough + signal extraction)
- `pi-client/proxy-sse-signals/package.json` — extension metadata
- `pi-client/proxy-sse-signals/README.md` — install/usage
- `tests/unit/test-proxy-sse-signals.mjs` — unit tests (node:test)

## Acceptance criteria coverage

| AC | Verification |
|---|---|
| AC1 — Comments surfaced | `tests/unit/test-proxy-sse-signals.mjs` feeds a synthetic `: re-route ...` chunk; asserts the signal is emitted to the callback and formatted for display. |
| AC2 — Distinct from content | Byte-passthrough test asserts the raw body still contains the comment while assistant `content` (from `data:` payloads) excludes it, and the signal is emitted separately. |
| AC3 — Chain-hold feedback | Synthetic `: chain exhausted ... in 240s` tests assert the model + countdown are displayed. |
| AC4 — No regression | Passthrough tests assert SSE bytes are forwarded unchanged and non-SSE responses are untouched; full project suite passes. |

## Deployment

Symlink the extension directory into a discovery location and `/reload`:

```bash
ln -s <repo>/pi-client/proxy-sse-signals ~/.pi/agent/extensions/proxy-sse-signals
```

See `pi-client/proxy-sse-signals/README.md` for details.

## Out of scope

- Proxy-side emission changes (owned by LP-0MSH94Z7K007VKC9 / LP-0MSG45I8Q0020N1F).
- Protocol changes — the emission convention stays `: ` prefixed SSE comments.
