/**
 * Unit tests for pi-client/proxy-sse-signals/sse-signals.js
 *
 * Acceptance criteria covered (SA-0MSHAKSEA001LQ6T):
 *   AC1 — SSE comment lines (": ") are surfaced visibly instead of discarded.
 *   AC2 — Signals are metadata: the assistant content stream excludes comment
 *         lines while the signal is emitted separately.
 *   AC3 — Chain-hold feedback comments display the retry/countdown info.
 *   AC4 — No regression: bytes pass through the capturing fetch unchanged.
 *
 * Run: node --test tests/unit/test-proxy-sse-signals.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "proxy-sse-signals", "sse-signals.js");

const {
  isSseCommentLine,
  commentText,
  parseSignal,
  formatSignal,
  createSignalExtractor,
  createCapturingFetch,
} = await import(MODULE);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const encoder = new TextEncoder();

/** Build an SSE Response whose body delivers `chunks` of raw SSE text. */
function sseResponse(chunks, { contentType = "text/event-stream", status = 200 } = {}) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status, headers: { "content-type": contentType } },
  );
}

/** Read a Response body to a string. */
async function bodyText(response) {
  return new Response(await response.arrayBuffer()).text();
}

// ---------------------------------------------------------------------------
// Line classification (AC1/AC2 foundations)
// ---------------------------------------------------------------------------

describe("comment line detection", () => {
  test("recognises ': ' prefixed SSE comment lines", () => {
    assert.equal(isSseCommentLine(": re-route provider=a->b reason=stall_after_reasoning"), true);
    assert.equal(isSseCommentLine(": chain exhausted (busy); retrying from local-qwen3 in 240s"), true);
  });

  test("rejects data and empty lines", () => {
    assert.equal(isSseCommentLine('data: {"choices":[]}'), false);
    assert.equal(isSseCommentLine(""), false);
    assert.equal(isSseCommentLine("event: done"), false);
  });

  test("commentText strips the colon and optional space", () => {
    assert.equal(commentText(": re-route provider=a->b"), "re-route provider=a->b");
    assert.equal(commentText(":re-route provider=a->b"), "re-route provider=a->b");
    assert.equal(commentText(": re-route x\r"), "re-route x"); // CRLF line ending
    assert.equal(commentText("data: x"), null);
  });
});

// ---------------------------------------------------------------------------
// AC1 — comments surfaced (re-route signal)
// ---------------------------------------------------------------------------

describe("re-route signal surfacing (AC1)", () => {
  test("extractor detects a synthetic re-route comment chunk", () => {
    const extractor = createSignalExtractor();
    const signals = extractor.push(': re-route provider=opencode-go->opencode-go-2 reason=stall_after_reasoning\ndata: {"choices":[]}\n');
    assert.deepEqual(signals, ["re-route provider=opencode-go->opencode-go-2 reason=stall_after_reasoning"]);
  });

  test("formatSignal renders a visible re-route status line", () => {
    const signal = formatSignal("re-route provider=opencode-go->opencode-go-2 reason=stall_after_reasoning");
    assert.equal(signal, "re-route: opencode-go → opencode-go-2 · stall_after_reasoning");
  });

  test("re-route without a reason still renders", () => {
    assert.equal(formatSignal("re-route provider=a->b"), "re-route: a → b");
  });

  test("capturing fetch surfaces the signal to the callback (end-to-end)", async () => {
    const received = [];
    const capturingFetch = createCapturingFetch(
      async () =>
        sseResponse([
          ": re-route provider=opencode-go->opencode-go-2 reason=stall_after_reasoning\n",
          'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
          "data: [DONE]\n",
        ]),
      (signal) => received.push(signal),
    );
    const response = await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" });
    await bodyText(response);
    assert.deepEqual(received, ["re-route: opencode-go → opencode-go-2 · stall_after_reasoning"]);
  });
});

// ---------------------------------------------------------------------------
// AC2 — signals are metadata, separate from the assistant transcript
// ---------------------------------------------------------------------------

describe("transcript purity (AC2)", () => {
  test("comment lines never leak into assistant content from data lines", async () => {
    const received = [];
    const capturingFetch = createCapturingFetch(
      async () =>
        sseResponse([
          ": re-route provider=a->b reason=stall_after_reasoning\n",
          'data: {"choices":[{"delta":{"content":"Assistant"}}]}\n',
          'data: {"choices":[{"delta":{"content":" reply"}}]}\n',
          "data: [DONE]\n",
        ]),
      (signal) => received.push(signal),
    );
    const response = await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" });

    // The consumer (provider SDK) receives the body byte-for-byte: comment and
    // data lines are both present in the raw stream...
    const raw = await bodyText(response);
    assert.ok(raw.includes("re-route provider=a->b"));

    // ...but the assistant content, derived solely from data: payloads, never
    // contains the comment text — while the signal is emitted separately.
    const assistantContent = [...raw.matchAll(/"content":"([^"]*)"/g)].map((m) => m[1]).join("");
    assert.equal(assistantContent, "Assistant reply");
    assert.ok(!assistantContent.includes("re-route"));
    assert.deepEqual(received, ["re-route: a → b · stall_after_reasoning"]);
  });

  test("extractor separates comments from data lines", () => {
    const extractor = createSignalExtractor();
    extractor.push(": re-route provider=a->b\n");
    const signals = extractor.push('data: {"choices":[{"delta":{"content":"clean"}}]}\n');
    assert.deepEqual(signals, []); // data lines yield no signals
    assert.equal(commentText('data: {"choices":[]}'), null);
  });
});

// ---------------------------------------------------------------------------
// AC3 — chain-hold feedback (countdown/retry info)
// ---------------------------------------------------------------------------

describe("chain-hold feedback (AC3)", () => {
  test("formats a synthetic hold comment with model and countdown", () => {
    const signal = formatSignal("chain exhausted (all local slots busy); retrying from local-qwen3 in 240s");
    assert.equal(signal, "chain exhausted — retrying from local-qwen3 in 240s");
  });

  test("handles seconds without trailing 's' unit", () => {
    assert.equal(formatSignal("chain exhausted; retrying from local-qwen3 in 300 s"), "chain exhausted — retrying from local-qwen3 in 300s");
  });

  test("renders partial hold info when fields are missing", () => {
    assert.equal(formatSignal("chain exhausted; retrying from local-qwen3"), "chain exhausted — retrying from local-qwen3");
    assert.equal(formatSignal("chain exhausted"), "chain exhausted — retrying");
  });

  test("capturing fetch surfaces the hold signal end-to-end", async () => {
    const received = [];
    const capturingFetch = createCapturingFetch(
      async () => sseResponse([": chain exhausted (busy); retrying from local-qwen3 in 240s\n"]),
      (signal) => received.push(signal),
    );
    await bodyText(await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" }));
    assert.deepEqual(received, ["chain exhausted — retrying from local-qwen3 in 240s"]);
  });
});

// ---------------------------------------------------------------------------
// AC4 — no regression: passthrough and non-SSE responses
// ---------------------------------------------------------------------------

describe("passthrough safety (AC4)", () => {
  test("capturing fetch forwards SSE bytes unchanged", async () => {
    const chunks = [
      "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n",
      ": re-route provider=a->b\n",
      "data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n",
      "data: [DONE]\n",
    ];
    const capturingFetch = createCapturingFetch(
      async () => sseResponse(chunks),
      () => {},
    );
    const response = await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" });
    assert.equal(await bodyText(response), chunks.join(""));
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "text/event-stream");
  });

  test("non event-stream responses pass through without capture", async () => {
    const jsonBody = JSON.stringify({ error: { message: "nope" } });
    const capturingFetch = createCapturingFetch(
      async () =>
        new Response(jsonBody, {
          status: 429,
          headers: { "content-type": "application/json" },
        }),
      () => assert.fail("no signals expected from JSON error body"),
    );
    const response = await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" });
    assert.equal(response.status, 429);
    assert.equal(await bodyText(response), jsonBody);
  });

  test("comments split across chunk boundaries are still detected", async () => {
    const received = [];
    const capturingFetch = createCapturingFetch(
      async () =>
        sseResponse([
          ": re-route provider=opencode-g",
          "o->opencode-go-2 reason=stall_after_reasoning\n",
          'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
        ]),
      (signal) => received.push(signal),
    );
    await bodyText(await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" }));
    assert.deepEqual(received, ["re-route: opencode-go → opencode-go-2 · stall_after_reasoning"]);
  });

  test("flush() surfaces a trailing comment without a newline", async () => {
    const received = [];
    const capturingFetch = createCapturingFetch(
      async () => sseResponse([": chain exhausted; retrying from local-qwen3 in 240s"]),
      (signal) => received.push(signal),
    );
    await bodyText(await capturingFetch("http://proxy/v1/chat/completions", { method: "POST" }));
    assert.deepEqual(received, ["chain exhausted — retrying from local-qwen3 in 240s"]);
  });

  test("unrecognised comments are surfaced verbatim (nothing swallowed)", () => {
    assert.equal(formatSignal("custom proxy note"), "custom proxy note");
  });
});
