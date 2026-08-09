/**
 * sse-signals.js
 *
 * Pure SSE signal handling for the llama-proxy (`: re-route ...` and
 * `: chain exhausted ...` comment markers). This module has NO pi imports so
 * it can be unit tested in plain Node.
 *
 * The llama-proxy emits OpenAI/SSE-standard comment lines (starting with
 * `: `) during fallback activity. Standard SSE parsers discard comment lines
 * by design, so the pi client never saw them. This module detects comment
 * lines in a raw event-stream body, classifies the proxy signals they carry,
 * and formats them for a dimmed status line.
 *
 * Signal conventions (emitted by proxy/proxy/provider.py):
 *   - re-route:   `: re-route provider=<from>-><to> reason=<reason>`
 *   - chain-hold: `: chain exhausted (<diagnostics>); retrying from <model> in <Ns>`
 */

// ---------------------------------------------------------------------------
// Line classification
// ---------------------------------------------------------------------------

/**
 * Returns true when a raw SSE line is a comment line (starts with ":").
 * SSE comment lines are metadata and are ignored by standard parsers.
 */
export function isSseCommentLine(line) {
  return typeof line === "string" && line.startsWith(":");
}

/**
 * Returns the comment text for an SSE comment line, or null for non-comment
 * lines. Handles the optional space after ":" (`: text` and `:text`) and a
 * trailing CR from CRLF line endings.
 */
export function commentText(line) {
  if (!isSseCommentLine(line)) return null;
  return line.slice(1).replace(/^ /, "").replace(/\r$/, "");
}

// ---------------------------------------------------------------------------
// Signal classification and formatting
// ---------------------------------------------------------------------------

/**
 * Classify a proxy signal comment into a structured signal.
 *
 * @param {string} comment comment text (no leading ":")
 * @returns {{ kind: "reroute"; providerFrom: string; providerTo: string; reason: string }
 *          | { kind: "chain-hold"; model: string; seconds?: number }
 *          | { kind: "unknown"; text: string }}
 */
export function parseSignal(comment) {
  if (comment.startsWith("re-route")) {
    const providerMatch = comment.match(/provider=([^\s>]+)->([^\s]+)/);
    const reasonMatch = comment.match(/reason=([^\s]+)/);
    return {
      kind: "reroute",
      providerFrom: providerMatch?.[1] ?? "",
      providerTo: providerMatch?.[2] ?? "",
      reason: reasonMatch?.[1] ?? "",
    };
  }
  if (comment.startsWith("chain exhausted")) {
    const modelMatch = comment.match(/retrying from\s+([^\s,;]+)/);
    const secondsMatch = comment.match(/in\s+(\d+)\s*s/i);
    return {
      kind: "chain-hold",
      model: modelMatch?.[1] ?? "",
      seconds: secondsMatch ? parseInt(secondsMatch[1], 10) : undefined,
    };
  }
  return { kind: "unknown", text: comment };
}

/**
 * Format a proxy signal comment for display in a status line.
 * Unrecognised comments are surfaced verbatim so nothing is ever swallowed.
 *
 * @param {string} comment comment text (no leading ":")
 * @returns {string} human-readable signal line
 */
export function formatSignal(comment) {
  const signal = parseSignal(comment);
  if (signal.kind === "reroute") {
    if (signal.providerFrom && signal.providerTo) {
      const arrow = `${signal.providerFrom} → ${signal.providerTo}`;
      return signal.reason
        ? `re-route: ${arrow} · ${signal.reason}`
        : `re-route: ${arrow}`;
    }
    return `re-route: ${comment.replace(/^re-route\s*/, "") || "provider changed"}`;
  }
  if (signal.kind === "chain-hold") {
    let text = "chain exhausted — retrying";
    if (signal.model) text += ` from ${signal.model}`;
    if (signal.seconds !== undefined) text += ` in ${signal.seconds}s`;
    return text;
  }
  return comment;
}

// ---------------------------------------------------------------------------
// Streaming extraction
// ---------------------------------------------------------------------------

/**
 * A stateful SSE line extractor that finds comment lines across chunk
 * boundaries. Feed decoded text chunks via `push()`; call `flush()` at end of
 * stream to handle a trailing partial line.
 */
export function createSignalExtractor() {
  let buffer = "";
  return {
    push(text) {
      buffer += text;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      const signals = [];
      for (const line of lines) {
        const comment = commentText(line);
        if (comment) signals.push(comment);
      }
      return signals;
    },
    flush() {
      const signals = [];
      if (buffer.length > 0) {
        const comment = commentText(buffer.trimEnd());
        if (comment) signals.push(comment);
      }
      buffer = "";
      return signals;
    },
  };
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

function safeCall(fn, ...args) {
  try {
    fn(...args);
  } catch {
    // Signal rendering must never break the response stream.
  }
}

/**
 * Wrap a fetch implementation so SSE comment lines in event-stream response
 * bodies are forwarded to `onSignal(comment)` while ALL bytes pass through
 * unchanged (the provider SDK sees an identical response body — zero content
 * impact). Non event-stream responses are returned untouched.
 *
 * @param {typeof fetch} originalFetch underlying fetch implementation
 * @param {(signalText: string) => void} onSignal called with each formatted signal
 * @param {(comment: string) => string} [format] signal formatter (default: formatSignal)
 * @returns {typeof fetch}
 */
export function createCapturingFetch(originalFetch, onSignal, format = formatSignal) {
  return async function capturingFetch(input, init) {
    const response = await originalFetch(input, init);
    if (!response || !response.body) return response;
    const contentType = response.headers?.get?.("content-type") ?? "";
    if (!contentType.includes("text/event-stream")) return response;

    const extractor = createSignalExtractor();
    const decoder = new TextDecoder();
    const { readable, writable } = new TransformStream();
    const writer = writable.getWriter();

    (async () => {
      const reader = response.body.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) {
            for (const comment of extractor.flush()) safeCall(onSignal, format(comment));
            await writer.close();
            break;
          }
          const text = decoder.decode(value, { stream: true });
          for (const comment of extractor.push(text)) safeCall(onSignal, format(comment));
          await writer.write(value);
        }
      } catch (error) {
        try {
          await writer.abort(error);
        } catch {
          // Response may already be closed (abort/cancel) — nothing to do.
        }
      } finally {
        reader.releaseLock();
      }
    })();

    return new Response(readable, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
}
