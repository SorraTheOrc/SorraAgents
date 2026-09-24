/**
 * Unit tests for pi-client/proxy-compaction-bridge/compaction-bridge.js
 *
 * Acceptance criteria covered (SA-0MTYGZIWF000ZLU0):
 *   AC2 — after a compaction-signalled response the next dispatched context
 *         contains the base64-decoded proxy marker (verbatim, delimiters
 *         included), excludes the folded middle turns, and keeps the tail
 *         without splitting turns; excluded count is consistent with
 *         X-Compaction-Turns-Summarized.
 *   AC3 — storage immutability: the reshape is dispatch-only, never mutates the
 *         caller's message list (the session JSONL is never touched).
 *   AC6 — the marker is never constructed by the extension; it comes only from
 *         the base64-decoded header.
 *   AC7 — coverage matrix: no-header passthrough, header → mirrored dispatch,
 *         marker base64 round-trip, tail-boundary handling, malformed-header
 *         no-op.
 *   AC8 — the four header names are frozen exactly as specified by the
 *         proxy-side contract (LP-0MTYGZ1DI0004QP8).
 *
 * Run: node --test tests/unit/test_compaction-bridge.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(
  __dirname,
  "..",
  "..",
  "pi-client",
  "proxy-compaction-bridge",
  "compaction-bridge.js",
);

const {
  HEADER_OCCURRED,
  HEADER_MARKER,
  HEADER_TURNS_SUMMARIZED,
  HEADER_RECENT_TURNS_KEPT,
  getHeader,
  parseCompactionSignal,
  pairTurns,
  reshapeContext,
  createCompactionBridge,
} = await import(MODULE);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const b64 = (value) => Buffer.from(value, "utf8").toString("base64");

const MARKER_TEXT =
  "The conversation history before this point was compacted into the " +
  "following summary:\n\n<summary>\nThe user asked about widgets → the " +
  "agent answered.\n</summary>";

/** A complete, well-formed compaction header set (mixed casing on purpose). */
function validHeaders(marker = MARKER_TEXT, turnsSummarized = 2, recentTurnsKept = 2) {
  return {
    "X-Compaction-Occurred": "true",
    "X-Compaction-Marker": b64(marker),
    "X-Compaction-Turns-Summarized": String(turnsSummarized),
    "X-Compaction-Recent-Turns-Kept": String(recentTurnsKept),
  };
}

const system = (text) => ({ role: "system", content: text });
const user = (text) => ({ role: "user", content: text });
const assistant = (text) => ({
  role: "assistant",
  content: [{ type: "text", text }],
  api: "openai-completions",
  provider: "Local Proxy",
  model: "plan",
  usage: {},
  stopReason: "stop",
  timestamp: 1,
});
const toolResult = (id) => ({
  role: "toolResult",
  toolCallId: id,
  toolName: "bash",
  content: [{ type: "text", text: "ok" }],
  isError: false,
  timestamp: 1,
});

const signal = (marker = MARKER_TEXT, turnsSummarized = 2, recentTurnsKept = 2) => ({
  marker,
  turnsSummarized,
  recentTurnsKept,
});

/**
 * History with 4 turns after the first user prompt:
 *   head   = [system, u1]
 *   turns  = [ [a1,tr1], [u2,a2], [u3,a3], [u4,a4] ]
 */
function history() {
  return [
    system("S"),
    user("u1"),
    assistant("a1"),
    toolResult("call-1"),
    user("u2"),
    assistant("a2"),
    user("u3"),
    assistant("a3"),
    user("u4"),
    assistant("a4"),
  ];
}

// ---------------------------------------------------------------------------
// AC8 — frozen header names
// ---------------------------------------------------------------------------

describe("wire contract header names (AC8)", () => {
  test("exactly the four contract header names are exported", () => {
    assert.equal(HEADER_OCCURRED, "X-Compaction-Occurred");
    assert.equal(HEADER_MARKER, "X-Compaction-Marker");
    assert.equal(HEADER_TURNS_SUMMARIZED, "X-Compaction-Turns-Summarized");
    assert.equal(HEADER_RECENT_TURNS_KEPT, "X-Compaction-Recent-Turns-Kept");
  });

  test("all four share the X-Compaction- prefix", () => {
    for (const name of [
      HEADER_OCCURRED,
      HEADER_MARKER,
      HEADER_TURNS_SUMMARIZED,
      HEADER_RECENT_TURNS_KEPT,
    ]) {
      assert.match(name, /^X-Compaction-/);
    }
  });
});

// ---------------------------------------------------------------------------
// Header parsing (AC7)
// ---------------------------------------------------------------------------

describe("parseCompactionSignal", () => {
  test("returns null when headers are absent", () => {
    assert.equal(parseCompactionSignal(undefined), null);
    assert.equal(parseCompactionSignal({}), null);
  });

  test("returns null when X-Compaction-Occurred is not 'true' (no trigger)", () => {
    const headers = validHeaders();
    headers["X-Compaction-Occurred"] = "false";
    assert.equal(parseCompactionSignal(headers), null);

    delete headers["X-Compaction-Occurred"];
    assert.equal(parseCompactionSignal(headers), null);
  });

  test("parses a complete signal and round-trips the marker verbatim", () => {
    const parsed = parseCompactionSignal(validHeaders(MARKER_TEXT, 3, 1));
    assert.deepEqual(parsed, {
      marker: MARKER_TEXT,
      turnsSummarized: 3,
      recentTurnsKept: 1,
    });
  });

  test("looks up headers case-insensitively", () => {
    const lowercase = {
      "x-compaction-occurred": "true",
      "x-compaction-marker": b64("marker"),
      "x-compaction-turns-summarized": "1",
      "x-compaction-recent-turns-kept": "0",
    };
    assert.deepEqual(parseCompactionSignal(lowercase), {
      marker: "marker",
      turnsSummarized: 1,
      recentTurnsKept: 0,
    });
  });

  test("accepts zero counts", () => {
    const parsed = parseCompactionSignal(validHeaders("m", 0, 0));
    assert.deepEqual(parsed, { marker: "m", turnsSummarized: 0, recentTurnsKept: 0 });
  });

  test("is a no-op when the marker header is missing or empty", () => {
    const missing = validHeaders();
    delete missing["X-Compaction-Marker"];
    assert.equal(parseCompactionSignal(missing), null);

    assert.equal(parseCompactionSignal(validHeaders("")), null);
  });

  test("is a no-op when the marker is not valid base64", () => {
    const headers = validHeaders();
    headers["X-Compaction-Marker"] = "not base64!!! ***";
    assert.equal(parseCompactionSignal(headers), null);
  });

  test("is a no-op when a count is missing or non-decimal", () => {
    const missing = validHeaders();
    delete missing["X-Compaction-Turns-Summarized"];
    assert.equal(parseCompactionSignal(missing), null);

    const nonDecimal = validHeaders();
    nonDecimal["X-Compaction-Recent-Turns-Kept"] = "two";
    assert.equal(parseCompactionSignal(nonDecimal), null);

    const negative = validHeaders();
    negative["X-Compaction-Turns-Summarized"] = "-1";
    assert.equal(parseCompactionSignal(negative), null);
  });

  test("malformed count makes the whole signal a no-op (occurred=true alone is not enough)", () => {
    const headers = {
      "X-Compaction-Occurred": "true",
      "X-Compaction-Marker": b64(MARKER_TEXT),
      // counts deliberately absent
    };
    assert.equal(parseCompactionSignal(headers), null);
  });

  test("getHeader returns undefined for absent/empty inputs", () => {
    assert.equal(getHeader(undefined, HEADER_MARKER), undefined);
    assert.equal(getHeader({}, HEADER_MARKER), undefined);
    assert.equal(getHeader({ "X-Compaction-Marker": "x" }, HEADER_MARKER), "x");
  });
});

// ---------------------------------------------------------------------------
// Turn grouping (AC2 foundation)
// ---------------------------------------------------------------------------

describe("pairTurns", () => {
  test("starts a new turn at each user message and attaches followers", () => {
    const turns = pairTurns([
      user("u1"),
      assistant("a1"),
      toolResult("t1"),
      user("u2"),
      assistant("a2"),
    ]);
    assert.equal(turns.length, 2);
    assert.deepEqual(
      turns.map((turn) => turn.map((m) => m.role)),
      [
        ["user", "assistant", "toolResult"],
        ["user", "assistant"],
      ],
    );
  });

  test("a leading non-user message opens its own turn", () => {
    const turns = pairTurns([assistant("a1"), toolResult("t1"), user("u2")]);
    assert.deepEqual(
      turns.map((turn) => turn.map((m) => m.role)),
      [
        ["assistant", "toolResult"],
        ["user"],
      ],
    );
  });

  test("is lossless — every message lands in exactly one turn", () => {
    const messages = history();
    const flat = pairTurns(messages).flat();
    assert.deepEqual(flat, messages);
  });

  test("returns an empty list for empty input", () => {
    assert.deepEqual(pairTurns([]), []);
  });
});

// ---------------------------------------------------------------------------
// Dispatch reshaping (AC2, AC3, AC6, AC7)
// ---------------------------------------------------------------------------

describe("reshapeContext", () => {
  test("mirrors the proxy view: head + marker + tail, middle turns excluded (AC2)", () => {
    // history turns = [[a1,tr1],[u2,a2],[u3,a3],[u4,a4]]
    const messages = history();
    // Fold the oldest 2 turns, keep the 2 newest.
    const result = reshapeContext(messages, signal(MARKER_TEXT, 2, 2));

    assert.deepEqual(
      result.map((m) => m.role),
      ["system", "user", "user", "user", "assistant", "user", "assistant"],
    );
    // head: system + first user
    assert.equal(result[0], messages[0]);
    assert.equal(result[1], messages[1]);
    // marker message inserted right after the head, role user
    assert.deepEqual(result[2], { role: "user", content: MARKER_TEXT });
    // tail: turns[2..] = [u3,a3],[u4,a4]
    assert.deepEqual(
      result.slice(3),
      [messages[6], messages[7], messages[8], messages[9]],
    );

    // excluded middle count is consistent with turnsSummarized (2 turns)
    const excluded = messages.filter(
      (m) => m === messages[2] || m === messages[3] || m === messages[4] || m === messages[5],
    );
    assert.equal(excluded.length, 4); // a1,tr1 (turn 1) + u2,a2 (turn 2)
  });

  test("immediately after the signal the tail has exactly recentTurnsKept turns", () => {
    const messages = history(); // 4 turns after the head
    const turnsSummarized = 2;
    const recentTurnsKept = 2;
    const result = reshapeContext(messages, signal(MARKER_TEXT, turnsSummarized, recentTurnsKept));

    // tail = result after the marker
    const tail = result.slice(3);
    const tailTurns = pairTurns(tail);
    assert.equal(tailTurns.length, recentTurnsKept);
  });

  test("keeps new turns added after the signal (incremental safety)", () => {
    const messages = [
      ...history(),
      user("u5"),
      assistant("a5"),
    ];
    // Same signal: 2 folded, 2 kept at signal time. The new turn must survive.
    const result = reshapeContext(messages, signal(MARKER_TEXT, 2, 2));
    // Folded middle (a1,tr1,u2,a2) is excluded …
    assert.ok(!result.includes(messages[2]));
    assert.ok(!result.includes(messages[4]));
    // … while the retained tail and the new turn (u5,a5) are all kept.
    assert.ok(result.includes(messages[6])); // u3
    assert.ok(result.includes(messages[8])); // u4
    assert.ok(result.includes(messages[9])); // a4
    assert.ok(result.includes(messages[10])); // u5
    assert.ok(result.includes(messages[11])); // a5
  });

  test("never splits a turn — tool results stay attached (AC2)", () => {
    const messages = history();
    const result = reshapeContext(messages, signal(MARKER_TEXT, 1, 3));
    // turn 0 = [a1,tr1] is folded; turns 1..3 kept whole.
    // tr1 must be excluded with its assistant, not orphaned.
    assert.ok(!result.includes(messages[3])); // tr1 excluded
    const resultTurns = pairTurns(result.slice(3));
    assert.ok(resultTurns.every((turn) => turn.length >= 1));
    // Each kept turn keeps its own messages intact and contiguous.
    assert.deepEqual(result.slice(3), messages.slice(4));
  });

  test("marker is never constructed — verbatim from the decoded header (AC6)", () => {
    const exotic = "<summary>\n# not parsed · <tags> & specials → ✓\n</summary>";
    const messages = history();
    const result = reshapeContext(messages, signal(exotic, 2, 2));
    const markerMessage = result[2];
    assert.deepEqual(markerMessage, { role: "user", content: exotic });

    // A different marker header yields a different inserted content.
    const other = reshapeContext(messages, signal("DIFFERENT", 2, 2));
    assert.equal(other[2].content, "DIFFERENT");
  });

  test("preserves all system messages ahead of the first user prompt", () => {
    const messages = [system("S1"), system("S2"), ...history().slice(1)];
    const result = reshapeContext(messages, signal(MARKER_TEXT, 2, 2));
    assert.equal(result[0], messages[0]);
    assert.equal(result[1], messages[1]);
    assert.equal(result[2], messages[2]); // first user
    assert.equal(result[3].content, MARKER_TEXT); // marker
  });

  test("is dispatch-only: never mutates the caller's list (AC3)", () => {
    const messages = history();
    const snapshot = JSON.parse(JSON.stringify(messages));
    const before = messages.length;

    const result = reshapeContext(messages, signal(MARKER_TEXT, 2, 2));

    assert.notEqual(result, messages); // a new list
    assert.equal(messages.length, before); // original untouched
    assert.deepEqual(messages, snapshot); // deep-equality preserved
  });

  test("no-op (null) when there is no user message to anchor the head", () => {
    const messages = [system("S"), assistant("a1")];
    assert.equal(reshapeContext(messages, signal("m", 0, 0)), null);
  });

  test("no-op (null) when no signal is present — passthrough (AC7)", () => {
    assert.equal(reshapeContext(history(), null), null);
    assert.equal(reshapeContext(history(), undefined), null);
  });

  test("no-op (null) when the signal folds more turns than exist (anomalous)", () => {
    const messages = history(); // 4 turns after the head
    assert.equal(reshapeContext(messages, signal("m", 5, 0)), null);
  });

  test("handles a recent window of zero (everything folded after the head)", () => {
    const messages = [system("S"), user("u1"), assistant("a1"), user("u2")];
    const result = reshapeContext(messages, signal("m", 2, 0));
    assert.deepEqual(
      result.map((m) => m.role),
      ["system", "user", "user"], // system, u1, marker — no tail
    );
    assert.equal(result[2].content, "m");
  });

  test("a second signal replaces the marker/counts used for the reshape", () => {
    const messages = history();
    const first = reshapeContext(messages, signal("old-marker", 1, 3));
    const second = reshapeContext(messages, signal("new-marker", 3, 1));
    assert.equal(first[2].content, "old-marker");
    assert.equal(second[2].content, "new-marker");
    assert.ok(second.length < first.length); // more folded → shorter tail
  });

  test("empty history is a safe no-op", () => {
    assert.equal(reshapeContext([], signal("m", 0, 0)), null);
  });
});

// ---------------------------------------------------------------------------
// Stateful bridge (AC2/AC7 wiring: per-session, sticky state)
// ---------------------------------------------------------------------------

describe("createCompactionBridge", () => {
  test("no signal → no state, reshape is a passthrough (null)", () => {
    const bridge = createCompactionBridge();
    assert.equal(bridge.handleResponse("s1", undefined), false);
    assert.equal(bridge.hasState("s1"), false);
    assert.equal(bridge.reshape("s1", history()), null);
  });

  test("a well-formed signal is captured and drives the reshape (AC2)", () => {
    const bridge = createCompactionBridge();
    assert.equal(bridge.handleResponse("s1", validHeaders(MARKER_TEXT, 2, 2)), true);
    assert.equal(bridge.hasState("s1"), true);

    const result = bridge.reshape("s1", history());
    assert.equal(result[2].content, MARKER_TEXT);
    assert.deepEqual(
      result.map((m) => m.role),
      ["system", "user", "user", "user", "assistant", "user", "assistant"],
    );
  });

  test("absent/malformed headers keep the previously captured state (no clear)", () => {
    const bridge = createCompactionBridge();
    bridge.handleResponse("s1", validHeaders(MARKER_TEXT, 2, 2));

    assert.equal(bridge.handleResponse("s1", {}), false);
    assert.equal(bridge.handleResponse("s1", { "X-Compaction-Occurred": "false" }), false);
    const malformed = validHeaders();
    malformed["X-Compaction-Marker"] = "%%%not-base64%%%";
    assert.equal(bridge.handleResponse("s1", malformed), false);

    // State survives → mirroring continues.
    assert.equal(bridge.hasState("s1"), true);
    assert.equal(bridge.reshape("s1", history())[2].content, MARKER_TEXT);
  });

  test("state is isolated per session", () => {
    const bridge = createCompactionBridge();
    bridge.handleResponse("a", validHeaders("marker-A", 2, 2));

    assert.equal(bridge.hasState("b"), false);
    assert.equal(bridge.reshape("b", history()), null); // b unaffected by a
    assert.equal(bridge.reshape("a", history())[2].content, "marker-A");
  });

  test("clear drops the session's state", () => {
    const bridge = createCompactionBridge();
    bridge.handleResponse("s1", validHeaders());
    bridge.clear("s1");
    assert.equal(bridge.hasState("s1"), false);
    assert.equal(bridge.reshape("s1", history()), null);
  });

  test("a later signal updates the mirror (marker/counts)", () => {
    const bridge = createCompactionBridge();
    bridge.handleResponse("s1", validHeaders("first", 1, 3));
    bridge.handleResponse("s1", validHeaders("second", 3, 1));
    const result = bridge.reshape("s1", history());
    assert.equal(result[2].content, "second");
  });
});
