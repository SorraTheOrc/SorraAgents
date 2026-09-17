# proxy-compaction-bridge — pi client extension

Mirrors the llama-proxy ("Local Proxy") compacted view in pi's **dispatch**
layer, so the proxy resumes **incremental** compaction instead of
re-summarizing the whole session from scratch on every turn.

The session JSONL (**storage**) is never touched — the reshape only affects the
per-turn context sent to the provider — so pre-compaction history stays fully
recoverable via `/tree`, `/fork` and `/export`.

## Why

When the proxy compacts a long session it keeps the system prompt(s) + the very
first user prompt verbatim, folds the middle turns into a single summary-marker
message, and retains a tail of whole recent turns. It stores that compacted
history server-side. If the client keeps sending the *uncompacted* full history,
the proxy's history match fails and it re-summarizes from scratch (CREATION) on
every turn. This extension makes the client send the same compacted history the
proxy already stored, so the proxy resumes incremental compaction.

## Wire contract (producer: LP-0MTYGZ1DI0004QP8)

Emitted only when live compaction is applied, on both buffered and streaming
responses:

| Header | Value |
| --- | --- |
| `X-Compaction-Occurred` | `true`; the other three are emitted with it |
| `X-Compaction-Marker` | **base64** of the exact message content the proxy injected, including the `_SUMMARY_MARKER` / `_SUMMARY_MARKER_END` delimiters |
| `X-Compaction-Turns-Summarized` | decimal integer — folded middle turns |
| `X-Compaction-Recent-Turns-Kept` | decimal integer — retained tail turns |

Contract rules the extension honours:

- `X-Compaction-Occurred: true` is the **only** trigger. Missing or malformed
  headers are a no-op: the original context passes through unchanged and any
  previously captured in-memory state is retained.
- The marker header is decoded verbatim; the extension never constructs,
  reformats, or guesses the marker text (delimiters included).
- Absent headers do **not** clear captured state. Mirroring continues until the
  session ends or the client restarts; the proxy re-signals once after a
  restart, then the view is stable.

## Install

Symlink (or copy) this directory into a pi extension discovery location:

```bash
mkdir -p ~/.pi/agent/extensions
ln -s "$PWD/pi-client/proxy-compaction-bridge" ~/.pi/agent/extensions/proxy-compaction-bridge
```

Inside pi, run `/reload` (or restart pi) to load the extension. Verify it is
active with `/extensions` — `proxy-compaction-bridge` should be listed.

## Usage

Nothing to configure. Run pi against the proxy ("Local Proxy" provider). After
the proxy signals a compaction, the next dispatched context is reshaped to
`[system…, first_user, proxy_marker, …recent]`. Storage is unaffected.

### Manual verification (end-to-end)

1. Start a session long enough to cross the proxy's compaction trigger (fast
   ≈58,300 tokens / cheap ≈43K — see `llm-manager/proxy/proxy/compaction.py`).
2. Confirm the proxy applied compaction and emitted the headers:
   `X-Compaction-Occurred: true` plus the marker and counts.
3. Snapshot the session file, continue the conversation, then diff it — only
   normal append-only growth (no rewrite/truncation by the extension):
   `diff <(cp "$SESSION_FILE" /tmp/before && cat /tmp/before) "$SESSION_FILE"`.
4. Confirm `/tree`, `/fork` and `/export` still expose the full pre-compaction
   history.
5. Confirm the dispatched context contains the decoded marker verbatim and
   excludes the folded middle turns (proxy logs / request capture).

Automated coverage of the pure logic is in
`tests/unit/test_compaction-bridge.mjs` (header parse matrix, marker
base64 round-trip, turn-boundary handling, tail/exclusion accounting,
passthrough on missing/malformed headers, input immutability).

## Files

- `index.ts` — pi extension entry (event wiring: `after_provider_response` +
  `context`).
- `compaction-bridge.js` — pure, dependency-free parsing/reshaping
  (unit tested).
- `package.json` — extension metadata.

## Coupling point (AC8)

The four header names and their value formats are coupled to the proxy-side
implementation and tests in **LP-0MTYGZ1DI0004QP8**
(`llm-manager/proxy/proxy/provider.py` emission + its unit tests). If the
proxy changes a header name, value encoding (base64 vs raw, decimal vs hex), or
the turn-accounting semantics (`pair_turns` in
`llm-manager/proxy/proxy/compaction.py`), this extension must change in lock
step. The header-name constants live at the top of `compaction-bridge.js` and
are asserted verbatim in the unit tests.

## Notes

- Only response headers are read; no pi internals are modified (extension file
  only).
- Header availability depends on the provider/transport exposing HTTP response
  headers. The "Local Proxy" provider uses pi's built-in `openai-completions`
  transport, which fires `after_provider_response` with the real response
  headers, so the contract applies.
- Header size caveat (LP-0MTYGZ1DI0004QP8): summaries are base64'd into a single
  header; if real summaries exceed a few KB, revisit with a sidecar endpoint
  (`GET /sessions/<id>/compaction`).
