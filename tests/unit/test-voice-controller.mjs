/**
 * Unit tests for pi-client/voice-input/controller.js
 *
 * Acceptance criteria covered (SA-0MUFSIB1U0088YEB):
 *   - recording state machine transitions idle → recording → submitting → idle
 *   - Ctrl+Space toggle starts/stops recording at the right times
 *   - partial transcripts replace the editor text in place (no duplication)
 *   - the final transcript (or the last partial) is submitted exactly once
 *   - empty transcripts do not submit; error paths never throw
 *   - the persistent worker loads once per session and is reused
 *
 * The recorder and whisper worker are mocked — no microphone, GPU or pi TUI is
 * required.
 *
 * Run: node --test tests/unit/test-voice-controller.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "controller.js");

const { createVoiceInputController, STATE, STATUS_KEY, statusText } = await import(MODULE);

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

class MockRecorder extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.startCount = 0;
    this.stopCount = 0;
  }

  start() {
    this.startCount += 1;
  }

  stop() {
    this.stopCount += 1;
  }
}

class MockClient extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.startCount = 0;
    this.finaliseCount = 0;
    this.stopCount = 0;
    this.finalText = "";
    this.startError = null;
    this.finalError = null;
    this.deferFinalise = null;
    this.fed = [];
  }

  async start() {
    this.startCount += 1;
    if (this.startError) throw this.startError;
    this.emit("ready", {});
    return { device: "cpu" };
  }

  feed(pcm) {
    this.fed.push(pcm);
  }

  async finalise() {
    this.finaliseCount += 1;
    if (this.deferFinalise) return this.deferFinalise.promise;
    if (this.finalError) throw this.finalError;
    return this.finalText;
  }

  async stop() {
    this.stopCount += 1;
  }
}

function harness(overrides = {}) {
  const recorders = [];
  const clients = [];
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
      captureArgs: ["-f", "S16_LE"],
      silenceMs: 3000,
      partialCadenceMs: 1000,
      silenceThreshold: 0.01,
      python: "python3",
      model: "small",
      device: "cuda",
      computeType: "float16",
      language: "",
      workerScript: "",
    },
    createRecorder: (options) => {
      const recorder = new MockRecorder(options);
      if (overrides.configureRecorder) overrides.configureRecorder(recorder);
      recorders.push(recorder);
      return recorder;
    },
    createWhisperClient: (options) => {
      const client = new MockClient(options);
      if (overrides.configureClient) overrides.configureClient(client);
      clients.push(client);
      return client;
    },
    ui,
    sendUserMessage: (text, options) => userMessages.push({ text, options }),
    ...overrides,
  });

  return { controller, ui, userMessages, recorders, clients };
}

/** Let queued microtasks settle. */
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

// ---------------------------------------------------------------------------
// State machine / toggle
// ---------------------------------------------------------------------------

describe("voice controller state machine", () => {
  test("toggle starts recording from idle", async () => {
    const { controller, ui, recorders, clients } = harness();
    assert.equal(controller.getState(), STATE.idle);

    await controller.toggle();

    assert.equal(controller.getState(), STATE.recording);
    assert.equal(recorders.length, 1);
    assert.equal(recorders[0].startCount, 1);
    assert.equal(clients.length, 1);
    assert.equal(clients[0].startCount, 1);
    assert.deepEqual(ui.statuses.at(-1), [STATUS_KEY, statusText(STATE.recording)]);
  });

  test("toggle again stops, finalises and returns to idle", async () => {
    const { controller, ui, userMessages, clients } = harness();
    await controller.toggle();
    clients[0].finalText = "hello world";

    await controller.toggle();

    assert.equal(clients[0].finaliseCount, 1);
    assert.equal(controller.getState(), STATE.idle);
    assert.deepEqual(userMessages, [{ text: "hello world", options: undefined }]);
    assert.equal(ui.statuses.at(-1)[1], undefined);
  });

  test("toggle while submitting is ignored", async () => {
    const { controller, clients, userMessages } = harness();
    await controller.toggle();
    clients[0].finalText = "once";
    // Start stop() without awaiting so the controller is mid-submit.
    const stopping = controller.toggle();
    await controller.toggle(); // must be a no-op
    await stopping;

    assert.equal(clients[0].finaliseCount, 1);
    assert.equal(userMessages.length, 1);
  });

  test("the persistent worker is started once and reused across recordings", async () => {
    const { controller, clients } = harness();
    await controller.toggle();
    await controller.toggle();
    await controller.toggle();
    await controller.toggle();

    assert.equal(clients.length, 1);
    assert.equal(clients[0].startCount, 1);
    assert.equal(clients[0].stopCount, 0);
  });

  test("dispose stops the persistent worker", async () => {
    const { controller, clients } = harness();
    await controller.toggle();
    await controller.dispose();
    assert.equal(clients[0].stopCount, 1);
    assert.equal(controller.getState(), STATE.idle);
  });
});

// ---------------------------------------------------------------------------
// Partial transcript replacement
// ---------------------------------------------------------------------------

describe("voice controller partials", () => {
  test("each partial replaces the editor text in place", async () => {
    const { controller, ui, clients } = harness();
    await controller.toggle();
    ui.setEditorCalls = [];

    clients[0].emit("partial", { text: "hello", sequence: 1 });
    clients[0].emit("partial", { text: "hello world", sequence: 2 });

    assert.deepEqual(ui.setEditorCalls, ["hello", "hello world"]);
    assert.equal(ui.editorText, "hello world");
    assert.equal(controller.getTranscript(), "hello world");
  });

  test("an identical partial does not re-write the editor", async () => {
    const { controller, ui, clients } = harness();
    await controller.toggle();
    ui.setEditorCalls = [];

    clients[0].emit("partial", { text: "same", sequence: 1 });
    clients[0].emit("partial", { text: "same", sequence: 2 });

    assert.deepEqual(ui.setEditorCalls, ["same"]);
  });

  test("partials are ignored once the recording is no longer active", async () => {
    const { controller, ui, clients } = harness();
    await controller.toggle();
    clients[0].finalText = "done";
    await controller.toggle();
    ui.setEditorCalls = [];

    clients[0].emit("partial", { text: "late partial" });
    assert.deepEqual(ui.setEditorCalls, []);
  });
});

// ---------------------------------------------------------------------------
// Submission semantics
// ---------------------------------------------------------------------------

describe("voice controller submission", () => {
  test("falls back to the last partial when finalise returns nothing", async () => {
    const { controller, userMessages, clients } = harness();
    await controller.toggle();
    clients[0].emit("partial", { text: "from partial" });
    clients[0].finalText = "";

    await controller.stop();
    assert.deepEqual(userMessages, [{ text: "from partial", options: undefined }]);
  });

  test("the editor is cleared after a successful submit", async () => {
    const { controller, ui, clients } = harness();
    await controller.toggle();
    clients[0].finalText = "send me";
    await controller.stop();
    assert.equal(ui.editorText, "");
    assert.equal(ui.setEditorCalls.at(-1), "");
  });

  test("an empty transcript is not submitted and the editor is restored", async () => {
    const { controller, ui, userMessages, clients } = harness();
    ui.editorText = "pre-existing draft";
    await controller.toggle();
    clients[0].finalText = "";

    await controller.stop();

    assert.deepEqual(userMessages, []);
    assert.equal(ui.editorText, "pre-existing draft");
    assert.equal(controller.getState(), STATE.idle);
    assert.ok(ui.notifications.some((n) => /no speech detected/.test(n.message)));
  });

  test("a busy pi receives the transcript as a steering message", async () => {
    const { controller, userMessages, clients } = harness({ isIdle: () => false });
    await controller.toggle();
    clients[0].finalText = "steer this";
    await controller.stop();
    assert.deepEqual(userMessages, [{ text: "steer this", options: { deliverAs: "steer" } }]);
  });

  test("stop is idempotent: two rapid stops submit exactly one message", async () => {
    const { controller, userMessages, clients } = harness();
    await controller.toggle();
    clients[0].finalText = "only once";

    await Promise.all([controller.stop(), controller.stop()]);

    assert.equal(clients[0].finaliseCount, 1);
    assert.equal(userMessages.length, 1);
  });
});

// ---------------------------------------------------------------------------
// Silence auto-stop
// ---------------------------------------------------------------------------

describe("voice controller silence auto-stop", () => {
  test("a silence event auto-submits the transcript", async () => {
    const { controller, recorders, userMessages, clients } = harness();
    await controller.toggle();
    clients[0].finalText = "auto submitted";

    recorders[0].emit("silence", { at: 3000, silentMs: 3000 });
    await flush();

    assert.equal(controller.getState(), STATE.idle);
    assert.equal(recorders[0].stopCount, 1);
    assert.deepEqual(userMessages, [{ text: "auto submitted", options: undefined }]);
  });
});

// ---------------------------------------------------------------------------
// Doctor preflight gating
// ---------------------------------------------------------------------------

describe("voice controller doctor gating", () => {
  test("critical doctor errors block recording and notify with the remedy", async () => {
    let doctorCalls = 0;
    const { controller, ui, recorders, clients } = harness({
      checkDoctor: () => {
        doctorCalls += 1;
        return {
          ok: false,
          hasErrors: true,
          hasWarnings: false,
          checks: [
            {
              id: "faster-whisper",
              status: "error",
              message: "faster-whisper is not importable",
              remedy: "pip install faster-whisper",
            },
          ],
        };
      },
    });

    await controller.toggle();

    assert.equal(controller.getState(), STATE.idle);
    assert.equal(recorders.length, 0);
    assert.equal(clients.length, 0);
    assert.ok(ui.notifications.some((n) => n.level === "error" && /pip install faster-whisper/.test(n.message)));

    // The check is cached: toggling again must not re-run it.
    await controller.toggle();
    assert.equal(doctorCalls, 1);
  });

  test("doctor warnings are surfaced but do not block recording", async () => {
    const { controller, ui, recorders } = harness({
      checkDoctor: () => ({
        ok: true,
        hasErrors: false,
        hasWarnings: true,
        checks: [{ id: "cuda", status: "warning", message: "CUDA unavailable", remedy: "using CPU" }],
      }),
    });

    await controller.toggle();
    assert.equal(controller.getState(), STATE.recording);
    assert.equal(recorders.length, 1);
    assert.ok(ui.notifications.some((n) => n.level === "warning" && /CUDA unavailable/.test(n.message)));
  });
});

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

describe("voice controller defaults", () => {
  test("statusText renders the recording and submitting indicators", () => {
    assert.match(statusText(STATE.recording), /Recording/);
    assert.match(statusText(STATE.submitting), /Transcribing/);
    assert.equal(statusText(STATE.idle), undefined);
  });
});

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

describe("voice controller errors", () => {
  test("a recorder error aborts, notifies and never throws", async () => {
    const { controller, ui, recorders } = harness();
    await controller.toggle();

    assert.doesNotThrow(() => recorders[0].emit("error", new Error("arecord failed")));
    await flush();

    assert.equal(controller.getState(), STATE.idle);
    assert.equal(recorders[0].stopCount, 1);
    assert.ok(ui.notifications.some((n) => n.level === "error" && /arecord failed/.test(n.message)));
  });

  test("a whisper client startup failure aborts before capture starts", async () => {
    const { controller, ui, recorders } = harness({
      configureClient: (client) => {
        client.startError = new Error("faster-whisper missing");
      },
    });

    await controller.start();
    await flush();

    assert.equal(controller.getState(), STATE.idle);
    assert.ok(ui.notifications.some((n) => /faster-whisper missing/.test(n.message)));
    assert.equal(recorders.length, 0);
  });

  test("a finalise failure aborts and does not submit", async () => {
    const { controller, ui, userMessages, clients } = harness({
      configureClient: (client) => {
        client.finalError = new Error("transcription exploded");
      },
    });
    await controller.toggle();
    await controller.stop();

    assert.equal(clients[0].finaliseCount, 1);
    assert.equal(controller.getState(), STATE.idle);
    assert.deepEqual(userMessages, []);
    assert.ok(ui.notifications.some((n) => /transcription exploded/.test(n.message)));
  });

  test("a client error event during recording aborts and restores the editor", async () => {
    const { controller, ui, clients } = harness();
    ui.editorText = "draft";
    await controller.toggle();
    clients[0].emit("partial", { text: "half" });

    assert.doesNotThrow(() => clients[0].emit("error", new Error("worker died")));
    await flush();

    assert.equal(controller.getState(), STATE.idle);
    assert.equal(ui.editorText, "draft");
  });
});
