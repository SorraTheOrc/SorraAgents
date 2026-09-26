/**
 * Unit tests for pi-client/voice-input/whisper-client.js
 *
 * Acceptance criteria covered (SA-0MUFSJ0D3006AWP8):
 *   - START spawns the worker and waits for READY
 *   - FEED forwards PCM and surfaces partial transcripts
 *   - FINALISE collects all buffered audio and resolves the final transcript
 *   - STOP sends SHUTDOWN, waits for the ACK and exits cleanly
 *   - worker stderr / exit code produces error events and never crashes
 *
 * The Python worker process is mocked: a fake child process with controllable
 * stdin/stdout/stdemit stands in for `python3 whisper_worker.py` — no GPU,
 * model download or faster-whisper install is needed.
 *
 * Run: node --test tests/unit/test-voice-whisper-client.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "whisper-client.js");

const { WhisperClient, createWhisperClient, DEFAULT_WORKER_SCRIPT } = await import(MODULE);

// ---------------------------------------------------------------------------
// Fake worker process
// ---------------------------------------------------------------------------

class FakeWorker extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.stdin = new PassThrough();
    this.commands = [];
    this.killed = [];
    this.stdin.on("data", (chunk) => {
      for (const line of chunk.toString().split("\n")) {
        if (line.trim()) this.commands.push(JSON.parse(line));
      }
    });
  }

  respond(message) {
    this.stdout.write(`${JSON.stringify(message)}\n`);
  }

  kill(signal) {
    this.killed.push(signal);
    return true;
  }
}

/** Create a client wired to a fake worker and return both. */
function harness(options = {}) {
  const worker = new FakeWorker();
  const calls = [];
  const spawn = (command, args, spawnOptions) => {
    calls.push({ command, args, spawnOptions });
    return worker;
  };
  const client = createWhisperClient({ spawn, startupTimeoutMs: 2000, stopTimeoutMs: 2000, ...options });
  const events = { ready: [], partial: [], final: [], stopped: [], error: [], warning: [], stderr: [] };
  for (const name of Object.keys(events)) client.on(name, (payload) => events[name].push(payload));
  return { client, worker, calls, events };
}

/** Let pending stream events flush. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

describe("whisper client startup", () => {
  test("spawns the worker with model/device/compute arguments", async () => {
    const { client, calls, worker } = harness({
      python: "python3",
      model: "small",
      device: "cuda",
      computeType: "float16",
      partialIntervalMs: 1500,
    });

    const ready = client.start();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].command, "python3");
    assert.equal(calls[0].args[0], DEFAULT_WORKER_SCRIPT);
    assert.deepEqual(calls[0].args.slice(1), [
      "--model", "small",
      "--device", "cuda",
      "--compute-type", "float16",
      "--partial-interval-ms", "1500",
    ]);

    await flush();
    assert.deepEqual(worker.commands, [
      {
        type: "start",
        model: "small",
        device: "cuda",
        computeType: "float16",
        partialIntervalMs: 1500,
      },
    ]);

    worker.respond({ type: "ready", model: "small", device: "cuda", computeType: "float16" });
    const info = await ready;
    assert.equal(info.device, "cuda");
    assert.equal(client.getState(), "ready");
  });

  test("passes a configured language through to the worker", async () => {
    const { client, worker, calls } = harness({ language: "en" });
    const ready = client.start();
    assert.ok(calls[0].args.includes("--language"));
    assert.equal(calls[0].args.at(-1), "en");
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;
  });

  test("start is a no-op while already active", async () => {
    const { client, calls, worker } = harness();
    const ready = client.start();
    const second = client.start();
    worker.respond({ type: "ready", model: "small", device: "cuda", computeType: "float16" });
    await Promise.all([ready, second]);
    assert.equal(calls.length, 1);
  });

  test("spawn failure rejects startup with an actionable error and never throws", async () => {
    const client = createWhisperClient({
      spawn: () => {
        throw new Error("spawn python3 ENOENT");
      },
    });
    const errors = [];
    client.on("error", (err) => errors.push(err));

    await assert.rejects(client.start(), /ENOENT/);
    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /faster-whisper|python3/);
    assert.equal(client.getState(), "idle");
  });

  test("worker error message rejects startup and is retained", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({
      type: "error",
      message: "faster-whisper is not installed in the selected Python environment",
    });

    await assert.rejects(ready, /faster-whisper is not installed/);
    assert.equal(events.error.length, 1);
    assert.equal(client.getState(), "idle");
    assert.match(client.lastError.message, /faster-whisper/);
  });
});

// ---------------------------------------------------------------------------
// FEED / partial transcripts
// ---------------------------------------------------------------------------

describe("whisper client feed", () => {
  test("forwards PCM as base64 and emits partial transcripts in order", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    assert.equal(client.feed(Buffer.from([1, 2, 3, 4])), true);
    await flush();
    assert.equal(worker.commands[1].type, "feed");
    assert.equal(worker.commands[1].audio, Buffer.from([1, 2, 3, 4]).toString("base64"));

    worker.respond({ type: "partial", text: "hello", sequence: 1 });
    worker.respond({ type: "partial", text: "hello world", sequence: 2 });
    await flush();

    assert.deepEqual(events.partial, [
      { text: "hello", sequence: 1 },
      { text: "hello world", sequence: 2 },
    ]);
  });

  test("feed before ready is a no-op and does not write", async () => {
    const { client, worker } = harness();
    assert.equal(client.feed(Buffer.from([0])), false);
    await flush();
    assert.deepEqual(worker.commands, []);
  });
});

// ---------------------------------------------------------------------------
// FINALISE
// ---------------------------------------------------------------------------

describe("whisper client finalise", () => {
  test("sends finalise and resolves with the worker's final transcript", async () => {
    const { client, worker } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    const promise = client.finalise();
    await flush();
    assert.equal(worker.commands.at(-1).type, "finalise");
    worker.respond({ type: "final", text: "the final transcript", sequence: 3 });

    assert.equal(await promise, "the final transcript");
    assert.equal(client.getState(), "ready");
  });

  test("finalise when no worker is running resolves to an empty transcript", async () => {
    const client = createWhisperClient({ spawn: () => new FakeWorker() });
    assert.equal(await client.finalise(), "");
  });

  test("a worker error during finalise rejects the promise without throwing", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    const promise = client.finalise();
    worker.respond({ type: "error", message: "transcription failed: boom" });
    await assert.rejects(promise, /transcription failed/);
    assert.equal(events.error.length, 1);
    assert.equal(client.getState(), "ready");
  });
});

// ---------------------------------------------------------------------------
// STOP / shutdown
// ---------------------------------------------------------------------------

describe("whisper client stop", () => {
  test("sends stop, waits for the ACK and marks the client stopped", async () => {
    const { client, worker } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    const stopped = client.stop();
    await flush();
    assert.equal(worker.commands.at(-1).type, "stop");
    worker.respond({ type: "stopped" });

    await stopped;
    assert.equal(client.getState(), "stopped");
    assert.deepEqual(worker.killed, []);
  });

  test("stop times out and force-terminates an unresponsive worker", async () => {
    const { client, worker } = harness({ stopTimeoutMs: 20 });
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    await assert.rejects(client.stop(), /timed out waiting for "stopped"/);
    assert.deepEqual(worker.killed, ["SIGTERM"]);
  });
});

// ---------------------------------------------------------------------------
// Crash / error handling
// ---------------------------------------------------------------------------

describe("whisper client resilience", () => {
  test("an unexpected worker exit surfaces stderr in an actionable error", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    worker.stderr.write("Traceback (most recent call last): OOM");
    worker.emit("exit", 1, null);

    assert.equal(events.error.length, 1);
    assert.match(events.error[0].message, /exited unexpectedly/);
    assert.match(events.error[0].message, /OOM/);
    assert.equal(client.getState(), "idle");
  });

  test("non-JSON worker output is surfaced as stderr, not an error", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;

    worker.stdout.write("some python logging noise\n");
    await flush();
    assert.ok(events.stderr.some((line) => line.includes("python logging noise")));
    assert.equal(events.error.length, 0);
    assert.equal(client.getState(), "ready");
  });

  test("warning messages are surfaced without failing", async () => {
    const { client, worker, events } = harness();
    const ready = client.start();
    worker.respond({ type: "warning", message: "CUDA unavailable; using CPU" });
    worker.respond({ type: "ready", model: "small", device: "cpu", computeType: "int8" });
    await ready;
    assert.deepEqual(events.warning, [{ message: "CUDA unavailable; using CPU" }]);
    assert.equal(events.error.length, 0);
  });
});
