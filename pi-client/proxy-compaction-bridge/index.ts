/**
 * proxy-compaction-bridge — pi client extension
 *
 * Mirrors the llama-proxy's compacted view in the client's *dispatch* layer so
 * the proxy resumes INCREMENTAL compaction instead of re-summarizing the whole
 * session from scratch on every turn.
 *
 * How it works:
 *   1. `after_provider_response` reads the proxy's compaction response headers
 *      (`X-Compaction-Occurred`, `X-Compaction-Marker`,
 *      `X-Compaction-Turns-Summarized`, `X-Compaction-Recent-Turns-Kept`),
 *      base64-decodes the marker, and keeps a per-session in-memory state.
 *   2. `context` rebuilds the dispatched message list as
 *      `[system…, first_user, proxy_marker, …recent]` — the marker is inserted
 *      verbatim (never constructed) and the folded middle turns are excluded,
 *      without splitting turns.
 *
 * The session JSONL (storage) is NEVER modified — the reshape only affects the
 * per-turn dispatch, so the full pre-compaction history stays recoverable via
 * `/tree`, `/fork` and `/export`. State is in-memory only; on client restart
 * the proxy re-signals once and the view stabilises again.
 *
 * All behaviour lives in the dependency-free `./compaction-bridge.js` module
 * (unit tested in `tests/unit/test-proxy-compaction-bridge.mjs`); this file is
 * a thin event adapter.
 *
 * Wire contract: LP-0MTYGZ1DI0004QP8 (proxy-side producer). Header names and
 * semantics are documented in `./README.md`.
 *
 * Install: symlink or copy this directory into a pi extension discovery
 * location (e.g. `~/.pi/agent/extensions/proxy-compaction-bridge`) and run
 * `/reload` inside pi.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createCompactionBridge } from "./compaction-bridge.js";

export default function (pi: ExtensionAPI) {
  const bridge = createCompactionBridge();

  pi.on("after_provider_response", (event, ctx) => {
    bridge.handleResponse(ctx.sessionManager.getSessionId(), event.headers);
  });

  pi.on("context", (event, ctx) => {
    const messages = bridge.reshape(ctx.sessionManager.getSessionId(), event.messages);
    if (!messages) return; // no signal / not applicable → pass through unchanged
    return { messages } as { messages: typeof event.messages };
  });
}
