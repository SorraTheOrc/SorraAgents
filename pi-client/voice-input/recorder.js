/**
 * voice-input / recorder — audio capture, PCM framing and silence detection.
 *
 * This is the dependency-free logic module for the `voice-input` pi extension
 * (the TypeScript entry point in `index.ts` is a thin adapter over it, matching
 * the `proxy-sse-signals` convention: logic in a testable `.js` module, jiti
 * loads the `.ts` adapter with no build step).
 *
 * Responsibilities:
 *   - spawn a configurable capture command (default `arecord`, 16 kHz mono
 *     signed 16-bit little-endian PCM on stdout);
 *   - frame the raw byte stream into fixed-size PCM frames;
 *   - compute per-frame RMS energy (normalised to 0..1);
 *   - detect speech pauses (partial-transcription cadence) and end-of-speech
 *     silence (auto-submit trigger);
 *   - surface capture failures as actionable `error` events, never throw;
 *   - shut the capture process down gracefully (SIGINT).
 *
 * The module is intentionally injectable: `spawn` and the audio timeline can be
 * substituted so the state machine is unit-testable without a microphone.
 */

import { EventEmitter } from "node:events";
import { spawn as nodeSpawn } from "node:child_process";

/** Audio format assumed throughout (must match the capture command). */
export const SAMPLE_RATE = 16000;
export const BYTES_PER_SAMPLE = 2;

/** Default framing window. 10 ms @ 16 kHz mono 16-bit = 320 bytes. */
export const DEFAULT_FRAME_MS = 10;

/** WSLg/PulseAudio-friendly default; overridable through config. */
export const DEFAULT_CAPTURE_COMMAND = "arecord";
export const DEFAULT_CAPTURE_ARGS = [
  "-f",
  "S16_LE",
  "-r",
  String(SAMPLE_RATE),
  "-c",
  "1",
  "-t",
  "raw",
  "-q",
];

/**
 * Compute the root-mean-square energy of a little-endian 16-bit PCM buffer,
 * normalised to the 0..1 range (so a configurable amplitude threshold is
 * device-independent).
 *
 * @param {Buffer|Uint8Array} chunk raw mono 16-bit little-endian PCM
 * @returns {number} RMS in [0, 1]
 */
export function computeRms(chunk) {
  const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
  const samples = Math.floor(buf.length / BYTES_PER_SAMPLE);
  if (samples === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples; i++) {
    const sample = buf.readInt16LE(i * BYTES_PER_SAMPLE) / 32768;
    sum += sample * sample;
  }
  return Math.sqrt(sum / samples);
}

/** Bytes in one frame of `frameMs` at the assumed format. */
export function frameBytesFor(frameMs) {
  return Math.max(
    BYTES_PER_SAMPLE,
    Math.round((SAMPLE_RATE * BYTES_PER_SAMPLE * frameMs) / 1000),
  );
}

/**
 * Accumulates arbitrarily-chunked PCM into fixed-size frames. Capture
 * backends deliver bytes in unpredictable chunks; the detector needs a stable
 * frame size so the audio timeline (and therefore silence timing) is
 * deterministic.
 */
export class PcmFramer {
  /** @param {number} frameBytes fixed frame size in bytes */
  constructor(frameBytes) {
    if (!Number.isFinite(frameBytes) || frameBytes < BYTES_PER_SAMPLE) {
      throw new Error(`PcmFramer: invalid frame size ${frameBytes}`);
    }
    this.frameBytes = frameBytes;
    /** @type {Buffer} */
    this.pending = Buffer.alloc(0);
  }

  /**
   * @param {Buffer|Uint8Array} chunk
   * @returns {Buffer[]} zero or more complete frames
   */
  push(chunk) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    this.pending = this.pending.length === 0 ? buf : Buffer.concat([this.pending, buf]);
    const frames = [];
    while (this.pending.length >= this.frameBytes) {
      // Copy so callers can retain frames after the next push mutates state.
      frames.push(Buffer.from(this.pending.subarray(0, this.frameBytes)));
      this.pending = this.pending.subarray(this.frameBytes);
    }
    return frames;
  }

  /** @returns {Buffer|null} the short trailing partial frame, if any */
  flush() {
    if (this.pending.length === 0) return null;
    const rest = Buffer.from(this.pending);
    this.pending = Buffer.alloc(0);
    return rest;
  }
}

/**
 * Energy-based pause/silence detector operating on an explicit audio timeline.
 *
 * `push(rms, at)` is called once per frame, where `at` is the frame's *end*
 * time in milliseconds since capture started. Feeding audio time (rather than
 * wall-clock time) makes the timing deterministic and immune to scheduler
 * jitter — a requirement for the unit tests.
 */
export class SilenceDetector {
  /**
   * @param {object} [options]
   * @param {number} [options.threshold] RMS below which a frame is silent
   * @param {number} [options.silenceMs] end-of-speech silence (auto-submit)
   * @param {number} [options.partialCadenceMs] pause/partial-transcription cadence
   */
  constructor({ threshold = 0.01, silenceMs = 3000, partialCadenceMs = 1000 } = {}) {
    this.threshold = threshold;
    this.silenceMs = silenceMs;
    this.partialCadenceMs = partialCadenceMs;
    /** @type {number} audio time of the most recent speech frame */
    this.lastSpeechAt = 0;
    /** @type {number|null} audio time of the last emitted pause */
    this.lastPartialAt = null;
    /** @type {boolean} whether the silence event fired for this speech run */
    this.silenceEmitted = false;
    this.started = false;
  }

  /**
   * @param {number} rms frame energy
   * @param {number} at frame end time in ms since capture start
   * @returns {Array<{type: "pause"|"silence", at: number, silentMs: number}>}
   */
  push(rms, at) {
    const events = [];
    if (!this.started) {
      this.started = true;
      // Treat recording start as the baseline so speaking nothing still
      // auto-stops after silenceMs.
      this.lastSpeechAt = 0;
    }

    if (rms >= this.threshold) {
      // Speech: reset the whole silence run.
      this.lastSpeechAt = at;
      this.lastPartialAt = null;
      this.silenceEmitted = false;
      return events;
    }

    const silentMs = at - this.lastSpeechAt;

    if (
      !this.silenceEmitted &&
      this.partialCadenceMs > 0 &&
      silentMs >= this.partialCadenceMs &&
      (this.lastPartialAt === null || at - this.lastPartialAt >= this.partialCadenceMs)
    ) {
      this.lastPartialAt = at;
      events.push({ type: "pause", at, silentMs });
    }

    if (!this.silenceEmitted && this.silenceMs > 0 && silentMs >= this.silenceMs) {
      this.silenceEmitted = true;
      events.push({ type: "silence", at, silentMs });
    }

    return events;
  }
}

/**
 * Build an actionable message for a capture failure. Kept as a pure helper so
 * the wording is testable and consistent between spawn-error and exit paths.
 */
export function captureErrorMessage({ command, code, signal, stderr }) {
  const detail = stderr ? ` Capture stderr: ${String(stderr).trim()}` : "";
  const reason =
    code !== undefined && code !== null
      ? `exited with code ${code}`
      : `was terminated by signal ${signal}`;
  return (
    `Audio capture command "${command}" ${reason}. ` +
    "Check that the capture backend is installed and that a microphone is " +
    `available (for WSL2, ensure WSLg/PulseAudio is running).${detail}`
  );
}

/**
 * Recording state machine + capture process owner.
 *
 * Events: `start`, `data` ({pcm, rms, at}), `pause`, `silence`, `exit`,
 * `error` (Error, with `.detail`). `error` is only emitted when a listener is
 * attached (plus always retained in `lastError`) so a missing listener can
 * never crash the pi session.
 */
export class Recorder {
  constructor({
    command = DEFAULT_CAPTURE_COMMAND,
    args = DEFAULT_CAPTURE_ARGS,
    spawn = nodeSpawn,
    frameMs = DEFAULT_FRAME_MS,
    silenceThreshold = 0.01,
    silenceMs = 3000,
    partialCadenceMs = 1000,
  } = {}) {
    this.command = command;
    this.args = args;
    this.spawn = spawn;
    this.frameMs = frameMs;
    this.silenceThreshold = silenceThreshold;
    this.silenceMs = silenceMs;
    this.partialCadenceMs = partialCadenceMs;

    this.emitter = new EventEmitter();
    this.state = "idle";
    this.child = null;
    this.stderr = "";
    this.lastError = null;

    this.framer = null;
    this.detector = null;
    this.totalSamples = 0;
  }

  on(event, handler) {
    this.emitter.on(event, handler);
    return this;
  }

  off(event, handler) {
    this.emitter.off(event, handler);
    return this;
  }

  /** Start capturing. Idempotent: a second call while active is a no-op. */
  start() {
    if (this.state !== "idle") return this;

    this.state = "recording";
    this.stopping = false;
    this.stderr = "";
    this.totalSamples = 0;
    this.framer = new PcmFramer(frameBytesFor(this.frameMs));
    this.detector = new SilenceDetector({
      threshold: this.silenceThreshold,
      silenceMs: this.silenceMs,
      partialCadenceMs: this.partialCadenceMs,
    });

    let child;
    try {
      child = this.spawn(this.command, this.args, { stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      this.state = "idle";
      this.emitError(
        `Failed to start audio capture command "${this.command}": ${err && err.message ? err.message : err}. ` +
          "Verify the capture backend is installed and the command is on PATH.",
      );
      return this;
    }

    if (!child || !child.stdout) {
      this.state = "idle";
      this.emitError(
        `Audio capture command "${this.command}" produced no stdout stream; check the capture arguments.`,
      );
      return this;
    }

    this.child = child;
    child.stdout.on("data", (chunk) => this.handleChunk(chunk));
    if (child.stderr && typeof child.stderr.on === "function") {
      child.stderr.on("data", (chunk) => {
        this.stderr += chunk.toString();
      });
    }
    child.on("error", (err) => {
      this.fail(
        `Audio capture command "${this.command}" failed to start: ${err && err.message ? err.message : err}. ` +
          "Verify the capture backend is installed and the command is on PATH.",
      );
    });
    child.on("exit", (code, signal) => this.handleExit(code, signal));

    this.emitter.emit("start", { command: this.command, args: this.args });
    return this;
  }

  /**
   * Inject a raw PCM chunk (used by the capture `data` handler and directly by
   * tests / alternative capture backends).
   * @param {Buffer|Uint8Array} chunk
   */
  handleChunk(chunk) {
    if (!this.framer) return;
    for (const frame of this.framer.push(chunk)) this.handleFrame(frame);
  }

  /** @param {Buffer} frame */
  handleFrame(frame) {
    if (!this.detector) return;
    this.totalSamples += frame.length / BYTES_PER_SAMPLE;
    const at = Math.round((this.totalSamples / SAMPLE_RATE) * 1000);
    const rms = computeRms(frame);

    this.emitter.emit("data", { pcm: frame, rms, at });

    for (const event of this.detector.push(rms, at)) {
      this.emitter.emit(event.type, event);
    }
  }

  /** Stop capturing gracefully (SIGINT); the `exit` event follows. */
  stop() {
    if (this.state !== "recording") return;
    this.state = "stopping";
    this.stopping = true;
    if (this.child) {
      try {
        this.child.kill("SIGINT");
      } catch (err) {
        this.emitError(
          `Failed to stop audio capture command "${this.command}": ${err && err.message ? err.message : err}.`,
        );
      }
    }
  }

  /** @returns {"idle"|"recording"|"stopping"} */
  getState() {
    return this.state;
  }

  handleExit(code, signal) {
    const unexpected = !this.stopping && (code !== 0 || Boolean(signal));
    this.child = null;
    this.state = "idle";
    if (unexpected) {
      this.fail(captureErrorMessage({ command: this.command, code, signal, stderr: this.stderr }));
    }
    this.emitter.emit("exit", { code, signal, unexpected });
  }

  /** Surface an error without ever throwing out of the extension. */
  fail(message) {
    this.state = "idle";
    this.child = null;
    this.emitError(message);
  }

  emitError(message) {
    const error = new Error(message);
    error.detail = message;
    this.lastError = error;
    if (this.emitter.listenerCount("error") > 0) {
      this.emitter.emit("error", error);
    }
  }
}

/**
 * @param {ConstructorParameters<typeof Recorder>[0]} [options]
 * @returns {Recorder}
 */
export function createRecorder(options) {
  return new Recorder(options);
}
