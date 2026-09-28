/**
 * voice-input / config — configuration resolution and validation.
 *
 * Resolves the extension's settings from (in increasing precedence):
 *   1. built-in defaults
 *   2. a JSON settings file (project `.pi/voice-input.json`, agent
 *      `~/.pi/agent/voice-input.json`, or `$PI_VOICE_INPUT_CONFIG`)
 *   3. `PI_VOICE_INPUT_*` environment variables
 *   4. explicit overrides (tests / programmatic use)
 *
 * `resolveConfig` is a pure function over plain objects so it is fully unit
 * testable; `loadConfig`/`getConfig` add filesystem resolution for the
 * extension. Dependency-free (`.js`) so node:test can import it without a
 * build step, matching the `proxy-sse-signals` convention.
 */

import { readFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Built-in defaults. GPU target is an RTX 3050 (4 GB) → `small` float16. */
export const DEFAULT_CONFIG = Object.freeze({
  keybinding: "ctrl+space",
  model: "small",
  device: "cuda",
  computeType: "float16",
  language: "",
  silenceThreshold: 0.01,
  silenceMs: 3000,
  partialCadenceMs: 1000,
  captureCommand: "arecord",
  captureArgs: ["-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-q"],
  python: "python3",
  workerScript: "",
  startupTimeoutMs: 120000,
});

/** Device values accepted by the worker. */
export const VALID_DEVICES = ["cuda", "cpu", "auto"];

/** Environment variable for each config key. */
const ENV_KEYS = {
  keybinding: "PI_VOICE_INPUT_KEYBINDING",
  model: "PI_VOICE_INPUT_MODEL",
  device: "PI_VOICE_INPUT_DEVICE",
  computeType: "PI_VOICE_INPUT_COMPUTE_TYPE",
  language: "PI_VOICE_INPUT_LANGUAGE",
  silenceThreshold: "PI_VOICE_INPUT_SILENCE_THRESHOLD",
  silenceMs: "PI_VOICE_INPUT_SILENCE_MS",
  partialCadenceMs: "PI_VOICE_INPUT_PARTIAL_CADENCE_MS",
  captureCommand: "PI_VOICE_INPUT_CAPTURE_COMMAND",
  captureArgs: "PI_VOICE_INPUT_CAPTURE_ARGS",
  python: "PI_VOICE_INPUT_PYTHON",
  workerScript: "PI_VOICE_INPUT_WORKER_SCRIPT",
  startupTimeoutMs: "PI_VOICE_INPUT_STARTUP_TIMEOUT_MS",
};

/** Approximate download sizes (MB) for the supported faster-whisper models. */
export const MODEL_SIZES_MB = Object.freeze({
  tiny: 75,
  "tiny.en": 75,
  base: 145,
  "base.en": 145,
  small: 484,
  "small.en": 484,
  medium: 1530,
  "medium.en": 1530,
  "large-v1": 3090,
  "large-v2": 3090,
  "large-v3": 3090,
  "distil-small.en": 332,
  "distil-medium.en": 789,
  "distil-large-v3": 1510,
});

/**
 * Convert environment variables into a partial config object.
 * Unknown/invalid values are passed through as strings and validated later.
 *
 * @param {Record<string, string|undefined>} env
 * @returns {{ partial: object, warnings: string[] }}
 */
export function configFromEnv(env = {}) {
  const partial = {};
  const warnings = [];
  for (const [key, envName] of Object.entries(ENV_KEYS)) {
    const raw = env[envName];
    if (raw === undefined || raw === "") continue;
    if (key === "captureArgs") {
      try {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) throw new Error("not an array");
        partial[key] = parsed;
      } catch {
        // Allow shell-style whitespace-separated args as a convenience.
        partial[key] = raw.split(/\s+/).filter(Boolean);
        warnings.push(
          `${envName} is not a JSON array; interpreted as whitespace-separated arguments.`,
        );
      }
      continue;
    }
    partial[key] = raw;
  }
  return { partial, warnings };
}

/**
 * Coerce and validate a single value.
 * @returns {{ value: any, warning: string|null }}
 */
function coerce(key, raw, fallback) {
  if (raw === undefined || raw === null) return { value: fallback, warning: null };

  const isNumeric =
    key === "silenceThreshold" ||
    key === "silenceMs" ||
    key === "partialCadenceMs" ||
    key === "startupTimeoutMs";

  if (key === "captureArgs") {
    if (!Array.isArray(raw) || raw.some((item) => typeof item !== "string")) {
      return { value: fallback, warning: `${key} must be an array of strings; using the default.` };
    }
    return { value: [...raw], warning: null };
  }

  if (isNumeric) {
    const num = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(num)) {
      return { value: fallback, warning: `${key} must be a finite number; using the default.` };
    }
    if (key === "silenceThreshold" && (num < 0 || num > 1)) {
      return {
        value: fallback,
        warning:
          "silenceThreshold must be between 0 and 1 (normalised RMS); using the default.",
      };
    }
    if (key !== "silenceThreshold" && num < 0) {
      return { value: fallback, warning: `${key} must not be negative; using the default.` };
    }
    return { value: num, warning: null };
  }

  if (typeof raw !== "string") {
    return { value: fallback, warning: `${key} must be a string; using the default.` };
  }
  return { value: raw, warning: null };
}

/**
 * Resolve a validated config from layered sources.
 *
 * @param {object} [options]
 * @param {object} [options.file] settings-file values (lowest precedence after defaults)
 * @param {Record<string,string|undefined>} [options.env] environment variables
 * @param {object} [options.overrides] explicit overrides (highest precedence)
 * @returns {{ config: object, warnings: string[] }}
 */
export function resolveConfig({ file = {}, env = {}, overrides = {} } = {}) {
  const warnings = [];
  const { partial: envPartial, warnings: envWarnings } = configFromEnv(env);
  warnings.push(...envWarnings);

  const layers = [file, envPartial, overrides];
  const config = {};

  for (const key of Object.keys(DEFAULT_CONFIG)) {
    const fallback = DEFAULT_CONFIG[key];
    let raw;
    for (const layer of layers) {
      if (layer && Object.prototype.hasOwnProperty.call(layer, key) && layer[key] !== undefined) {
        raw = layer[key];
      }
    }
    const { value, warning } = coerce(key, raw, fallback);
    if (warning) warnings.push(warning);
    config[key] = value;
  }

  if (!VALID_DEVICES.includes(config.device)) {
    warnings.push(
      `device "${config.device}" is not one of ${VALID_DEVICES.join(", ")}; using "${DEFAULT_CONFIG.device}".`,
    );
    config.device = DEFAULT_CONFIG.device;
  }

  if (config.model && !(config.model in MODEL_SIZES_MB)) {
    warnings.push(
      `model "${config.model}" is not a known faster-whisper size; it will be passed through to faster-whisper unchanged.`,
    );
  }

  return { config, warnings };
}

/** Candidate settings-file paths, most specific first. */
export function configFileCandidates({ cwd = process.cwd(), env = {} } = {}) {
  const candidates = [];
  if (env.PI_VOICE_INPUT_CONFIG) candidates.push(env.PI_VOICE_INPUT_CONFIG);
  if (cwd) candidates.push(join(cwd, ".pi", "voice-input.json"));
  candidates.push(join(homedir(), ".pi", "agent", "voice-input.json"));
  return candidates;
}

/** Read the first existing settings file. @returns {{file: object, path: string|null, warnings: string[]}} */
export function readConfigFile({ cwd = process.cwd(), env = {} } = {}) {
  const warnings = [];
  for (const path of configFileCandidates({ cwd, env })) {
    if (!path || !existsSync(path)) continue;
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        warnings.push(`settings file ${path} must contain a JSON object; ignoring it.`);
        continue;
      }
      return { file: parsed, path, warnings };
    } catch (err) {
      warnings.push(`failed to read settings file ${path}: ${err.message}; ignoring it.`);
    }
  }
  return { file: {}, path: null, warnings };
}

let currentConfig = null;
let currentWarnings = [];

/**
 * Load and cache the extension config from the environment and settings files.
 * @param {object} [options]
 * @returns {{ config: object, warnings: string[], path: string|null }}
 */
export function loadConfig({ cwd = process.cwd(), env = process.env, overrides = {} } = {}) {
  const { file, path, warnings: fileWarnings } = readConfigFile({ cwd, env });
  const { config, warnings } = resolveConfig({ file, env, overrides });
  currentConfig = config;
  currentWarnings = [...fileWarnings, ...warnings];
  return { config, warnings: currentWarnings, path };
}

/**
 * Return the current config, loading defaults on first use.
 * @returns {object}
 */
export function getConfig() {
  if (!currentConfig) loadConfig();
  return currentConfig;
}

/** Warnings produced by the most recent `loadConfig` call. */
export function getConfigWarnings() {
  return currentWarnings;
}

/** Test helper: reset the cached config. */
export function resetConfig() {
  currentConfig = null;
  currentWarnings = [];
}
