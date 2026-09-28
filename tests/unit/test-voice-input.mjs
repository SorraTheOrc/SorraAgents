/**
 * End-to-end unit test for the voice-input pipeline (SA-0MUFSP9QN005KYWT).
 *
 * Wires the *real* modules together — controller → recorder (with an injected
 * fake capture process) → persistent faster-whisper worker (real Python, stub
 * `faster_whisper` module injected via PYTHONPATH) → pi callbacks — and feeds
 * synthetic PCM directly, so the whole path from audio frames to an
 * auto-submitted user message is exercised without a microphone, GPU or model
 * download.
 *
 * Run: node --test tests/unit/test-voice-input.mjs
 */

import { describe, test, before, after } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const VOICE_DIR = join(__dirname, "..", "..", "pi-client", "voice-input");
const WORKER = join(VOICE_DIR, "whisper_worker.py");

const { createVoiceInputController } = await import(join(VOICE_DIR, "controller.js"));
const { Recorder } = await import(join(VOICE_DIR, "recorder.js"));
const { WhisperClient } = await import(join(VOICE_DIR, "whisper-client.js"));

const STUB = `
class WhisperModel:
    def __init__(self, model_size, device="cpu", compute_type="int8", **kwargs):
        self.device = device

    def transcribe(self, audio, language=None, **kwargs):
        from types import SimpleNamespace
        if len(audio) == 0 or not any(audio):
            return [], SimpleNamespace()
        return [SimpleNamespace(text="[%s] %d samples" % (self.device, len(audio)))], SimpleNamespace()
`;

let stubDir;
const liveChildren = new Set();

before(() => {
  stubDir = mkdtempSync(join(tmpdir(), "voice-input-e2e-"));
  writeFileSync(join(stubDir, "faster_whisper.py"), STUB, "utf8");
});

after(() => {
  for (const child of liveChildren) {
    try {
      child.kill("SIGKILL");
    } catch {
      /* already gone */
    }
  }
  liveChildren.clear();
});

/** A fake arecord process whose stdout we drive from the test. */
class FakeCapture extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.stdin = new PassThrough();
    this.killed = [];
    liveChildren.add(this);
  }

  kill(signal) {
    this.killed.push(signal);
    this.emit("exit", 0, null);
    return true;
  }
}

/** Real whisper-worker spawn with the stub module on PYTHONPATH. */
function whisperSpawn(command, args, options) {
  const child = spawn(command, args, {
    ...options,
    env: { ...process.env, PYTHONPATH: stubDir },
  });
  liveChildren.add(child);
  return child;
}

function makeHarness() {
  const capture = new FakeCapture();
  const ui = {
    editorText: "",
    setEditorText(text) {
      this.editorText = text;
      this.setEditorCalls = this.setEditorCalls || [];
      this.setEditorCalls.push(text);
    },
    getEditorText() {
      return this.editorText;
    },
    statuses: [],
    setStatus(key, text) {
      this.statuses.push([key, text]);
    },
    notifications: [],
    notify(message, level) {
      this.notifications.push({ message, level });
    },
  };
  const userMessages = [];

  const controller = createVoiceInputController({
    config: {
      captureCommand: "arecord",
      captureArgs: ["-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-q"],
      silenceMs: 3000,
      partialCadenceMs: 1000,
      silenceThreshold: 0.01,
      python: process.env.PYTHON || "python3",
      model: "small",
      device: "cpu",
      computeType: "int8",
      language: "",
      workerScript: WORKER,
    },
    createRecorder: (options) => new Recorder({ ...options, spawn: () => capture }),
    createWhisperClient: (options) => new WhisperClient({ ...options, spawn: whisperSpawn }),
    ui,
    sendUserMessage: (text, options) => userMessages.push({ text, options }),
  });

  return { controller, ui, userMessages, capture };
}

/** One 16 kHz mono 16-bit frame of `value` (160 samples = 10 ms). */
function frame(value) {
  const buf = Buffer.alloc(320);
  for (let i = 0; i < 160; i++) buf.writeInt16LE(value, i * 2);
  return buf;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitUntil(predicate, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(20);
  }
  throw new Error("timed out waiting for the pipeline to settle");
}

describe("voice-input end-to-end", () => {
  test("speech then silence auto-submits the transcribed prompt", async () => {
    const { controller, ui, userMessages, capture } = makeHarness();

    await controller.start();
    assert.equal(controller.getState(), "recording");
    assert.ok(ui.statuses.some(([, text]) => /Recording/.test(text ?? "")));

    // 0.5 s of speech followed by exactly 3 s of silence (auto-stop trigger).
    for (let i = 0; i < 50; i++) capture.stdout.write(frame(16384));
    for (let i = 0; i < 300; i++) capture.stdout.write(frame(0));

    await waitUntil(() => userMessages.length === 1);

    // The final transcript is the stub's sample count for all 350 frames.
    assert.match(userMessages[0].text, /\[cpu\] 56000 samples/);
    assert.equal(controller.getState(), "idle");
    assert.equal(ui.editorText, "");
    assert.equal(capture.killed.at(-1), "SIGINT");

    // Live partials reached the editor while recording.
    assert.ok(
      (ui.setEditorCalls || []).some((text) => /samples/.test(text)),
      "expected at least one live partial transcript in the editor",
    );

    await controller.dispose();
  });

  test("a manual stop with no speech does not submit and restores the editor", async () => {
    const { controller, ui, userMessages, capture } = makeHarness();
    ui.editorText = "my draft";

    await controller.start();
    // Only silence, but stop before the 3 s auto-stop threshold.
    for (let i = 0; i < 10; i++) capture.stdout.write(frame(0));
    await controller.stop();

    assert.deepEqual(userMessages, []);
    assert.equal(ui.editorText, "my draft");
    assert.ok(ui.notifications.some((n) => /no speech detected/.test(n.message)));

    await controller.dispose();
  });
});
