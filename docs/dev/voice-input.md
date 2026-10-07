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

### Feedback events (SA-0MUXZBHX0006IXNZ)

The controller funnels every acknowledgement through a single
`feedback(kind, details)` helper so each transition emits **exactly one**
non-blocking notification and (optionally) one audible cue.

| Transition (`kind`) | Level | Message (indicative) |
|---|---|---|
| `start` — recording begins | `info` | `🎙 Voice input enabled — listening… (Ctrl+Space to stop)` |
| `stop` — recording ends | `info` | `🔇 Voice input disabled — processing…` |
| `sending` — non-empty transcript submitted | `info` | `📤 Voice input: sending "<first words>…"` (truncated) |
| `empty` — nothing captured | `warning` | `Voice input: nothing captured — nothing was sent.` |

The `stop` acknowledgement fires once because `stop()` is state-guarded, so a
manual stop racing the silence auto-stop cannot double-acknowledge. `abort()`
keeps its existing single `error`/`warning` notification (no extra stop
acknowledgement) and clears the footer.

The wording lives in `feedbackMessage()` and is centralised so it can be tuned
without changing the contract; tests assert the presence, level and named state
of each notification rather than exact strings.

When `config.feedbackSound` is not `false`, each feedback event also calls the
injected `playSound` hook (the `index.ts` adapter writes the terminal bell
`\x07`). The hook is injectable so it is unit-testable and fully suppressible,
and a throwing hook is caught and logged so it can never break a transition.
The bell is never relied on for correctness.

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
| `{"type":"start","model","device","computeType","partialIntervalMs","beamSize","vadFilter","initialPrompt"}` | Load the model (once) and report readiness. CLI args provide defaults. |
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
  `cpu` + `int8` and reports the effective device in `ready`. Because
  ctranslate2 loads CUDA libraries lazily, a CUDA failure can also surface on
  the first `transcribe()` (e.g. a missing `libcublas.so.12`); the worker then
  emits a `warning`, reloads the model on `cpu`/`int8` and retries the
  transcription transparently.
- Partials are emitted once per `partialIntervalMs` of *new* audio, and both
  partials and the final transcript re-transcribe the whole utterance buffer so
  the live editor text stays cumulative. Prompt-length dictation is the target;
  a bounded sliding window can be layered on later if latency demands it.
- `beamSize`, `vadFilter` and `initialPrompt` are forwarded to every
  `transcribe()` call: widen the beam search, drop non-speech audio with VAD, or
  bias the decoder with an initial prompt. `language` forces the source
  language. For a 4 GB GPU, `large-v3-turbo` (~1.6 GB) is the accuracy upgrade
  over the default `small`; `large-v3` (~3 GB) is too tight.
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
| `beamSize` | `PI_VOICE_INPUT_BEAM_SIZE` | `5` |
| `vadFilter` | `PI_VOICE_INPUT_VAD_FILTER` | `false` |
| `initialPrompt` | `PI_VOICE_INPUT_INITIAL_PROMPT` | *(none)* |
| `feedbackSound` | `PI_VOICE_INPUT_FEEDBACK_SOUND` | `true` |
| `silenceThreshold` | `PI_VOICE_INPUT_SILENCE_THRESHOLD` | `0.01` |
| `silenceMs` | `PI_VOICE_INPUT_SILENCE_MS` | `3000` |
| `partialCadenceMs` | `PI_VOICE_INPUT_PARTIAL_CADENCE_MS` | `1000` |
| `captureCommand` | `PI_VOICE_INPUT_CAPTURE_COMMAND` | `arecord` |
| `captureArgs` | `PI_VOICE_INPUT_CAPTURE_ARGS` | `-f S16_LE -r 16000 -c 1 -t raw -q` |
| `python` | `PI_VOICE_INPUT_PYTHON` | `python3` |
| `workerScript` | `PI_VOICE_INPUT_WORKER_SCRIPT` | bundled `whisper_worker.py` |
| `startupTimeoutMs` | `PI_VOICE_INPUT_STARTUP_TIMEOUT_MS` | `120000` |
| `shortcuts` | `PI_VOICE_INPUT_SHORTCUTS` | `[]` |
| `targetPaneLabel` | `PI_VOICE_INPUT_TARGET_PANE_LABEL` | `Work Items` |
| `targetPaneId` | `PI_VOICE_INPUT_TARGET_PANE_ID` | *(none)* |

Settings files are searched in order: `$PI_VOICE_INPUT_CONFIG`,
`<project>/.pi/voice-input.json`, `~/.pi/agent/voice-input.json`.

## Herdr voice shortcuts (`herdr-shortcuts.js`)

`shortcuts` maps a spoken phrase to a Herdr key chord; `targetPaneLabel` (or
`targetPaneId`) selects the pane. On the final transcript the controller asks
`sendShortcut(transcript)` (injected from `herdr-shortcuts.js`); when it returns
true the chord was sent and the transcript is **not** submitted to pi.

Resolution and dispatch:

1. `matchShortcut(shortcuts, transcript)` — normalises case, whitespace and
trailing punctuation and returns the first phrase that matches exactly.
2. `resolveTargetPane(panes, {targetPaneLabel, targetPaneId})` — an explicit id
   wins; otherwise the pane whose `label` (or `terminal_title_stripped`) matches
   the label is used.
3. `herdr pane list --workspace $HERDR_WORKSPACE_ID` discovers panes and
   `herdr pane send-keys <pane-id> <key>...` injects the chord. `HERDR_BIN_PATH`
   overrides the binary.

Safety: this is an explicit allowlist — only configured phrases trigger a chord,
and no arbitrary command is run from speech. Discovery/send failures notify the
operator and never throw; a matched phrase is still consumed (not submitted to
pi) so a misheard command cannot leak into the prompt.

The logic is dependency-free with an injectable `run`, so
`tests/unit/test-voice-herdr-shortcuts.mjs` exercises it without a live Herdr
session.

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
| `tests/unit/test-voice-config.mjs` | settings resolution/validation/precedence (incl. `feedbackSound`) |
| `tests/unit/test-voice-doctor.mjs` | preflight checks and severity aggregation |
| `tests/unit/test-voice-controller.mjs` | state machine, toggle, partial replacement, submission, shortcut dispatch, doctor gating, feedback transitions, sound on/off, stop-race, errors |
| `tests/unit/test-voice-herdr-shortcuts.mjs` | phrase matching, target-pane resolution, chord dispatch, failure handling (injected herdr runner) |
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
