# proxy-sse-signals — pi client extension

Surfaces the llama-proxy's SSE comment signals in the pi client UI.

The llama-proxy emits OpenAI/SSE-standard comment lines during fallback
activity:

- `: re-route provider=<from>-><to> reason=<reason>` — mid-stream provider
  re-route (e.g. `stall_after_reasoning`)
- `: chain exhausted (<diagnostics>); retrying from <model> in <Ns>` —
  periodic chain-hold feedback while a request waits for a retry

Standard SSE parsers discard comment lines by design, so the pi client never
showed them. This extension detects the comment lines, formats them, and
renders them as a **dimmed footer status** (`proxy-signals`) — metadata only,
never injected into the assistant transcript.

## Install

Symlink (or copy) this directory into a pi extension discovery location:

```bash
mkdir -p ~/.pi/agent/extensions
ln -s "$PWD/pi-client/proxy-sse-signals" ~/.pi/agent/extensions/proxy-sse-signals
```

Inside pi, run `/reload` (or restart pi) to load the extension.

Verify it is active with `/extensions` — you should see
`proxy-sse-signals` listed.

## Usage

Run pi against the llama-proxy ("Local Proxy" provider). When the proxy
re-routes mid-stream or holds a request, the footer shows e.g.:

```
re-route: opencode-go → opencode-go-2 · stall_after_reasoning
chain exhausted — retrying from local-qwen3 in 240s
```

The status clears when the turn ends. Assistant content and tool-call
handling are unaffected — the SSE body passes through byte-for-byte.

## Files

- `index.ts` — pi extension entry (provider override + footer rendering)
- `sse-signals.js` — pure, dependency-free signal detection/formatting
  (unit tested in `tests/unit/test-proxy-sse-signals.mjs`)
- `package.json` — extension metadata

## Notes

- Only the `Local Proxy` provider is intercepted; other providers stream
  untouched. To target a differently-named proxy provider, change
  `PROXY_PROVIDER` in `index.ts`.
- The proxy-side emission convention is unchanged (`: ` prefixed SSE
  comments); this extension only surfaces signals that already arrive.
