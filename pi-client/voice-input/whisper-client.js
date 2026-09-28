/**
 * voice-input / whisper-client — persistent faster-whisper worker client.
 *
 * Talks to `whisper_worker.py` over a line-delimited JSON protocol (one JSON
 * object per line) so the model is loaded once per pi session. This module
 * contains the dependency-free client logic (unit tested in
 * `tests/unit/test-voice-whisper-client.mjs`); the TypeScript entry point is a
 * thin adapter, matching the `proxy-sse-signals` convention.
 *
 * Protocol (see `whisper_worker.py` for the full spec):
 *   -> {"type":"start","model","device","computeType","partialIntervalMs"}
 *   <- {"type":"ready","model","device","computeType"}
 *   -> {"type":"feed","audio":"<base64 int16 PCM>"}
 *   <- {"type":"partial","text","sequence"}
 *   -> {"type":"finalise"}
 *   <- {"type":"final","text","sequence"}
 *   -> {"type":"stop"}
 *   <- {"type":"stopped"}
 *   <- {"type":"error","message"} / {"type":"warning","message"}
 *
 * Failures are surfaced as `error` events and rejected startup/finalise
 * promises; the client never throws from `feed()` and never crashes the pi
 * session.
 */

import { EventEmitter } from "node:events";
import { spawn as nodeSpawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

/** Path to the bundled Python worker, resolved relative to this module. */
export const DEFAULT_WORKER_SCRIPT = fileURLToPath(
  new URL("./whisper_worker.py", import.meta.url),
);

/** Python interpreter used to run the worker. */
export const DEFAULT_PYTHON = "python3";

/**
 * Persistent faster-whisper worker client.
 *
 * Events: `ready`, `partial` ({text, sequence}), `final` ({text, sequence}),
 * `stopped`, `warning` ({message}), `error` (Error), `stderr` (string),
 * `exit` ({code, signal, unexpected}).
 */
export class WhisperClient {
  constructor({
    python = DEFAULT_PYTHON,
    workerScript = DEFAULT_WORKER_SCRIPT,
    model = "small",
    device = "cuda",
    computeType = "float16",
    partialIntervalMs = 1000,
    language = null,
    spawn = nodeSpawn,
    startupTimeoutMs = 120000,
    finaliseTimeoutMs = 60000,
    stopTimeoutMs = 10000,
  } = {}) {
    this.python = python;
    this.workerScript = workerScript;
    this.model = model;
    this.device = device;
    this.computeType = computeType;
    this.partialIntervalMs = partialIntervalMs;
    this.language = language;
    this.spawn = spawn;
    this.startupTimeoutMs = startupTimeoutMs;
    this.finaliseTimeoutMs = finaliseTimeoutMs;
    this.stopTimeoutMs = stopTimeoutMs;

    this.emitter = new EventEmitter();
    this.state = "idle";
    this.child = null;
    this.stderr = "";
    this.lastError = null;
    this.readyInfo = null;
    /** @type {Map<string, {resolve: Function, reject: Function}>} */
    this.waiters = new Map();
  }

  on(event, handler) {
    this.emitter.on(event, handler);
    return this;
  }

  off(event, handler) {
    this.emitter.off(event, handler);
    return this;
  }

  /** @returns {"idle"|"starting"|"ready"|"finalising"|"stopping"|"stopped"} */
  getState() {
    return this.state;
  }

  /**
   * Spawn the worker and send the START command.
   * @returns {Promise<object>} resolves with the worker's READY payload
   */
  start() {
    if (this.state !== "idle") return Promise.resolve(this.readyInfo);
    this.state = "starting";
    this.stderr = "";

    const args = [
      this.workerScript,
      "--model",
      this.model,
      "--device",
      this.device,
      "--compute-type",
      this.computeType,
      "--partial-interval-ms",
      String(this.partialIntervalMs),
    ];
    if (this.language) args.push("--language", this.language);

    try {
      this.child = this.spawn(this.python, args, { stdio: ["pipe", "pipe", "pipe"] });
    } catch (err) {
      this.state = "idle";
      this.child = null;
      this.fail(
        `failed to spawn the whisper worker (${this.python}): ${err && err.message ? err.message : err}. ` +
          "Install faster-whisper and ensure python3 is on PATH.",
      );
      return Promise.reject(this.lastError);
    }

    if (!this.child || !this.child.stdout || !this.child.stdin) {
      this.state = "idle";
      this.child = null;
      this.fail(
        `the whisper worker (${this.python} ${this.workerScript}) produced no stdio streams`,
      );
      return Promise.reject(this.lastError);
    }

    this.readline = createInterface({ input: this.child.stdout });
    this.readline.on("line", (line) => this.handleLine(line));
    if (this.child.stderr && typeof this.child.stderr.on === "function") {
      this.child.stderr.on("data", (chunk) => {
        const text = chunk.toString();
        this.stderr += text;
        this.emitter.emit("stderr", text);
      });
    }
    this.child.on("error", (err) => {
      this.fail(
        `the whisper worker failed: ${err && err.message ? err.message : err}. ` +
          "Verify faster-whisper is installed in the selected Python environment.",
      );
    });
    this.child.on("exit", (code, signal) => this.handleExit(code, signal));

    const ready = this.waitFor("ready", this.startupTimeoutMs);
    this.send({
      type: "start",
      model: this.model,
      device: this.device,
      computeType: this.computeType,
      partialIntervalMs: this.partialIntervalMs,
    });
    return ready.then((info) => {
      this.readyInfo = info;
      return info;
    });
  }

  /**
   * Forward a PCM chunk to the worker for accumulation and partial
   * transcription.
   * @param {Buffer|Uint8Array} pcm
   * @returns {boolean} whether the chunk was sent
   */
  feed(pcm) {
    if (!this.child || (this.state !== "ready" && this.state !== "finalising")) {
      return false;
    }
    const buf = Buffer.isBuffer(pcm) ? pcm : Buffer.from(pcm);
    this.send({ type: "feed", audio: buf.toString("base64") });
    return true;
  }

  /**
   * Ask the worker for the complete transcript of everything fed so far.
   * @returns {Promise<string>}
   */
  finalise() {
    if (!this.child) return Promise.resolve("");
    this.state = "finalising";
    const pending = this.waitFor("final", this.finaliseTimeoutMs);
    this.send({ type: "finalise" });
    return pending
      .then((message) => {
        this.state = "ready";
        return message.text ?? "";
      })
      .catch((err) => {
        this.state = this.child ? "ready" : "idle";
        throw err;
      });
  }

  /**
   * Ask the worker to shut down cleanly.
   * @returns {Promise<void>}
   */
  stop() {
    if (!this.child) return Promise.resolve();
    this.state = "stopping";
    this.stopping = true;
    const pending = this.waitFor("stopped", this.stopTimeoutMs);
    this.send({ type: "stop" });
    return pending
      .catch((err) => {
        // Force-terminate if the worker does not acknowledge in time.
        this.kill();
        throw err;
      })
      .finally(() => {
        this.state = "stopped";
      });
  }

  /** Terminate the worker process immediately (best effort). */
  kill(signal = "SIGTERM") {
    if (this.child) {
      try {
        this.child.kill(signal);
      } catch (err) {
        this.emitError(`failed to terminate the whisper worker: ${err && err.message ? err.message : err}`);
      }
    }
  }

  /** @param {object} message */
  send(message) {
    if (!this.child || !this.child.stdin) return;
    try {
      this.child.stdin.write(`${JSON.stringify(message)}\n`);
    } catch (err) {
      this.fail(`failed to write to the whisper worker: ${err && err.message ? err.message : err}`);
    }
  }

  /** @param {string} line one JSON line from the worker's stdout */
  handleLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return;
    let message;
    try {
      message = JSON.parse(trimmed);
    } catch {
      // Non-JSON stdout is informational (e.g. a Python traceback banner).
      this.emitter.emit("stderr", `${trimmed}\n`);
      return;
    }
    if (!message || typeof message !== "object") return;

    switch (message.type) {
      case "ready":
        this.state = "ready";
        this.resolveWaiter("ready", message);
        this.emitter.emit("ready", message);
        break;
      case "partial":
        this.emitter.emit("partial", {
          text: message.text ?? "",
          sequence: message.sequence ?? null,
        });
        break;
      case "final":
        this.resolveWaiter("final", message);
        this.emitter.emit("final", {
          text: message.text ?? "",
          sequence: message.sequence ?? null,
        });
        break;
      case "stopped":
        this.resolveWaiter("stopped", message);
        this.emitter.emit("stopped", message);
        break;
      case "warning":
        this.emitter.emit("warning", { message: message.message ?? "" });
        break;
      case "error": {
        // A worker error during startup is fatal (model load failed); one
        // during finalise/feed is recoverable and the worker stays usable.
        const fatal = this.state === "starting";
        this.errorReported = true;
        if (fatal) {
          this.state = "idle";
          this.child = null;
          if (this.readline) {
            this.readline.close();
            this.readline = null;
          }
        }
        this.fail(message.message || "the whisper worker reported an error");
        break;
      }
      default:
        this.emitter.emit("stderr", `${trimmed}\n`);
        break;
    }
  }

  handleExit(code, signal) {
    const unexpected = !this.stopping && (code !== 0 || Boolean(signal));
    if (this.readline) {
      this.readline.close();
      this.readline = null;
    }
    this.child = null;
    if (this.state !== "stopped") this.state = "idle";
    if (unexpected && !this.errorReported) {
      const detail = this.stderr.trim() ? ` Worker stderr: ${this.stderr.trim()}` : "";
      this.fail(
        `the whisper worker exited unexpectedly (code ${code}, signal ${signal}).${detail}`,
      );
    }
    this.errorReported = false;
    this.emitter.emit("exit", { code, signal, unexpected });
  }

  /**
   * Register a one-shot waiter for a worker response type.
   * @param {string} type
   * @param {number} timeoutMs
   * @returns {Promise<object>}
   */
  waitFor(type, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(type);
        reject(new Error(`timed out waiting for "${type}" from the whisper worker`));
      }, timeoutMs);
      this.waiters.set(type, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });
    });
  }

  resolveWaiter(type, value) {
    const waiter = this.waiters.get(type);
    if (!waiter) return;
    this.waiters.delete(type);
    waiter.resolve(value);
  }

  rejectWaiters(error) {
    for (const [type, waiter] of this.waiters) {
      this.waiters.delete(type);
      waiter.reject(error);
    }
  }

  fail(message) {
    const error = new Error(message);
    error.detail = message;
    this.lastError = error;
    this.rejectWaiters(error);
    if (this.emitter.listenerCount("error") > 0) {
      this.emitter.emit("error", error);
    }
  }

  emitError(message) {
    this.fail(message);
  }
}

/**
 * @param {ConstructorParameters<typeof WhisperClient>[0]} [options]
 * @returns {WhisperClient}
 */
export function createWhisperClient(options) {
  return new WhisperClient(options);
}
