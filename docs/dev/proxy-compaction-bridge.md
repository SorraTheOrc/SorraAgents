# Mirroring proxy-side compaction into the pi client dispatch context (SA-0MTYGZIWF000ZLU0)

## Summary

The llama-proxy ("Local Proxy") compacts long sessions at prompt-assembly
time: it keeps the system prompt(s) + the very first user prompt verbatim,
folds the middle turns into a single summary-marker message, and retains a tail
of whole recent turns (`proxy/proxy/compaction.py`). It stores that compacted
history server-side.

If the pi client keeps sending the *uncompacted* full history, the proxy's
history match fails and it re-summarizes from scratch (CREATION) on every turn,
and local dispatch can fall back with a `history_mismatch`. This work makes the
client send the same compacted history the proxy already stored, so the proxy
resumes **incremental** compaction.

Storage is never touched: the reshape only affects the per-turn **dispatch**
context, so pre-compaction history stays recoverable via `/tree`, `/fork` and
`/export`.

## Wire contract (producer: LP-0MTYGZ1DI0004QP8)

The proxy signals completed live compaction via response headers, on both
buffered and streaming (`StreamingResponse`/SSE) responses:

| Header | Value |
| --- | --- |
| `X-Compaction-Occurred` | `true` (the only trigger) |
| `X-Compaction-Marker` | **base64** of the exact injected message content, including `_SUMMARY_MARKER` / `_SUMMARY_MARKER_END` delimiters |
| `X-Compaction-Turns-Summarized` | decimal integer — folded middle turns |
| `X-Compaction-Recent-Turns-Kept` | decimal integer — retained tail turns |

`pi`'s `after_provider_response` event exposes `event.headers` before the stream
body is consumed. The `Local Proxy` provider uses the built-in
`openai-completions` transport (`@earendil-works/pi-ai`), which fires
`onResponse({ status, headers })` from the real HTTP response
(`dist/api/openai-completions.js`), so the proxy's headers reach the extension.

## Approach: a pi client extension

Modifying the distributed pi package is not viable, so the change ships as a
**pi client extension** in this repo (`pi-client/proxy-compaction-bridge/`).

### State capture — `after_provider_response`

1. Read `event.headers`; only `X-Compaction-Occurred: true` triggers capture.
2. Base64-decode `X-Compaction-Marker` (verbatim — never constructed,
   reformatted, or guessed).
3. Parse the two decimal counts.
4. Store `{ marker, turnsSummarized, recentTurnsKept }` per session id,
   **in memory only**.

A missing or malformed header set is a no-op: it neither clears the captured
state nor changes dispatch. State survives until the session ends or the client
restarts (the proxy re-signals once after a restart, then the view is stable).

### Dispatch reshape — `context`

When a signal has been captured for the session, the dispatched message list is
rebuilt as:

```
[ ...system…, first_user, proxy_marker, ...tail ]
```

- The head is all system messages + the first user message (the proxy's
  retention set).
- Turns are grouped with the proxy's `pair_turns` semantics (a new turn begins
  at each user message; a leading non-user message opens its own turn) — **no
  turn is ever split**.
- The oldest `X-Compaction-Turns-Summarized` whole turns after the head (the
  folded middle) are excluded; the marker message is inserted in their place.
- Immediately after the signal the tail therefore has exactly
  `X-Compaction-Recent-Turns-Kept` turns; later turns are kept, so the mirror
  stays incremental.

`context` results are dispatch-only and non-destructive: the session JSONL is
never written by the extension.

## Files

- `pi-client/proxy-compaction-bridge/index.ts` — extension entry (event
  wiring: `after_provider_response` + `context`)
- `pi-client/proxy-compaction-bridge/compaction-bridge.js` — pure,
  dependency-free parsing/reshaping + per-session bridge
- `pi-client/proxy-compaction-bridge/README.md` — install/usage
- `tests/unit/test_compaction-bridge.mjs` — unit tests (node:test)

## Acceptance criteria coverage

| AC | Verification |
|---|---|
| AC1 — loads via discovery; no-op without headers | Symlink into `~/.pi/agent/extensions/`; `parseCompactionSignal` returns `null` for absent/non-`true` headers and `reshapeContext` passes context through unchanged. |
| AC2 — mirror after a signal | `reshapeContext` tests assert the decoded marker (verbatim) is inserted, the folded middle is excluded, the immediate tail has exactly `recentTurnsKept` turns, and no turn is split. |
| AC3 — JSONL unchanged | The pure module has no storage access; `reshapeContext` returns a new list and never mutates the caller's messages (asserted). |
| AC4 — pi compaction setting irrelevant | The extension never reads/writes pi's compaction setting; behaviour depends only on proxy headers. |
| AC5 — `/tree`, `/fork`, `/export` intact | Storage is untouched (AC3) → the full pre-compaction history remains. |
| AC6 — marker never constructed | Test asserts the inserted content equals the base64-decoded header exactly (including delimiters), and that a different header yields different content. |
| AC7 — coverage matrix | Tests cover: no-header passthrough, header → mirrored dispatch, base64 round-trip, tail-boundary handling, JSONL/purity immutability, malformed-header no-op. |
| AC8 — coupling documented | Header-name constants asserted verbatim; the coupling point to LP-0MTYGZ1DI0004QP8 is documented here and in the extension README. |

## Deployment

Symlink the extension directory into a discovery location and `/reload`:

```bash
ln -s <repo>/pi-client/proxy-compaction-bridge ~/.pi/agent/extensions/proxy-compaction-bridge
```

See `pi-client/proxy-compaction-bridge/README.md` for details.

## Out of scope

- Proxy-side emission changes (owned by LP-0MTYGZ1DI0004QP8).
- Live end-to-end validation requires the proxy to emit the headers; until the
  producer side lands, the extension is validated against the frozen contract
  with simulated headers (unit tests).
