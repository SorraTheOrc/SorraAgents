/**
 * Unit tests for pi-client/voice-input/recorder.js
 *
 * Acceptance criteria covered (SA-0MUFSIOFC005C93J):
 *   - RMS computation over known 16-bit samples
 *   - PCM framing across arbitrary chunk boundaries
 *   - silence fires after exactly the configured idle duration
 *   - speech resets the silence timer
 *   - pause events fire on the partial-transcription cadence
 *   - capture-process failures surface actionable errors and never throw
 *
 * No real microphone or capture backend is required: synthetic PCM is fed to
 * the recorder and `spawn` is replaced with a fake child process.
 *
 * Run: node --test tests/unit/test-voice-recorder.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "recorder.js");

const {
  computeRms,
  frameBytesFor,
  PcmFramer,
  SilenceDetector,
  captureErrorMessage,
  Recorder,
  createRecorder,
  DEFAULT_CAPTURE_COMMAND,
  DEFAULT_CAPTURE_ARGS,
  SAMPLE_RATE,
} = await import(MODULE);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A fake child process with a writable stdout/stderr we control. */
class FakeChild extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.killSignal = null;
  }

  kill(signal) {
    this.killSignal = signal;
    return true;
  }
}

/** Fake spawn capturing the invocation and exposing the fake child. */
function fakeSpawn() {
  const calls = [];
  const children = [];
  const spawn = (command, args, options) => {
    const child = new FakeChild();
    calls.push({ command, args, options });
    children.push(child);
    return child;
  };
  return { spawn, calls, children };
}

/** One constant-amplitude 16-bit mono PCM frame of `samples` samples. */
function pcm(value, samples = 160) {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) buf.writeInt16LE(value, i * 2);
  return buf;
}

/** Feed `count` frames of `value` into a recorder (one frame per 10 ms). */
function feedFrames(recorder, value, count, samples = 160) {
  const frame = pcm(value, samples);
  for (let i = 0; i < count; i++) recorder.handleChunk(frame);
}

function collect(recorder, event) {
  const seen = [];
  recorder.on(event, (payload) => seen.push(payload));
  return seen;
}

// ---------------------------------------------------------------------------
// RMS computation
// ---------------------------------------------------------------------------

describe("computeRms", () => {
  test("digital silence yields zero energy", () => {
    assert.equal(computeRms(pcm(0)), 0);
    assert.equal(computeRms(Buffer.alloc(0)), 0);
  });

  test("full-scale positive and negative samples both yield ~1", () => {
    assert.ok(Math.abs(computeRms(pcm(32767)) - 1) < 1e-3);
    assert.ok(Math.abs(computeRms(pcm(-32768)) - 1) < 1e-3);
  });

  test("constant half-scale amplitude yields 0.5", () => {
    assert.ok(Math.abs(computeRms(pcm(16384)) - 0.5) < 1e-3);
  });

  test("energy is amplitude-independent of sample count", () => {
    assert.equal(computeRms(pcm(32767, 32)), computeRms(pcm(32767, 400)));
  });
});

// ---------------------------------------------------------------------------
// PCM framing
// ---------------------------------------------------------------------------

describe("PcmFramer", () => {
  test("splits a large chunk into exact fixed-size frames", () => {
    const framer = new PcmFramer(320);
    const frames = framer.push(Buffer.alloc(960, 1));
    assert.equal(frames.length, 3);
    assert.ok(frames.every((f) => f.length === 320));
  });

  test("reassembles frames across arbitrary chunk boundaries", () => {
    const framer = new PcmFramer(320);
    assert.equal(framer.push(Buffer.alloc(100, 1)).length, 0);
    assert.equal(framer.push(Buffer.alloc(100, 1)).length, 0);
    assert.equal(framer.push(Buffer.alloc(200, 1)).length, 1);
  });

  test("flush returns a short trailing frame and then nothing", () => {
    const framer = new PcmFramer(320);
    framer.push(Buffer.alloc(400, 1));
    assert.equal(framer.flush().length, 80);
    assert.equal(framer.flush(), null);
  });

  test("frame size matches 10 ms at 16 kHz mono 16-bit", () => {
    assert.equal(frameBytesFor(10), 320);
    assert.equal(frameBytesFor(1000), 32000);
  });

  test("rejects invalid frame sizes", () => {
    assert.throws(() => new PcmFramer(1), /invalid frame size/);
  });
});

// ---------------------------------------------------------------------------
// Silence / pause detection (deterministic audio timeline)
// ---------------------------------------------------------------------------

describe("SilenceDetector", () => {
  test("fires silence exactly once after the configured idle duration", () => {
    const detector = new SilenceDetector({ silenceMs: 3000, partialCadenceMs: 0 });
    const events = [];
    for (let at = 10; at <= 3000; at += 10) {
      events.push(...detector.push(0, at));
    }
    assert.deepEqual(
      events.map((e) => e.type),
      ["silence"],
    );
    assert.equal(events[0].at, 3000);
    assert.equal(events[0].silentMs, 3000);

    // Further silence must not re-fire the end-of-speech event.
    assert.deepEqual(detector.push(0, 3010), []);
  });

  test("speech resets the silence timer", () => {
    const detector = new SilenceDetector({ silenceMs: 3000, partialCadenceMs: 0 });
    // 2 s silence, a speech frame, then more silence.
    let events = [];
    for (let at = 10; at <= 2000; at += 10) events = events.concat(detector.push(0, at));
    assert.deepEqual(events, []);
    detector.push(1, 2010);
    // Silence run now measured from 2010 -> fires at 5010.
    events = [];
    for (let at = 2020; at < 5010; at += 10) events = events.concat(detector.push(0, at));
    assert.deepEqual(events, []);
    assert.deepEqual(detector.push(0, 5010).map((e) => e.at), [5010]);
  });

  test("emits pause events on the partial-transcription cadence", () => {
    const detector = new SilenceDetector({
      silenceMs: 3000,
      partialCadenceMs: 1000,
    });
    const pauses = [];
    for (let at = 10; at <= 3000; at += 10) {
      for (const event of detector.push(0, at)) {
        if (event.type === "pause") pauses.push(event.at);
      }
    }
    assert.deepEqual(pauses, [1000, 2000, 3000]);
  });

  test("loud audio is never classified as silence", () => {
    const detector = new SilenceDetector({ silenceMs: 1000, partialCadenceMs: 500 });
    let events = [];
    for (let at = 10; at <= 10000; at += 10) events = events.concat(detector.push(0.5, at));
    assert.deepEqual(events, []);
  });
});

// ---------------------------------------------------------------------------
// Recorder: capture process lifecycle
// ---------------------------------------------------------------------------

describe("Recorder capture process", () => {
  test("starts the configured capture command with PCM stdout", () => {
    const { spawn, calls } = fakeSpawn();
    const recorder = createRecorder({ spawn });
    const starts = collect(recorder, "start");
    recorder.start();

    assert.equal(recorder.getState(), "recording");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].command, DEFAULT_CAPTURE_COMMAND);
    assert.deepEqual(calls[0].args, DEFAULT_CAPTURE_ARGS);
    assert.equal(starts.length, 1);
  });

  test("a custom capture command overrides the default", () => {
    const { spawn, calls } = fakeSpawn();
    const recorder = createRecorder({
      spawn,
      command: "parecord",
      args: ["--format=s16le", "--rate=16000", "--channels=1"],
    });
    recorder.start();
    assert.equal(calls[0].command, "parecord");
    assert.deepEqual(calls[0].args, ["--format=s16le", "--rate=16000", "--channels=1"]);
  });

  test("emits framed data with RMS and an audio timestamp", () => {
    const { spawn, children } = fakeSpawn();
    const recorder = createRecorder({ spawn });
    const data = collect(recorder, "data");
    recorder.start();

    children[0].stdout.write(pcm(32767, 160));
    children[0].stdout.write(pcm(0, 160));

    assert.equal(data.length, 2);
    assert.equal(data[0].pcm.length, 320);
    assert.ok(Math.abs(data[0].rms - 1) < 1e-3);
    assert.equal(data[0].at, 10);
    assert.equal(data[1].rms, 0);
    assert.equal(data[1].at, 20);
  });

  test("detects silence and pauses through the full capture pipeline", () => {
    const { spawn, children } = fakeSpawn();
    const recorder = createRecorder({ spawn, silenceMs: 1000, partialCadenceMs: 500 });
    const silence = collect(recorder, "silence");
    const pauses = collect(recorder, "pause");
    recorder.start();

    // 1.5 s of silence in 10 ms frames.
    feedFrames(recorder, 0, 150);

    assert.deepEqual(
      pauses.map((p) => p.at),
      [500, 1000], // partials stop once the end-of-speech silence fires
    );
    assert.equal(silence.length, 1);
    assert.equal(silence[0].at, 1000);
    assert.equal(children[0].killSignal, null);
  });

  test("stop() sends SIGINT and a clean exit does not raise an error", () => {
    const { spawn, children } = fakeSpawn();
    const recorder = createRecorder({ spawn });
    const errors = collect(recorder, "error");
    const exits = collect(recorder, "exit");
    recorder.start();

    recorder.stop();
    assert.equal(recorder.getState(), "stopping");
    assert.equal(children[0].killSignal, "SIGINT");

    children[0].emit("exit", 0, null);
    assert.equal(recorder.getState(), "idle");
    assert.equal(errors.length, 0);
    assert.equal(exits.length, 1);
    assert.equal(exits[0].unexpected, false);
  });

  test("unexpected capture exit surfaces an actionable error, never throws", () => {
    const { spawn, children } = fakeSpawn();
    const recorder = createRecorder({ spawn });
    const errors = collect(recorder, "error");
    recorder.start();

    children[0].stderr.write("arecord: main: audio open error: No such file or directory");
    assert.doesNotThrow(() => children[0].emit("exit", 1, null));

    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /arecord/);
    assert.match(errors[0].message, /exited with code 1/);
    assert.match(errors[0].message, /No such file or directory/);
    assert.equal(recorder.getState(), "idle");
  });

  test("a signal-terminated capture process is treated as unexpected", () => {
    const { spawn, children } = fakeSpawn();
    const recorder = createRecorder({ spawn });
    const errors = collect(recorder, "error");
    recorder.start();

    children[0].emit("exit", null, "SIGKILL");
    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /signal SIGKILL/);
  });

  test("spawn failure emits an error and leaves the recorder idle", () => {
    const recorder = createRecorder({
      spawn: () => {
        throw new Error("spawn arecord ENOENT");
      },
    });
    const errors = collect(recorder, "error");

    assert.doesNotThrow(() => recorder.start());
    assert.equal(recorder.getState(), "idle");
    assert.equal(errors.length, 1);
    assert.match(errors[0].message, /ENOENT/);
    assert.match(errors[0].message, /PATH/);
  });

  test("missing stdout stream is reported instead of crashing", () => {
    const child = new EventEmitter();
    child.stdout = null;
    const recorder = createRecorder({ spawn: () => child });
    const errors = collect(recorder, "error");

    assert.doesNotThrow(() => recorder.start());
    assert.equal(recorder.getState(), "idle");
    assert.match(errors[0].message, /no stdout stream/);
  });

  test("errors are retained even without an error listener and never throw", () => {
    const recorder = createRecorder({
      spawn: () => {
        throw new Error("boom");
      },
    });
    assert.doesNotThrow(() => recorder.start());
    assert.ok(recorder.lastError instanceof Error);
    assert.match(recorder.lastError.message, /boom/);
  });

  test("start is idempotent while recording and restartable after stop", () => {
    const { spawn, calls, children } = fakeSpawn();
    const recorder = createRecorder({ spawn });

    recorder.start();
    recorder.start();
    assert.equal(calls.length, 1);

    recorder.stop();
    children[0].emit("exit", 0, null);
    recorder.start();
    assert.equal(calls.length, 2);
  });
});

// ---------------------------------------------------------------------------
// captureErrorMessage helper
// ---------------------------------------------------------------------------

describe("captureErrorMessage", () => {
  test("names the command and exit code and suggests a fix", () => {
    const message = captureErrorMessage({ command: "parecord", code: 2, signal: null, stderr: "" });
    assert.match(message, /parecord/);
    assert.match(message, /code 2/);
    assert.match(message, /WSLg\/PulseAudio/);
  });

  test("reports signal termination", () => {
    const message = captureErrorMessage({ command: "arecord", code: null, signal: "SIGTERM", stderr: "x" });
    assert.match(message, /SIGTERM/);
    assert.match(message, /stderr: x/);
  });
});

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

describe("defaults", () => {
  test("default capture format is 16 kHz mono S16_LE", () => {
    assert.equal(SAMPLE_RATE, 16000);
    assert.ok(DEFAULT_CAPTURE_ARGS.includes("S16_LE"));
    assert.ok(DEFAULT_CAPTURE_ARGS.includes("16000"));
    assert.ok(DEFAULT_CAPTURE_ARGS.includes("1"));
  });
});
