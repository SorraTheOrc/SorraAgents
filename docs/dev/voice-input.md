# Voice input (local voice-to-text) — design and protocol reference

Work item: **SA-0MUFEVYAF002GD3A** — *Voice-to-text pi extension using
faster-whisper (Ctrl+Space)*.

The `voice-input` pi client extension lets an operator dictate prompts. Audio
is captured locally, transcribed on the operator's own machine by a persistent
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) worker, streamed
into the editor as live partials, and submitted as a normal user message.
Nothing leaves the machine.

Source: [`pi-client/voice-input/`](../../pi-client/voice-input/). User-facing
setup lives in the extension [`README.md`](../../pi-client/voice-input/README.md);
this document is the maintainer reference.

## Architecture

```
 pi TUI (Ctrl+Space)
        │  registerShortcut
        ▼
 ┌───────────────────┐   toggle()    ┌──────────────────────┐
 │ controller.js     │──────────────▶│ recorder.js          │
 │ state machine,    │  data frames  │ spawn capture (arecord)│
 │ partials, submit  │◀──────────────│ 16 kHz PCM + RMS      │
 └─────────┬─────────┘   pause/silence└──────────────────────┘
           │ feed(pcm)                       │ silence → auto-stop
           ▼                                 │
 ┌───────────────────┐   JSONL (stdin/stdout) ┌───────────────────┐
 │ whisper-client.js │◀──────────────────────▶│ whisper_worker.py │
 │ persistent worker │   partial / final      │ faster-whisper    │
 └───────────────────┘                        └───────────────────┘
           ▲
           │ checkDoctor()
 ┌─────────┴─────────┐   ┌───────────────┐
 │ doctor.js         │   │ config.js     │
 │ preflight checks  │   │ settings      │
 └───────────────────┘   └───────────────┘
```

Design rules:

- **Logic modules are dependency-free `.js`** so `node:test` can import them
  without a build step; `index.ts` is a thin `ExtensionAPI` adapter. This
  mirrors [`proxy-sse-signals`](../../pi-client/proxy-sse-signals/index.ts).
- **The worker is persistent per session.** The model is expensive to load, so
  it is started once (lazily on the first recording) and reused; each
  `finalise` clears the utterance buffer.
- **All transcription is local.** The worker makes no network calls.
- **Nothing crashes the pi session.** Recorder/client failures are surfaced as
  notifications; `error` events are only emitted when a listener is attached.

## Recording state machine (`controller.js`)

```
        toggle()                 stop() / silence
 idle ───────────▶ recording ───────────────────▶ submitting
   ▲                   │                              │
   │                   │ recorder/client error        │ finalise()
   └───────────────────┴──────────────────────────────┘
                       abort()  (notify + restore editor)
```

- `idle → recording`: doctor gate → start worker (first time) → start capture →
  set the footer indicator.
- `recording → submitting`: SIGINT the capture process, `finalise()` the
  worker.
- `submitting → idle`: if a non-empty transcript, set the editor text and call
  `pi.sendUserMessage()` (delivered as a *steer* message when pi is busy); if
  empty, restore the pre-recording editor and notify.
- **Single submission:** `stop()` is guarded by the state, so a manual stop and
  a silence auto-stop racing each other submit exactly once.
- Partials are ignored outside `recording`, so late worker messages cannot
  clobber a submitted editor.

## Capture and silence detection (`recorder.js`)

- Spawns a configurable capture command; the default `arecord -f S16_LE -r
  16000 -c 1 -t raw -q` produces 16 kHz mono signed 16-bit little-endian PCM.
- `PcmFramer` reassembles arbitrary stdout chunks into fixed 10 ms frames
  (320 bytes at 16 kHz) so timing is deterministic.
- `computeRms` normalises frame energy to `0..1`; a frame is *speech* when
  `rms >= silenceThreshold`.
- `SilenceDetector` runs on the **audio timeline** (frame end time), not wall
  clock: it emits `pause` on the partial cadence and a single `silence` event
  once the configured silent duration elapses. Speech resets the run.
- Capture-process failures produce an actionable `error` (command, exit code,
  stderr); `stop()` sends `SIGINT`.

## Worker protocol (`whisper_worker.py` / `whisper-client.js`)

Line-delimited JSON, one object per line. The worker reads commands from stdin
and writes responses to stdout.

### Client → worker

| Message | Purpose |
|---|---|
| `{"type":"start","model","device","computeType","partialIntervalMs"}` | Load the model (once) and report readiness. CLI args provide defaults. |
| `{"type":"feed","audio":"<base64 int16 PCM>"}` | Append PCM; may trigger a `partial`. |
| `{"type":"finalise"}` | Transcribe all buffered audio, emit `final`, then clear the buffer. |
| `{"type":"stop"}` | Acknowledge with `stopped` and exit cleanly. |

### Worker → client

| Message | Purpose |
|---|---|
| `{"type":"ready","model","device","computeType"}` | Model loaded; `device` is the *effective* device after CPU fallback. |
| `{"type":"partial","text","sequence"}` | Live transcript of the buffered audio. |
| `{"type":"final","text","sequence"}` | Complete transcript for the utterance. |
| `{"type":"stopped"}` | Shutdown acknowledgement. |
| `{"type":"warning","message"}` | Non-fatal condition (e.g. CUDA unavailable → CPU fallback). |
| `{"type":"error","message"}` | Fatal or recoverable error; a fatal startup error is followed by exit code 1. |

Notes:

- Model/device/compute-type come from the `start` command, with CLI arguments
  as defaults. If `device=cuda` cannot initialise, the worker falls back to
  `cpu` + `int8` and reports the effective device in `ready`.
- Partials are emitted once per `partialIntervalMs` of *new* audio, and both
  partials and the final transcript re-transcribe the whole utterance buffer so
  the live editor text stays cumulative. Prompt-length dictation is the target;
  a bounded sliding window can be layered on later if latency demands it.
- A missing `faster-whisper` install emits an actionable `error` and exits 1
  (the extension disables itself gracefully via the doctor instead of crashing).

## Preflight checks (`doctor.js`)

`runDoctor()` returns `{ ok, hasErrors, hasWarnings, checks }`. Each check has
`{ id, label, status, message, remedy }`.

| Check | `error` | `warning` | `ok` |
|---|---|---|---|
| `faster-whisper` | import fails | — | import succeeds |
| `cuda` | — | `nvidia-smi`/CUDA unavailable with `device=cuda` | CUDA available, or `device=cpu` |
| `capture` | configured capture command not on PATH | — | command available |
| `disk` | free space < model size | free space < 1.5× model size, unknown model, or unknown free space | ample space |

A critical (`error`) result disables the extension on start and shows the
remedy; warnings are surfaced but do not block. `/voice-input-doctor` prints the
full report.

## Configuration reference

Resolution precedence (lowest → highest): defaults → settings file →
`PI_VOICE_INPUT_*` environment → explicit overrides. Invalid values keep the
default and produce a warning; an unknown model is passed through to
faster-whisper unchanged.

| Key | Env var | Default |
|---|---|---|
| `keybinding` | `PI_VOICE_INPUT_KEYBINDING` | `ctrl+space` |
| `model` | `PI_VOICE_INPUT_MODEL` | `small` |
| `device` | `PI_VOICE_INPUT_DEVICE` | `cuda` |
| `computeType` | `PI_VOICE_INPUT_COMPUTE_TYPE` | `float16` |
| `language` | `PI_VOICE_INPUT_LANGUAGE` | *(auto)* |
| `silenceThreshold` | `PI_VOICE_INPUT_SILENCE_THRESHOLD` | `0.01` |
| `silenceMs` | `PI_VOICE_INPUT_SILENCE_MS` | `3000` |
| `partialCadenceMs` | `PI_VOICE_INPUT_PARTIAL_CADENCE_MS` | `1000` |
| `captureCommand` | `PI_VOICE_INPUT_CAPTURE_COMMAND` | `arecord` |
| `captureArgs` | `PI_VOICE_INPUT_CAPTURE_ARGS` | `-f S16_LE -r 16000 -c 1 -t raw -q` |
| `python` | `PI_VOICE_INPUT_PYTHON` | `python3` |
| `workerScript` | `PI_VOICE_INPUT_WORKER_SCRIPT` | bundled `whisper_worker.py` |
| `startupTimeoutMs` | `PI_VOICE_INPUT_STARTUP_TIMEOUT_MS` | `120000` |

Settings files are searched in order: `$PI_VOICE_INPUT_CONFIG`,
`<project>/.pi/voice-input.json`, `~/.pi/agent/voice-input.json`.

## Install wiring

[`scripts/install_pi.sh`](../../scripts/install_pi.sh) symlinks every
`pi-client/*` directory containing an `index.ts`/`index.js` into
`~/.pi/agent/extensions/`, including `voice-input`. The wiring is idempotent
and covered by
[`tests/test_install_pi_extensions.py`](../../tests/test_install_pi_extensions.py).

## Tests

Pure unit tests (no microphone, GPU or model download):

| File | Covers |
|---|---|
| `tests/unit/test-voice-recorder.mjs` | RMS, PCM framing, silence/pause timing, capture errors, SIGINT shutdown |
| `tests/unit/test-voice-whisper-client.mjs` | worker lifecycle, JSON protocol, partials, finalise, stop, crash handling |
| `tests/unit/test-voice-whisper-worker.mjs` | real Python worker against a stub `faster_whisper`: start/ready/feed/partial/finalise/stop, CUDA fallback, missing dependency, buffer reset |
| `tests/unit/test-voice-config.mjs` | settings resolution/validation/precedence |
| `tests/unit/test-voice-doctor.mjs` | preflight checks and severity aggregation |
| `tests/unit/test-voice-controller.mjs` | state machine, toggle, partial replacement, submission, doctor gating, errors |
| `tests/unit/test-voice-input.mjs` | end-to-end wiring: controller → recorder → real worker → auto-submit |

The worker tests inject a stub `faster_whisper` module via `PYTHONPATH`, so the
real line-delimited JSON loop is exercised without the dependency installed.

## Scope and non-goals

Out of scope: cloud speech-to-text, multi-language selection UI, speaker
diarisation, and wake-word activation. Ctrl+Space is commonly intercepted by
terminals/IMEs, so the keybinding is configurable and `/voice-input` is the
documented fallback.

## Related work

- **SA-0MRHSXIT00089DQ3** — the `speak` skill (text-to-speech), the inverse
  direction and precedent for an external-process audio bridge.
- **SA-0MTWOYFLU004LB2R** — TTS in the standup skill.
