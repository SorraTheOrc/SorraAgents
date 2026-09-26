/**
 * voice-input / doctor — preflight dependency, CUDA and disk checks.
 *
 * `runDoctor` is a pure-ish function over injectable probes (`runCommand`,
 * `commandExists`, `freeBytes`) so the checks are unit testable without a GPU,
 * microphone or a faster-whisper install. The default probes use real
 * subprocesses and `statfs`.
 *
 * Severity model:
 *   - `error`   — the extension cannot work at all (faster-whisper missing,
 *                 capture backend missing); the entry disables itself and
 *                 reports the remedy.
 *   - `warning` — degraded mode is possible (no CUDA → CPU int8 fallback,
 *                 low disk); the extension starts but notifies.
 *   - `ok`      — healthy.
 */

import { spawnSync } from "node:child_process";
import { statfsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { MODEL_SIZES_MB } from "./config.js";

/** @typedef {"ok"|"warning"|"error"} DoctorStatus */

export const STATUS = Object.freeze({ ok: "ok", warning: "warning", error: "error" });

/** Build a structured check result. */
export function makeCheck(id, label, status, message, remedy = "") {
  return { id, label, status, message, remedy };
}

/** Default probe: run a command synchronously. */
export function defaultRunCommand(command, args = []) {
  const result = spawnSync(command, args, { encoding: "utf8", timeout: 30000 });
  return {
    ok: result.status === 0,
    code: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    error: result.error ? String(result.error.message ?? result.error) : null,
  };
}

/** Default probe: is an executable on PATH? */
export function defaultCommandExists(command) {
  const finder = process.platform === "win32" ? "where" : "which";
  const result = spawnSync(finder, [command], { encoding: "utf8", timeout: 10000 });
  return result.status === 0;
}

/** Default probe: free bytes on the filesystem holding `path`. */
export function defaultFreeBytes(path) {
  try {
    const stats = statfsSync(path);
    return Number(stats.bavail) * Number(stats.bsize);
  } catch {
    return null;
  }
}

/** Directory where faster-whisper downloads model weights by default. */
export function defaultModelCacheDir() {
  return join(homedir(), ".cache", "huggingface");
}

/** Check that faster-whisper can be imported by the configured Python. */
export function checkFasterWhisper({ config, runCommand = defaultRunCommand }) {
  const probe = runCommand(config.python, [
    "-c",
    "import faster_whisper, sys; print(getattr(faster_whisper, '__version__', 'unknown'))",
  ]);
  if (probe.ok) {
    const version = probe.stdout.trim() || "unknown";
    return makeCheck(
      "faster-whisper",
      "faster-whisper",
      STATUS.ok,
      `faster-whisper ${version} is importable with ${config.python}.`,
    );
  }
  return makeCheck(
    "faster-whisper",
    "faster-whisper",
    STATUS.error,
    `faster-whisper is not importable with ${config.python}` +
      (probe.stderr || probe.error ? `: ${(probe.stderr || probe.error).trim().slice(0, 300)}` : "."),
    `Install it with \`${config.python} -m pip install faster-whisper\` ` +
      "(a CUDA build also needs cuDNN/cuBLAS).",
  );
}

/** Check CUDA availability (skipped when CPU mode is configured). */
export function checkCuda({ config, runCommand = defaultRunCommand }) {
  if (config.device === "cpu") {
    return makeCheck("cuda", "CUDA GPU", STATUS.ok, "CPU inference is configured; no GPU required.");
  }
  const probe = runCommand("nvidia-smi", ["--query-gpu=name", "--format=csv,noheader"]);
  if (probe.ok) {
    const gpu = probe.stdout.trim().split("\n")[0] || "unknown GPU";
    return makeCheck("cuda", "CUDA GPU", STATUS.ok, `CUDA available: ${gpu}.`);
  }
  return makeCheck(
    "cuda",
    "CUDA GPU",
    STATUS.warning,
    "nvidia-smi is unavailable; transcription will fall back to CPU int8 (slower).",
    "Install the NVIDIA driver and a CUDA build of faster-whisper, or set device=cpu to silence this warning.",
  );
}

/** Check that the configured capture backend is installed. */
export function checkCaptureBackend({ config, commandExists = defaultCommandExists }) {
  if (commandExists(config.captureCommand)) {
    return makeCheck(
      "capture",
      "Audio capture",
      STATUS.ok,
      `Capture command "${config.captureCommand}" is available.`,
    );
  }
  return makeCheck(
    "capture",
    "Audio capture",
    STATUS.error,
    `Capture command "${config.captureCommand}" was not found on PATH.`,
    "Install it (Debian/Ubuntu: `sudo apt install alsa-utils`) and, under WSL2, " +
      "make sure WSLg/PulseAudio is running; or set captureCommand to parecord/ffmpeg.",
  );
}

/** Check that disk space can accommodate the model download. */
export function checkDiskSpace({
  config,
  freeBytes = defaultFreeBytes,
  cacheDir = defaultModelCacheDir(),
}) {
  const requiredMb = MODEL_SIZES_MB[config.model];
  if (!requiredMb) {
    return makeCheck(
      "disk",
      "Model download",
      STATUS.warning,
      `Unknown model "${config.model}"; cannot estimate the required disk space.`,
      "Use a known model size (tiny, base, small, medium, large-v3) to enable the disk check.",
    );
  }
  let free;
  try {
    free = freeBytes(cacheDir);
  } catch {
    free = null;
  }
  if (free === null || free === undefined) {
    return makeCheck(
      "disk",
      "Model download",
      STATUS.warning,
      `Could not determine free disk space at ${cacheDir}.`,
      `Ensure the model cache directory exists and is writable: ${cacheDir}.`,
    );
  }
  const freeMb = Math.floor(free / (1024 * 1024));
  if (freeMb < requiredMb) {
    return makeCheck(
      "disk",
      "Model download",
      STATUS.error,
      `Only ${freeMb} MB free at ${cacheDir}; model "${config.model}" needs about ${requiredMb} MB.`,
      "Free disk space or choose a smaller model (e.g. base or small).",
    );
  }
  if (freeMb < requiredMb * 1.5) {
    return makeCheck(
      "disk",
      "Model download",
      STATUS.warning,
      `Only ${freeMb} MB free at ${cacheDir}; model "${config.model}" needs about ${requiredMb} MB.`,
      "Free disk space or choose a smaller model.",
    );
  }
  return makeCheck(
    "disk",
    "Model download",
    STATUS.ok,
    `${freeMb} MB free; model "${config.model}" needs about ${requiredMb} MB.`,
  );
}

/**
 * Run all preflight checks.
 *
 * @param {object} options
 * @param {object} options.config resolved voice-input config
 * @param {object} [options.probes] injected probes { runCommand, commandExists, freeBytes, cacheDir }
 * @returns {{ok: boolean, hasErrors: boolean, hasWarnings: boolean, checks: object[]}}
 */
export function runDoctor({ config, probes = {} }) {
  const { runCommand, commandExists, freeBytes, cacheDir } = probes;
  const checks = [
    checkFasterWhisper({ config, runCommand }),
    checkCuda({ config, runCommand }),
    checkCaptureBackend({ config, commandExists }),
    checkDiskSpace({
      config,
      freeBytes: freeBytes ?? defaultFreeBytes,
      cacheDir: cacheDir ?? defaultModelCacheDir(),
    }),
  ];
  const hasErrors = checks.some((check) => check.status === STATUS.error);
  const hasWarnings = checks.some((check) => check.status === STATUS.warning);
  return { ok: !hasErrors, hasErrors, hasWarnings, checks };
}

/** Render checks as a human-readable multi-line report. */
export function formatDoctorReport(checks) {
  const icon = { ok: "✓", warning: "!", error: "✗" };
  return checks
    .map((check) => `${icon[check.status] ?? "?"} ${check.label}: ${check.message}${check.remedy ? ` — ${check.remedy}` : ""}`)
    .join("\n");
}
