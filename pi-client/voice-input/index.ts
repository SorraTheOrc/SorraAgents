/**
 * voice-input — pi client extension
 *
 * Press Ctrl+Space (configurable) to dictate a prompt: audio is captured
 * locally, transcribed on your own machine by a persistent faster-whisper
 * worker, streamed into the editor as live partials, and submitted as a normal
 * user message when you stop. Nothing leaves the machine.
 *
 * This file is the thin `ExtensionAPI` adapter; all behaviour lives in the
 * dependency-free modules next to it (unit tested with node:test):
 *   - `config.js`        — settings resolution (file / env / defaults)
 *   - `doctor.js`        — preflight faster-whisper / CUDA / capture / disk checks
 *   - `recorder.js`      — capture process, PCM framing, silence detection
 *   - `whisper-client.js`— persistent worker lifecycle and JSON protocol
 *   - `controller.js`    — idle → recording → submitting state machine
 *
 * Install: symlink or copy this directory into a pi extension discovery
 * location (e.g. `~/.pi/agent/extensions/voice-input`) and run `/reload`.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { KeyId } from "@earendil-works/pi-tui";
import { loadConfig } from "./config.js";
import { runDoctor, formatDoctorReport } from "./doctor.js";
import { createRecorder } from "./recorder.js";
import { createWhisperClient } from "./whisper-client.js";
import { createVoiceInputController } from "./controller.js";

export default function voiceInputExtension(pi: ExtensionAPI): void {
  // Load config once at registration so the shortcut can be bound; the
  // controller is created lazily on first use, when a `ctx` is available.
  const { config, warnings } = loadConfig();

  /** @type {ReturnType<typeof createVoiceInputController> | null} */
  let controller: ReturnType<typeof createVoiceInputController> | null = null;

  const ensureController = (ctx: ExtensionContext): ReturnType<typeof createVoiceInputController> => {
    if (controller) return controller;
    for (const warning of warnings) {
      ctx.ui.notify(`Voice input config: ${warning}`, "warning");
    }
    controller = createVoiceInputController({
      config,
      createRecorder,
      createWhisperClient,
      ui: ctx.ui,
      sendUserMessage: (text, options) => pi.sendUserMessage(text, options),
      isIdle: () => ctx.isIdle(),
      checkDoctor: () => runDoctor({ config }),
    });
    return controller;
  };

  const toggle = async (ctx: ExtensionContext): Promise<void> => {
    await ensureController(ctx).toggle();
  };

  pi.registerShortcut((config.keybinding || "ctrl+space") as KeyId, {
    description: "Toggle voice input recording (Ctrl+Space)",
    handler: toggle,
  });

  // Fallback for terminals/IMEs that swallow Ctrl+Space.
  pi.registerCommand("voice-input", {
    description: "Toggle voice input recording (fallback for Ctrl+Space)",
    handler: async (_args, ctx) => toggle(ctx),
  });

  pi.registerCommand("voice-input-doctor", {
    description: "Run the voice-input dependency/GPU/capture preflight check",
    handler: async (_args, ctx) => {
      const result = runDoctor({ config });
      ctx.ui.notify(formatDoctorReport(result.checks), result.ok ? "info" : "error");
    },
  });

  pi.registerCommand("voice-input-status", {
    description: "Show the voice-input recording state",
    handler: async (_args, ctx) => {
      ctx.ui.notify(`Voice input: ${controller ? controller.getState() : "not started"}`, "info");
    },
  });

  pi.on("session_shutdown", async () => {
    if (controller) {
      await controller.dispose();
      controller = null;
    }
  });
}
