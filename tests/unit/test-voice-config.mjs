/**
 * Unit tests for pi-client/voice-input/config.js
 *
 * Acceptance criteria covered (SA-0MUFSKZGH008PJO8):
 *   - defaults for every setting (keybinding, model, device, compute type,
 *     silence/pause timing, capture command)
 *   - resolution precedence: defaults < file < environment < overrides
 *   - validation keeps the default and warns on invalid values
 *   - settings-file loading and error handling
 *
 * Run: node --test tests/unit/test-voice-config.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "config.js");

const {
  DEFAULT_CONFIG,
  resolveConfig,
  configFromEnv,
  configFileCandidates,
  readConfigFile,
  loadConfig,
  getConfig,
  getConfigWarnings,
  resetConfig,
} = await import(MODULE);

// ---------------------------------------------------------------------------
// Defaults and precedence
// ---------------------------------------------------------------------------

describe("config defaults", () => {
  test("resolves the documented defaults with no sources", () => {
    const { config, warnings } = resolveConfig();
    assert.deepEqual(config, { ...DEFAULT_CONFIG });
    assert.deepEqual(warnings, []);
  });

  test("defaults target the RTX 3050 4 GB profile", () => {
    const { config } = resolveConfig();
    assert.equal(config.model, "small");
    assert.equal(config.device, "cuda");
    assert.equal(config.computeType, "float16");
    assert.equal(config.keybinding, "ctrl+space");
    assert.equal(config.silenceMs, 3000);
    assert.equal(config.partialCadenceMs, 1000);
    assert.equal(config.captureCommand, "arecord");
  });
});

describe("config precedence", () => {
  test("file values override defaults, env overrides file, overrides win", () => {
    const { config } = resolveConfig({
      file: { model: "base", silenceMs: 5000 },
      env: { PI_VOICE_INPUT_MODEL: "medium" },
      overrides: { model: "large-v3" },
    });
    assert.equal(config.model, "large-v3");
    assert.equal(config.silenceMs, 5000);
  });

  test("numeric environment values are coerced", () => {
    const { config } = resolveConfig({
      env: {
        PI_VOICE_INPUT_SILENCE_MS: "2500",
        PI_VOICE_INPUT_PARTIAL_CADENCE_MS: "400",
        PI_VOICE_INPUT_SILENCE_THRESHOLD: "0.02",
      },
    });
    assert.equal(config.silenceMs, 2500);
    assert.equal(config.partialCadenceMs, 400);
    assert.equal(config.silenceThreshold, 0.02);
  });

  test("environment variable mapping covers every configurable key", () => {
    const { partial, warnings } = configFromEnv({
      PI_VOICE_INPUT_KEYBINDING: "ctrl+alt+space",
      PI_VOICE_INPUT_MODEL: "base",
      PI_VOICE_INPUT_DEVICE: "cpu",
      PI_VOICE_INPUT_COMPUTE_TYPE: "int8",
      PI_VOICE_INPUT_LANGUAGE: "en",
      PI_VOICE_INPUT_CAPTURE_COMMAND: "parecord",
      PI_VOICE_INPUT_CAPTURE_ARGS: '["--rate=16000"]',
      PI_VOICE_INPUT_PYTHON: "python3.12",
      PI_VOICE_INPUT_WORKER_SCRIPT: "/tmp/worker.py",
      PI_VOICE_INPUT_STARTUP_TIMEOUT_MS: "30000",
    });
    assert.deepEqual(partial, {
      keybinding: "ctrl+alt+space",
      model: "base",
      device: "cpu",
      computeType: "int8",
      language: "en",
      captureCommand: "parecord",
      captureArgs: ["--rate=16000"],
      python: "python3.12",
      workerScript: "/tmp/worker.py",
      startupTimeoutMs: "30000",
    });
    assert.deepEqual(warnings, []);
  });

  test("whitespace capture args are accepted with a warning", () => {
    const { partial, warnings } = configFromEnv({
      PI_VOICE_INPUT_CAPTURE_ARGS: "--format=s16le --rate=16000",
    });
    assert.deepEqual(partial.captureArgs, ["--format=s16le", "--rate=16000"]);
    assert.equal(warnings.length, 1);
    assert.match(warnings[0], /not a JSON array/);
  });
});

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

describe("config validation", () => {
  test("an unknown device is rejected with a warning", () => {
    const { config, warnings } = resolveConfig({ overrides: { device: "tpu" } });
    assert.equal(config.device, "cuda");
    assert.ok(warnings.some((w) => /device "tpu"/.test(w)));
  });

  test("a non-numeric silence timeout falls back to the default", () => {
    const { config, warnings } = resolveConfig({ overrides: { silenceMs: "soon" } });
    assert.equal(config.silenceMs, 3000);
    assert.ok(warnings.some((w) => /silenceMs must be a finite number/.test(w)));
  });

  test("a negative pause cadence is rejected", () => {
    const { config, warnings } = resolveConfig({ overrides: { partialCadenceMs: -1 } });
    assert.equal(config.partialCadenceMs, 1000);
    assert.ok(warnings.some((w) => /partialCadenceMs must not be negative/.test(w)));
  });

  test("an out-of-range silence threshold is rejected", () => {
    const { config, warnings } = resolveConfig({ overrides: { silenceThreshold: 2 } });
    assert.equal(config.silenceThreshold, 0.01);
    assert.ok(warnings.some((w) => /silenceThreshold must be between 0 and 1/.test(w)));
  });

  test("non-array capture args fall back to the default", () => {
    const { config, warnings } = resolveConfig({ overrides: { captureArgs: "arecord" } });
    assert.deepEqual(config.captureArgs, DEFAULT_CONFIG.captureArgs);
    assert.ok(warnings.some((w) => /captureArgs must be an array of strings/.test(w)));
  });

  test("an unknown model is preserved with a warning (faster-whisper validates it)", () => {
    const { config, warnings } = resolveConfig({ overrides: { model: "gigantic" } });
    assert.equal(config.model, "gigantic");
    assert.ok(warnings.some((w) => /not a known faster-whisper size/.test(w)));
  });
});

// ---------------------------------------------------------------------------
// Settings file loading
// ---------------------------------------------------------------------------

describe("config file loading", () => {
  test("reads .pi/voice-input.json from the project directory", () => {
    const cwd = mkdtempSync(join(tmpdir(), "voice-config-"));
    mkdirSync(join(cwd, ".pi"));
    writeFileSync(join(cwd, ".pi", "voice-input.json"), JSON.stringify({ model: "base" }));

    const { file, path, warnings } = readConfigFile({ cwd, env: {} });
    assert.equal(file.model, "base");
    assert.equal(path, join(cwd, ".pi", "voice-input.json"));
    assert.deepEqual(warnings, []);
  });

  test("PI_VOICE_INPUT_CONFIG takes precedence over the project file", () => {
    const cwd = mkdtempSync(join(tmpdir(), "voice-config-"));
    const explicit = join(cwd, "explicit.json");
    writeFileSync(explicit, JSON.stringify({ model: "tiny" }));

    const { file, path } = readConfigFile({ cwd, env: { PI_VOICE_INPUT_CONFIG: explicit } });
    assert.equal(file.model, "tiny");
    assert.equal(path, explicit);
  });

  test("invalid JSON is ignored with a warning", () => {
    const cwd = mkdtempSync(join(tmpdir(), "voice-config-"));
    mkdirSync(join(cwd, ".pi"));
    writeFileSync(join(cwd, ".pi", "voice-input.json"), "{ not json");

    const { file, warnings } = readConfigFile({ cwd, env: {} });
    assert.deepEqual(file, {});
    assert.ok(warnings.some((w) => /failed to read settings file/.test(w)));
  });

  test("non-object JSON is ignored with a warning", () => {
    const cwd = mkdtempSync(join(tmpdir(), "voice-config-"));
    mkdirSync(join(cwd, ".pi"));
    writeFileSync(join(cwd, ".pi", "voice-input.json"), "[1,2,3]");
    const { file, warnings } = readConfigFile({ cwd, env: {} });
    assert.deepEqual(file, {});
    assert.ok(warnings.some((w) => /must contain a JSON object/.test(w)));
  });

  test("configFileCandidates prefers explicit, then project, then agent", () => {
    const candidates = configFileCandidates({ cwd: "/proj", env: { PI_VOICE_INPUT_CONFIG: "/x.json" } });
    assert.equal(candidates[0], "/x.json");
    assert.equal(candidates[1], join("/proj", ".pi", "voice-input.json"));
  });

  test("loadConfig/getConfig cache and combine file+env+defaults", () => {
    resetConfig();
    const cwd = mkdtempSync(join(tmpdir(), "voice-config-"));
    mkdirSync(join(cwd, ".pi"));
    writeFileSync(join(cwd, ".pi", "voice-input.json"), JSON.stringify({ keybinding: "ctrl+alt+v" }));

    const loaded = loadConfig({ cwd, env: { PI_VOICE_INPUT_MODEL: "base" } });
    assert.equal(loaded.config.keybinding, "ctrl+alt+v");
    assert.equal(loaded.config.model, "base");
    assert.equal(loaded.config.device, "cuda"); // default retained
    assert.equal(getConfig(), loaded.config);
    assert.equal(getConfigWarnings(), loaded.warnings);

    resetConfig();
    assert.notEqual(getConfig(), loaded.config);
  });
});
