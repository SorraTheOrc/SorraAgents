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

/**
 * Notification level for each feedback transition (SA-0MUXZBHX0006IXNZ).
 * State/success transitions use `info`; the empty-capture outcome (and the
 * existing failure paths) use `warning`/`error`.
 */
export const FEEDBACK_LEVELS = Object.freeze({
  start: "info",
  stop: "info",
  sending: "info",
  empty: "warning",
});

/**
 * Build the one-line message for a feedback transition.
 *
 * Wording is intentionally centralised here so it can be tuned without
 * changing the feedback contract; tests assert the presence, level and state
 * named by each message rather than its exact string.
 *
 * @param {string} kind one of `start` | `stop` | `sending` | `empty`
 * @param {object} [details]
 * @returns {string|null}
 */
export function feedbackMessage(kind, details = {}) {
  switch (kind) {
    case "start":
      return "🎙 Voice input enabled — listening… (Ctrl+Space to stop)";
    case "stop":
      return "🔇 Voice input disabled — processing…";
    case "sending": {
      const preview = previewTranscript(details.transcript);
      return preview
        ? `📤 Voice input: sending “${preview}”`
        : "📤 Voice input: sending transcript…";
    }
    case "empty":
      return "Voice input: nothing captured — nothing was sent.";
    default:
      return null;
  }
}

/** Truncate a transcript to its first few words for a one-line notification. */
export function previewTranscript(text, maxWords = 6) {
  const words = String(text ?? "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "";
  const head = words.slice(0, maxWords).join(" ");
  return words.length > maxWords ? `${head}…` : head;
}

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
 * @param {(transcript: string) => Promise<boolean>} [deps.sendShortcut] optional
 *   Herdr shortcut dispatcher: returns true when the transcript matched a
 *   configured phrase and was sent as a key chord instead of a prompt.
 * @param {() => boolean} [deps.isIdle] whether pi is idle (not streaming)
 * @param {() => object} [deps.checkDoctor] preflight check (run once on first start)
 * @param {() => void} [deps.playSound] audible-cue hook (terminal bell);
 *   injected so it is testable and suppressible. Only fired when
 *   `config.feedbackSound` is not `false`.
 * @param {object} [deps.logger]
 */
export function createVoiceInputController({
  config,
  createRecorder,
  createWhisperClient,
  ui,
  sendUserMessage,
  sendShortcut = null,
  isIdle = () => true,
  checkDoctor = null,
  playSound = null,
  logger = console,
} = {}) {
  const emitter = new EventEmitter();
  let state = STATE.idle;
  let recorder = null;
  let client = null;
  let originalEditorText = "";
  let lastPartial = "";
  let doctorResult = null;

  /**
   * Emit exactly one acknowledgement for a transition: a non-blocking
   * notification plus an optional, injected audible cue. Never throws, so a
   * failing cue can never break a recording transition.
   */
  function feedback(kind, details = {}) {
    const message = feedbackMessage(kind, details);
    if (!message) return;
    ui.notify(message, FEEDBACK_LEVELS[kind] ?? "info");
    if (config?.feedbackSound !== false && typeof playSound === "function") {
      try {
        playSound();
      } catch (err) {
        logger?.warn?.(`voice-input sound cue failed: ${err && err.message ? err.message : err}`);
      }
    }
  }

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
      feedback("start");
      emitter.emit("started");
    },

    /** Stop recording, finalise the transcript and submit it. */
    async stop() {
      if (state !== STATE.recording) return;
      state = STATE.submitting;
      ui.setStatus(STATUS_KEY, statusText(state));
      feedback("stop");

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
        feedback("empty");
        state = STATE.idle;
        emitter.emit("stopped", { submitted: false, transcript: "" });
        return;
      }

      // A configured voice shortcut sends a Herdr key chord instead of
      // submitting the transcript as a prompt.
      if (sendShortcut) {
        let handled = false;
        try {
          handled = Boolean(await sendShortcut(transcript));
        } catch (err) {
          logger?.warn?.(
            `voice-input shortcut failed: ${err && err.message ? err.message : err}`,
          );
        }
        if (handled) {
          lastPartial = transcript;
          ui.setEditorText("");
          state = STATE.idle;
          ui.setStatus(STATUS_KEY, undefined);
          feedback("sending", { transcript });
          emitter.emit("stopped", { submitted: true, transcript, shortcut: true });
          return;
        }
      }

      lastPartial = transcript;
      ui.setEditorText(transcript);
      feedback("sending", { transcript });
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
      beamSize: config.beamSize,
      vadFilter: config.vadFilter,
      initialPrompt: config.initialPrompt,
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
