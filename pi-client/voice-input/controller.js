/**
 * voice-input / controller — recording state machine and pi integration logic.
 *
 * This module holds the behaviour that must be unit-testable without a pi TUI,
 * microphone or GPU: the idle → recording → submitting state machine, partial
 * transcript replacement, auto-submit, error handling and doctor gating. The
 * TypeScript entry point (`index.ts`) is a thin adapter that supplies the pi
 * `ExtensionAPI`/`ctx.ui` callbacks and registers the shortcut.
 *
 * Dependency-free (`.js`) so node:test can import it without a build step,
 * matching the `proxy-sse-signals` convention.
 */

import { EventEmitter } from "node:events";

/** Controller states. */
export const STATE = Object.freeze({
  idle: "idle",
  recording: "recording",
  submitting: "submitting",
});

/** Footer status key used for the recording indicator. */
export const STATUS_KEY = "voice-input";

/** Render the footer indicator for a state (undefined clears it). */
export function statusText(state) {
  switch (state) {
    case STATE.recording:
      return "🎙 Recording… (Ctrl+Space to stop)";
    case STATE.submitting:
      return "⏳ Transcribing…";
    default:
      return undefined;
  }
}

/**
 * Create the voice-input controller.
 *
 * @param {object} deps
 * @param {object} deps.config resolved voice-input config
 * @param {(options: object) => object} deps.createRecorder recorder factory
 * @param {(options: object) => object} deps.createWhisperClient worker client factory
 * @param {object} deps.ui `{ setEditorText, getEditorText, setStatus, notify }`
 * @param {(text: string, options?: object) => void} deps.sendUserMessage
 * @param {() => boolean} [deps.isIdle] whether pi is idle (not streaming)
 * @param {() => object} [deps.checkDoctor] preflight check (run once on first start)
 * @param {object} [deps.logger]
 */
export function createVoiceInputController({
  config,
  createRecorder,
  createWhisperClient,
  ui,
  sendUserMessage,
  isIdle = () => true,
  checkDoctor = null,
  logger = console,
} = {}) {
  const emitter = new EventEmitter();
  let state = STATE.idle;
  let recorder = null;
  let client = null;
  let originalEditorText = "";
  let lastPartial = "";
  let doctorResult = null;

  const controller = {
    on(event, handler) {
      emitter.on(event, handler);
      return controller;
    },

    off(event, handler) {
      emitter.off(event, handler);
      return controller;
    },

    getState() {
      return state;
    },

    /** The most recent partial/final transcript text. */
    getTranscript() {
      return lastPartial;
    },

    /** Toggle recording: start when idle, stop when recording. */
    async toggle() {
      if (state === STATE.idle) {
        await controller.start();
      } else if (state === STATE.recording) {
        await controller.stop();
      }
      // `submitting` is intentionally ignored to avoid double submission.
    },

    /** Start recording (no-op unless idle). */
    async start() {
      if (state !== STATE.idle) return;

      if (checkDoctor && !doctorResult) {
        try {
          doctorResult = await checkDoctor();
        } catch (err) {
          logger?.warn?.(`voice-input doctor check failed: ${err && err.message ? err.message : err}`);
          doctorResult = null;
        }
        if (doctorResult && doctorResult.hasErrors) {
          const firstError = doctorResult.checks?.find((c) => c.status === "error");
          const remedy = firstError?.remedy ? ` ${firstError.remedy}` : "";
          const message = firstError?.message ?? "preflight checks failed";
          ui.notify(`Voice input unavailable: ${message}${remedy}`, "error");
          if (emitter.listenerCount("error") > 0) emitter.emit("error", new Error(message));
          return;
        }
        for (const warning of doctorResult?.checks?.filter((c) => c.status === "warning") ?? []) {
          ui.notify(`Voice input: ${warning.message}${warning.remedy ? ` ${warning.remedy}` : ""}`, "warning");
        }
      }

      state = STATE.recording;
      originalEditorText = ui.getEditorText();
      lastPartial = "";
      ui.setStatus(STATUS_KEY, statusText(state));

      // The worker is persistent: load it once per session and reuse it across
      // recordings so the model is not reloaded per utterance.
      if (!client) {
        client = createWhisperClient(clientOptions());
        wireClientEvents(client);
        try {
          await client.start();
        } catch (err) {
          const message = err && err.message ? err.message : String(err);
          await controller.abort(`failed to start transcription: ${message}`, "error");
          return;
        }
      }

      recorder = createRecorder(recorderOptions());
      wireRecorderEvents(recorder);
      try {
        recorder.start();
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        await controller.abort(`failed to start audio capture: ${message}`, "error");
        return;
      }
      emitter.emit("started");
    },

    /** Stop recording, finalise the transcript and submit it. */
    async stop() {
      if (state !== STATE.recording) return;
      state = STATE.submitting;
      ui.setStatus(STATUS_KEY, statusText(state));

      const activeRecorder = recorder;
      const activeClient = client;
      recorder = null;
      if (activeRecorder) {
        try {
          activeRecorder.stop();
        } catch (err) {
          logger?.warn?.(`voice-input recorder stop failed: ${err && err.message ? err.message : err}`);
        }
      }

      let transcript = "";
      try {
        if (activeClient) transcript = await activeClient.finalise();
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        await controller.abort(`transcription failed: ${message}`, "error");
        return;
      }

      transcript = String(transcript || lastPartial || "").trim();
      if (!transcript) {
        // Nothing recognised: do not submit; restore the pre-recording editor.
        ui.setEditorText(originalEditorText);
        ui.setStatus(STATUS_KEY, undefined);
        ui.notify("Voice input: no speech detected — nothing was sent.", "warning");
        state = STATE.idle;
        emitter.emit("stopped", { submitted: false, transcript: "" });
        return;
      }

      lastPartial = transcript;
      ui.setEditorText(transcript);
      submit(transcript);

      state = STATE.idle;
      ui.setStatus(STATUS_KEY, undefined);
      emitter.emit("stopped", { submitted: true, transcript });
    },

    /**
     * Abort the current recording, notify the operator and reset to idle.
     * Never throws.
     */
    async abort(message, level = "error") {
      const currentRecorder = recorder;
      recorder = null;
      if (currentRecorder) {
        try {
          currentRecorder.stop();
        } catch (err) {
          logger?.warn?.(`voice-input recorder stop failed: ${err && err.message ? err.message : err}`);
        }
      }
      // Partial text is discarded on error; restore the pre-recording editor.
      try {
        ui.setEditorText(originalEditorText);
      } catch {
        /* ignore UI restore failures */
      }
      ui.setStatus(STATUS_KEY, undefined);
      state = STATE.idle;
      ui.notify(`Voice input: ${message}`, level);
      if (emitter.listenerCount("error") > 0) emitter.emit("error", new Error(message));
    },

    /** Stop and release the persistent worker (session shutdown). */
    async dispose() {
      const currentRecorder = recorder;
      recorder = null;
      if (currentRecorder) {
        try {
          currentRecorder.stop();
        } catch {
          /* ignore */
        }
      }
      const currentClient = client;
      client = null;
      state = STATE.idle;
      ui.setStatus(STATUS_KEY, undefined);
      if (currentClient) {
        try {
          await currentClient.stop();
        } catch {
          /* ignore */
        }
      }
    },
  };

  function submit(text) {
    const options = isIdle() ? undefined : { deliverAs: "steer" };
    sendUserMessage(text, options);
    // Mirror "typed it and pressed Enter": the editor is cleared after submit.
    ui.setEditorText("");
  }

  function recorderOptions() {
    return {
      command: config.captureCommand,
      args: config.captureArgs,
      silenceMs: config.silenceMs,
      partialCadenceMs: config.partialCadenceMs,
      silenceThreshold: config.silenceThreshold,
    };
  }

  function clientOptions() {
    const options = {
      python: config.python,
      model: config.model,
      device: config.device,
      computeType: config.computeType,
      language: config.language || null,
      partialIntervalMs: config.partialCadenceMs,
    };
    if (config.workerScript) options.workerScript = config.workerScript;
    return options;
  }

  function wireRecorderEvents(activeRecorder) {
    activeRecorder.on("data", ({ pcm }) => {
      if (client) client.feed(pcm);
    });
    activeRecorder.on("silence", () => {
      // Auto-submit after the configured silent pause.
      void controller.stop();
    });
    activeRecorder.on("error", (err) => {
      void controller.abort(`audio capture failed: ${err && err.message ? err.message : err}`);
    });
  }

  function wireClientEvents(activeClient) {
    activeClient.on("partial", ({ text }) => {
      if (state !== STATE.recording) return;
      if (!text || text === lastPartial) return;
      lastPartial = text;
      ui.setEditorText(text);
    });
    activeClient.on("warning", ({ message }) => {
      if (message) ui.notify(`Voice input: ${message}`, "warning");
    });
    activeClient.on("error", (err) => {
      void controller.abort(`transcription failed: ${err && err.message ? err.message : err}`);
    });
  }

  return controller;
}
