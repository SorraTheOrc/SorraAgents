/**
 * Unit tests for pi-client/voice-input/whisper_worker.py
 *
 * Acceptance criteria covered (SA-0MUFSJ0D3006AWP8):
 *   - the worker speaks the documented line-delimited JSON protocol
 *   - START loads the model once and reports READY
 *   - FEED accumulates audio and emits PARTIAL on the configured cadence
 *   - FINALISE transcribes everything buffered and emits FINAL
 *   - STOP acknowledges with STOPPED and exits cleanly
 *   - a CUDA initialisation failure falls back to CPU int8
 *   - a missing faster-whisper install produces an actionable error, not a crash
 *
 * The real Python worker is exercised end-to-end. A stub `faster_whisper`
 * module is injected via PYTHONPATH, so no GPU, model download or
 * faster-whisper install is required.
 *
 * Run: node --test tests/unit/test-voice-whisper-worker.mjs
 */

import { describe, test, before } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKER = join(__dirname, "..", "..", "pi-client", "voice-input", "whisper_worker.py");
const PYTHON = process.env.PYTHON || "python3";

/** Deterministic stub replacement for faster-whisper (no CUDA, no model). */
const STUB = `
class WhisperModel:
    def __init__(self, model_size, device="cpu", compute_type="int8", **kwargs):
        if device == "cuda":
            raise RuntimeError("CUDA driver not available in test stub")
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type

    def transcribe(self, audio, language=None, **kwargs):
        from types import SimpleNamespace
        text = "[%s] %d samples" % (self.device, len(audio))
        return [SimpleNamespace(text=text)], SimpleNamespace()
`;

let stubDir;

before(() => {
  stubDir = mkdtempSync(join(tmpdir(), "voice-whisper-stub-"));
  writeFileSync(join(stubDir, "faster_whisper.py"), STUB, "utf8");
  // An empty dir used to simulate faster-whisper being absent.
  const empty = join(stubDir, "empty");
  mkdirSync(empty, { recursive: true });
  stubDir = { stub: stubDir, empty };
});

/** Spawn the real worker and expose a message queue. */
class WorkerHarness {
  constructor(args = [], { pythonPath } = {}) {
    const env = { ...process.env };
    if (pythonPath !== undefined) env.PYTHONPATH = pythonPath;
    this.child = spawn(PYTHON, [WORKER, ...args], {
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.messages = [];
    this.stderr = "";
    this.readline = createInterface({ input: this.child.stdout });
    this.readline.on("line", (line) => {
      try {
        this.messages.push(JSON.parse(line));
      } catch {
        // ignore non-JSON noise
      }
    });
    this.child.stderr.on("data", (chunk) => {
      this.stderr += chunk.toString();
    });
    this.exit = new Promise((resolve) => {
      this.child.on("exit", (code, signal) => resolve({ code, signal }));
    });
  }

  send(message) {
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  async waitFor(type, timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const found = this.messages.find((m) => m.type === type);
      if (found) return found;
      if (this.child.exitCode !== null) break;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    throw new Error(
      `timed out waiting for "${type}". messages=${JSON.stringify(this.messages)} stderr=${this.stderr}`,
    );
  }
}

/** Little-endian int16 mono PCM of `samples` zero samples. */
function pcm(samples) {
  return Buffer.alloc(samples * 2);
}

// ---------------------------------------------------------------------------
// Full lifecycle
// ---------------------------------------------------------------------------

describe("whisper worker protocol", () => {
  test("start -> ready -> feed -> partial -> finalise -> stop", async () => {
    const worker = new WorkerHarness([
      "--model", "small",
      "--device", "cpu",
      "--compute-type", "int8",
      "--partial-interval-ms", "10",
    ], { pythonPath: stubDir.stub });

    worker.send({ type: "start", model: "small", device: "cpu", computeType: "int8" });
    const ready = await worker.waitFor("ready");
    assert.equal(ready.device, "cpu");
    assert.equal(ready.model, "small");
    assert.equal(ready.computeType, "int8");

    worker.send({ type: "feed", audio: pcm(1600).toString("base64") });
    const partial = await worker.waitFor("partial");
    assert.equal(partial.sequence, 1);
    assert.match(partial.text, /\[cpu\] 1600 samples/);

    worker.send({ type: "finalise" });
    const final = await worker.waitFor("final");
    assert.equal(final.sequence, 2);
    assert.match(final.text, /\[cpu\] 1600 samples/);

    worker.send({ type: "stop" });
    await worker.waitFor("stopped");
    const { code } = await worker.exit;
    assert.equal(code, 0);
  });

  test("finalise without any audio returns an empty transcript", async () => {
    const worker = new WorkerHarness(["--device", "cpu"], { pythonPath: stubDir.stub });
    worker.send({ type: "start", device: "cpu" });
    await worker.waitFor("ready");

    worker.send({ type: "finalise" });
    const final = await worker.waitFor("final");
    assert.equal(final.text, "");

    worker.send({ type: "stop" });
    await worker.exit;
  });

  test("partials are emitted on the configured cadence, not per frame", async () => {
    const worker = new WorkerHarness(["--device", "cpu", "--partial-interval-ms", "1000"], {
      pythonPath: stubDir.stub,
    });
    worker.send({ type: "start", device: "cpu" });
    await worker.waitFor("ready");

    // 0.5 s of audio: below the 1 s cadence -> no partial yet.
    worker.send({ type: "feed", audio: pcm(8000).toString("base64") });
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.equal(worker.messages.filter((m) => m.type === "partial").length, 0);

    // Another 0.6 s crosses the cadence -> exactly one partial.
    worker.send({ type: "feed", audio: pcm(9600).toString("base64") });
    const partial = await worker.waitFor("partial");
    assert.equal(partial.sequence, 1);

    worker.send({ type: "stop" });
    await worker.exit;
  });
});

// ---------------------------------------------------------------------------
// CUDA fallback
// ---------------------------------------------------------------------------

describe("whisper worker CUDA fallback", () => {
  test("falls back to CPU int8 when CUDA init fails and reports the effective device", async () => {
    const worker = new WorkerHarness(["--model", "small", "--device", "cuda", "--compute-type", "float16"], {
      pythonPath: stubDir.stub,
    });
    worker.send({ type: "start", model: "small", device: "cuda", computeType: "float16" });

    const warning = await worker.waitFor("warning");
    assert.match(warning.message, /CUDA device unavailable/);

    const ready = await worker.waitFor("ready");
    assert.equal(ready.device, "cpu");
    assert.equal(ready.computeType, "int8");

    worker.send({ type: "stop" });
    await worker.exit;
  });
});

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

describe("whisper worker error handling", () => {
  test("a missing faster-whisper install emits an actionable error and exits non-zero", async () => {
    const worker = new WorkerHarness(["--device", "cpu"], { pythonPath: stubDir.empty });
    worker.send({ type: "start", device: "cpu" });

    const error = await worker.waitFor("error");
    assert.match(error.message, /faster-whisper is not installed/);

    const { code } = await worker.exit;
    assert.equal(code, 1);
  });

  test("invalid JSON produces an error and the worker keeps running", async () => {
    const worker = new WorkerHarness(["--device", "cpu"], { pythonPath: stubDir.stub });
    worker.child.stdin.write("not json\n");
    const error = await worker.waitFor("error");
    assert.match(error.message, /invalid JSON/);

    // Still responsive afterwards.
    worker.send({ type: "start", device: "cpu" });
    await worker.waitFor("ready");
    worker.send({ type: "stop" });
    await worker.exit;
  });

  test("feed before start is rejected without crashing", async () => {
    const worker = new WorkerHarness(["--device", "cpu"], { pythonPath: stubDir.stub });
    worker.send({ type: "feed", audio: pcm(160).toString("base64") });
    const error = await worker.waitFor("error");
    assert.match(error.message, /before 'start'/);

    worker.send({ type: "stop" });
    await worker.exit;
  });

  test("invalid base64 audio is reported and the worker keeps running", async () => {
    const worker = new WorkerHarness(["--device", "cpu"], { pythonPath: stubDir.stub });
    worker.send({ type: "start", device: "cpu" });
    await worker.waitFor("ready");

    worker.send({ type: "feed", audio: "!!!not-base64!!!" });
    const error = await worker.waitFor("error");
    assert.match(error.message, /invalid base64/);

    worker.send({ type: "finalise" });
    await worker.waitFor("final");
    worker.send({ type: "stop" });
    await worker.exit;
  });
});
