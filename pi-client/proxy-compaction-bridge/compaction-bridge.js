/**
 * compaction-bridge.js
 *
 * Pure, dependency-free core for the `proxy-compaction-bridge` pi client
 * extension. It has NO pi imports so it can be unit tested in plain Node.
 *
 * The llama-proxy ("Local Proxy") compacts long sessions at prompt-assembly
 * time: it keeps the system prompt(s) + the very first user prompt verbatim,
 * folds the middle turns into a single summary-marker message, and retains a
 * tail of whole recent turns. When live compaction is applied it signals the
 * client via response headers (wire contract LP-0MTYGZ1DI0004QP8):
 *
 *   X-Compaction-Occurred: true
 *   X-Compaction-Marker: <base64 of the exact injected message content>
 *   X-Compaction-Turns-Summarized: <decimal integer>
 *   X-Compaction-Recent-Turns-Kept: <decimal integer>
 *
 * This module parses that signal and reshapes the *dispatch* context so the
 * client sends the proxy the same compacted history the proxy already stored —
 * enabling incremental compaction instead of CREATION re-summarization on every
 * turn. The session JSONL is never touched; the reshape only affects what is
 * dispatched to the provider for a single request.
 *
 * Proxy-side reference semantics (proxy/proxy/compaction.py):
 *   - `pair_turns`: a new turn begins at each user message; every following
 *     message attaches to the current turn until the next user message; a
 *     leading non-user message opens its own turn.
 *   - compacted view = `[...system, first_user, summary_marker, ...recent]`.
 */

// ---------------------------------------------------------------------------
// Wire contract — exact header names frozen by LP-0MTYGZ1DI0004QP8.
// Look-ups are case-insensitive (Fetch `Response.headers` is normalised to
// lower case), but the emitted names must stay exactly these strings.
// ---------------------------------------------------------------------------

export const HEADER_OCCURRED = "X-Compaction-Occurred";
export const HEADER_MARKER = "X-Compaction-Marker";
export const HEADER_TURNS_SUMMARIZED = "X-Compaction-Turns-Summarized";
export const HEADER_RECENT_TURNS_KEPT = "X-Compaction-Recent-Turns-Kept";

const USER_ROLE = "user";
const SYSTEM_ROLE = "system";

// ---------------------------------------------------------------------------
// Header parsing
// ---------------------------------------------------------------------------

/**
 * Case-insensitive header lookup. `pi` normalises provider response header
 * names to lower case; accept either casing so the parser is robust to both.
 *
 * @param {Record<string, string> | undefined} headers
 * @param {string} name
 * @returns {string | undefined}
 */
export function getHeader(headers, name) {
  if (!headers || typeof headers !== "object") return undefined;
  const target = name.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === target) return headers[key];
  }
  return undefined;
}

/**
 * Decode a base64 header value as UTF-8 text. Throws on malformed base64.
 *
 * @param {string} value
 * @returns {string}
 */
function decodeBase64Utf8(value) {
  const binary = atob(String(value).trim());
  const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/**
 * Parse a decimal-integer header value, or null when absent/non-numeric.
 *
 * @param {string | undefined} value
 * @returns {number | null}
 */
function parseCount(value) {
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  if (!/^\d+$/.test(text)) return null;
  return Number.parseInt(text, 10);
}

/**
 * Parse the compaction signal from a provider response's headers.
 *
 * Returns `{ marker, turnsSummarized, recentTurnsKept }` when the response
 * carries a *complete, well-formed* compaction signal, or `null` otherwise.
 *
 * Fail-safe by contract (AC7): a missing or malformed header is a no-op — the
 * caller must leave previously captured state untouched and pass the original
 * context through unchanged. `X-Compaction-Occurred: true` is the only trigger;
 * the other three headers are required alongside it.
 *
 * @param {Record<string, string> | undefined} headers
 * @returns {{ marker: string, turnsSummarized: number, recentTurnsKept: number } | null}
 */
export function parseCompactionSignal(headers) {
  const occurred = getHeader(headers, HEADER_OCCURRED);
  if (String(occurred ?? "").trim().toLowerCase() !== "true") return null;

  const markerHeader = getHeader(headers, HEADER_MARKER);
  if (typeof markerHeader !== "string" || markerHeader.length === 0) return null;

  let marker;
  try {
    marker = decodeBase64Utf8(markerHeader);
  } catch {
    return null; // malformed base64 → no-op
  }
  if (marker.length === 0) return null;

  const turnsSummarized = parseCount(getHeader(headers, HEADER_TURNS_SUMMARIZED));
  const recentTurnsKept = parseCount(getHeader(headers, HEADER_RECENT_TURNS_KEPT));
  if (turnsSummarized === null || recentTurnsKept === null) return null;

  return { marker, turnsSummarized, recentTurnsKept };
}

// ---------------------------------------------------------------------------
// Turn grouping and dispatch reshaping
// ---------------------------------------------------------------------------

/**
 * Group a message list into whole turns using the proxy's `pair_turns`
 * semantics: a new turn begins at each user message; every following message
 * (assistant, toolResult, …) attaches to the current turn until the next user
 * message. A leading non-user message opens its own turn.
 *
 * Lossless: every message lands in exactly one turn.
 *
 * @param {Array<{ role?: string }>} messages
 * @returns {Array<Array<{ role?: string }>>}
 */
export function pairTurns(messages) {
  const turns = [];
  for (const message of messages) {
    if (message?.role === USER_ROLE || turns.length === 0) {
      turns.push([message]);
    } else {
      turns[turns.length - 1].push(message);
    }
  }
  return turns;
}

/**
 * Reshape the dispatch context to mirror the proxy's compacted view.
 *
 * Given the client's full history and the last captured compaction signal,
 * returns a NEW message list:
 *
 *   [ ...system..., first_user, marker_message, ...tail_turns ]
 *
 * where the tail is every turn after the head EXCEPT the oldest
 * `turnsSummarized` whole turns. In other words the folded middle turns are
 * replaced by the decoded marker message (verbatim — never constructed here)
 * and no turn is ever split. Immediately after the signal the tail therefore
 * has exactly `recentTurnsKept` turns; as the conversation continues, later
 * turns are kept (never dropped), so the mirror stays incremental.
 *
 * Returns `null` when no reshape is possible or appropriate (no user message,
 * or fewer turns than the signal says were folded). The caller must treat
 * `null` as "pass the original context through unchanged".
 *
 * Pure: `messages` and its entries are never mutated.
 *
 * @param {Array<{ role?: string, content?: unknown }>} messages
 * @param {{ marker: string, turnsSummarized: number, recentTurnsKept: number } | null} signal
 * @returns {Array<{ role?: string, content?: unknown }> | null}
 */
export function reshapeContext(messages, signal) {
  if (!signal || !Array.isArray(messages)) return null;

  const systemMessages = messages.filter((m) => m?.role === SYSTEM_ROLE);
  const firstUserIndex = messages.findIndex((m) => m?.role === USER_ROLE);
  if (firstUserIndex === -1) return null; // nothing to anchor a head to

  const head = [...systemMessages, messages[firstUserIndex]];
  const turns = pairTurns(messages.slice(firstUserIndex + 1));
  if (turns.length < signal.turnsSummarized) return null; // anomalous signal

  const tail = turns.slice(signal.turnsSummarized).flat();
  const markerMessage = { role: USER_ROLE, content: signal.marker };
  return [...head, markerMessage, ...tail];
}

// ---------------------------------------------------------------------------
// Per-session bridge (stateful glue, still dependency-free)
// ---------------------------------------------------------------------------

/**
 * Create the per-session compaction mirror.
 *
 * The returned object is the whole extension behaviour, free of any pi
 * dependency, so it can be unit tested in plain Node. `index.ts` only wires
 * it to the `after_provider_response` and `context` events.
 *
 * State is in-memory only (never persisted). An absent/malformed header set
 * leaves any previously captured state untouched (contract rule: absent
 * headers mean "no new signal").
 *
 * @returns {{
 *   handleResponse: (sessionId: string, headers: Record<string, string> | undefined) => boolean,
 *   reshape: (sessionId: string, messages: Array<{ role?: string, content?: unknown }>) => Array<{ role?: string, content?: unknown }> | null,
 *   hasState: (sessionId: string) => boolean,
 *   clear: (sessionId: string) => void,
 * }}
 */
export function createCompactionBridge() {
  const states = new Map();

  return {
    /**
     * Record a provider response's headers for a session.
     * @returns {boolean} true when a new, well-formed signal was captured.
     */
    handleResponse(sessionId, headers) {
      const signal = parseCompactionSignal(headers);
      if (!signal) return false; // no new signal → keep prior state
      states.set(sessionId, signal);
      return true;
    },

    /**
     * Reshape a session's dispatch context, or null to pass it through.
     * @returns {Array<{ role?: string, content?: unknown }> | null}
     */
    reshape(sessionId, messages) {
      const state = states.get(sessionId);
      if (!state) return null; // no compaction signalled yet
      return reshapeContext(messages, state);
    },

    /** Whether a compaction signal has been captured for the session. */
    hasState(sessionId) {
      return states.has(sessionId);
    },

    /** Drop a session's captured state (e.g. on session end). */
    clear(sessionId) {
      states.delete(sessionId);
    },
  };
}
