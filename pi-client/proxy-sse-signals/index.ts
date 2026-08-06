/**
 * proxy-sse-signals — pi client extension
 *
 * Surfaces llama-proxy SSE comment signals (`: re-route ...`,
 * `: chain exhausted ...`) as a dimmed footer status line in the pi TUI
 * instead of letting the SSE parser discard them.
 *
 * How it works:
 *   1. Overrides the streaming function of the proxy provider ("Local Proxy")
 *      so every request uses a capturing fetch.
 *   2. The capturing fetch passes the SSE body through to the built-in
 *      OpenAI-compatible provider UNCHANGED (no content/tool-call impact),
 *      while separately detecting `: ` comment lines.
 *   3. Signal lines are rendered via ctx.ui.setStatus() with dim styling —
 *      metadata only, never injected into the assistant transcript.
 *
 * Install: symlink or copy this directory into an extension discovery
 * location (e.g. `~/.pi/agent/extensions/proxy-sse-signals`) and run
 * `/reload` inside pi.
 */

import { streamSimple } from "@earendil-works/pi-ai/compat";
import type {
  AssistantMessageEventStream,
  Context,
  Model,
  SimpleStreamOptions,
} from "@earendil-works/pi-ai/compat";
import type { ExtensionAPI, ExtensionUIContext } from "@earendil-works/pi-coding-agent";
import { createCapturingFetch } from "./sse-signals.js";

/** Provider whose event-stream bodies carry the proxy's SSE comment signals. */
const PROXY_PROVIDER = "Local Proxy";

/** Footer status key used to display the latest proxy signal. */
const STATUS_KEY = "proxy-signals";

export default function (pi: ExtensionAPI) {
  // UI handle captured at turn_start so signals arriving while the response
  // stream is idle (e.g. during a chain-hold pause before any data event)
  // are still rendered immediately.
  let statusUi: ExtensionUIContext | null = null;

  pi.on("turn_start", (_event, ctx) => {
    statusUi = ctx.hasUI ? ctx.ui : null;
  });

  pi.on("turn_end", (_event, ctx) => {
    statusUi = null;
    if (ctx.hasUI) ctx.ui.setStatus(STATUS_KEY, undefined);
  });

  const capturingStreamSimple = (
    model: Model,
    context: Context,
    options?: SimpleStreamOptions,
  ): AssistantMessageEventStream => {
    if (model.provider !== PROXY_PROVIDER) {
      // Not a proxy request — delegate untouched.
      return streamSimple(model, context, options);
    }
    const capturingFetch = createCapturingFetch(globalThis.fetch, (signalText) => {
      if (!statusUi) return;
      // Dim styling matches the existing progress/status output convention.
      statusUi.setStatus(STATUS_KEY, statusUi.theme.fg("dim", signalText));
    });
    return streamSimple(model, context, { ...options, fetch: capturingFetch });
  };

  pi.registerProvider(PROXY_PROVIDER, {
    api: "openai-completions",
    streamSimple: capturingStreamSimple,
  });
}
