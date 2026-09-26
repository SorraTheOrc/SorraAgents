/**
 * Unit tests for pi-client/voice-input/doctor.js
 *
 * Acceptance criteria covered (SA-0MUFSKZGH008PJO8):
 *   - faster-whisper import check with an actionable remedy
 *   - CUDA availability check (and CPU-mode short circuit)
 *   - capture backend availability check
 *   - model download disk-space feasibility check
 *   - structured results, severity aggregation and graceful degradation
 *
 * All external probes are injected — no GPU, microphone or faster-whisper
 * install is required.
 *
 * Run: node --test tests/unit/test-voice-doctor.mjs
 */

import { describe, test } from "node:test";
import assert from "node:assert/strict";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MODULE = join(__dirname, "..", "..", "pi-client", "voice-input", "doctor.js");

const {
  STATUS,
  checkFasterWhisper,
  checkCuda,
  checkCaptureBackend,
  checkDiskSpace,
  runDoctor,
  formatDoctorReport,
  makeCheck,
} = await import(MODULE);

const MB = 1024 * 1024;

/** A config with a CUDA/small profile by default; override per test. */
function config(overrides = {}) {
  return {
    python: "python3",
    device: "cuda",
    model: "small",
    captureCommand: "arecord",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// faster-whisper
// ---------------------------------------------------------------------------

describe("doctor: faster-whisper", () => {
  test("reports ok with the detected version", () => {
    const check = checkFasterWhisper({
      config: config(),
      runCommand: () => ({ ok: true, stdout: "1.0.3\n", stderr: "", code: 0 }),
    });
    assert.equal(check.status, STATUS.ok);
    assert.match(check.message, /1\.0\.3/);
  });

  test("reports an error with pip install guidance when the import fails", () => {
    const check = checkFasterWhisper({
      config: config(),
      runCommand: () => ({ ok: false, stdout: "", stderr: "ModuleNotFoundError: No module named 'faster_whisper'", code: 1 }),
    });
    assert.equal(check.status, STATUS.error);
    assert.match(check.message, /not importable/);
    assert.match(check.remedy, /pip install faster-whisper/);
  });
});

// ---------------------------------------------------------------------------
// CUDA
// ---------------------------------------------------------------------------

describe("doctor: CUDA", () => {
  test("does not probe when CPU inference is configured", () => {
    let probed = false;
    const check = checkCuda({
      config: config({ device: "cpu" }),
      runCommand: () => {
        probed = true;
        return { ok: true, stdout: "", stderr: "", code: 0 };
      },
    });
    assert.equal(check.status, STATUS.ok);
    assert.match(check.message, /CPU inference/);
    assert.equal(probed, false);
  });

  test("reports the GPU name when nvidia-smi succeeds", () => {
    const check = checkCuda({
      config: config(),
      runCommand: () => ({ ok: true, stdout: "NVIDIA GeForce RTX 3050 Laptop GPU\n", stderr: "", code: 0 }),
    });
    assert.equal(check.status, STATUS.ok);
    assert.match(check.message, /RTX 3050/);
  });

  test("warns about CPU fallback when CUDA is unavailable", () => {
    const check = checkCuda({
      config: config(),
      runCommand: () => ({ ok: false, stdout: "", stderr: "command not found", code: 127 }),
    });
    assert.equal(check.status, STATUS.warning);
    assert.match(check.message, /CPU int8/);
    assert.match(check.remedy, /device=cpu/);
  });
});

// ---------------------------------------------------------------------------
// Capture backend
// ---------------------------------------------------------------------------

describe("doctor: capture backend", () => {
  test("ok when the configured command is on PATH", () => {
    const check = checkCaptureBackend({
      config: config(),
      commandExists: (cmd) => cmd === "arecord",
    });
    assert.equal(check.status, STATUS.ok);
    assert.match(check.message, /arecord/);
  });

  test("error with WSL/PulseAudio guidance when the command is missing", () => {
    const check = checkCaptureBackend({
      config: config({ captureCommand: "parecord" }),
      commandExists: () => false,
    });
    assert.equal(check.status, STATUS.error);
    assert.match(check.message, /parecord/);
    assert.match(check.remedy, /PulseAudio/);
  });
});

// ---------------------------------------------------------------------------
// Disk space
// ---------------------------------------------------------------------------

describe("doctor: disk space", () => {
  const cacheDir = "/tmp/voice-cache";

  test("ok when there is ample free space", () => {
    const check = checkDiskSpace({ config: config(), freeBytes: () => 5000 * MB, cacheDir });
    assert.equal(check.status, STATUS.ok);
    assert.match(check.message, /5000 MB free/);
  });

  test("warns when free space is below 1.5x the model size", () => {
    // small = 484 MB; 600 MB free -> warning
    const check = checkDiskSpace({ config: config(), freeBytes: () => 600 * MB, cacheDir });
    assert.equal(check.status, STATUS.warning);
    assert.match(check.message, /600 MB free/);
  });

  test("errors when free space is below the model size", () => {
    const check = checkDiskSpace({ config: config(), freeBytes: () => 100 * MB, cacheDir });
    assert.equal(check.status, STATUS.error);
    assert.match(check.remedy, /Free disk space/);
  });

  test("warns when the model size is unknown", () => {
    const check = checkDiskSpace({
      config: config({ model: "mystery" }),
      freeBytes: () => 5000 * MB,
      cacheDir,
    });
    assert.equal(check.status, STATUS.warning);
    assert.match(check.message, /Unknown model/);
  });

  test("warns when free space cannot be determined", () => {
    const check = checkDiskSpace({ config: config(), freeBytes: () => null, cacheDir });
    assert.equal(check.status, STATUS.warning);
    assert.match(check.message, /Could not determine free disk space/);
  });
});

// ---------------------------------------------------------------------------
// Aggregation
// ---------------------------------------------------------------------------

describe("doctor: aggregation", () => {
  test("a healthy environment reports ok with four checks", () => {
    const result = runDoctor({
      config: config(),
      probes: {
        runCommand: (cmd) =>
          cmd === "nvidia-smi"
            ? { ok: true, stdout: "RTX 3050\n", stderr: "", code: 0 }
            : { ok: true, stdout: "1.0.3\n", stderr: "", code: 0 },
        commandExists: () => true,
        freeBytes: () => 5000 * MB,
        cacheDir: "/tmp/voice-cache",
      },
    });
    assert.equal(result.ok, true);
    assert.equal(result.hasErrors, false);
    assert.equal(result.hasWarnings, false);
    assert.equal(result.checks.length, 4);
  });

  test("a missing capture backend is a critical failure that disables the extension", () => {
    const result = runDoctor({
      config: config(),
      probes: {
        runCommand: () => ({ ok: true, stdout: "1.0.3\n", stderr: "", code: 0 }),
        commandExists: () => false,
        freeBytes: () => 5000 * MB,
        cacheDir: "/tmp/voice-cache",
      },
    });
    assert.equal(result.ok, false);
    assert.equal(result.hasErrors, true);
    assert.equal(result.checks.find((c) => c.id === "capture").status, STATUS.error);
  });

  test("missing CUDA is a warning, not a critical failure (CPU fallback)", () => {
    const result = runDoctor({
      config: config(),
      probes: {
        runCommand: (cmd) =>
          cmd === "nvidia-smi"
            ? { ok: false, stdout: "", stderr: "not found", code: 127 }
            : { ok: true, stdout: "1.0.3\n", stderr: "", code: 0 },
        commandExists: () => true,
        freeBytes: () => 5000 * MB,
        cacheDir: "/tmp/voice-cache",
      },
    });
    assert.equal(result.ok, true);
    assert.equal(result.hasWarnings, true);
    assert.equal(result.checks.find((c) => c.id === "cuda").status, STATUS.warning);
  });

  test("formatDoctorReport renders each check with its status", () => {
    const report = formatDoctorReport([
      makeCheck("a", "Alpha", STATUS.ok, "fine"),
      makeCheck("b", "Beta", STATUS.warning, "degraded", "do X"),
      makeCheck("c", "Gamma", STATUS.error, "broken"),
    ]);
    assert.match(report, /✓ Alpha: fine/);
    assert.match(report, /! Beta: degraded — do X/);
    assert.match(report, /✗ Gamma: broken/);
  });
});
