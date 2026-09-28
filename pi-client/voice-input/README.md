# voice-input — local voice-to-text for pi

Press **Ctrl+Space** to dictate instead of typing. Audio is captured locally,
transcribed on your own machine by a persistent
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) worker, streamed
into the pi editor as live partial text, and submitted as a normal user message
when you stop. **No audio or text ever leaves your machine.**

This is a pi client extension (`index.ts` + dependency-free logic modules),
loaded by jiti with no build step — consistent with the other `pi-client/`
extensions.

## How it works

1. **Ctrl+Space** (configurable) runs the preflight doctor check, starts an
   audio capture process and (on first use) loads the faster-whisper model.
2. While you speak, 16 kHz mono PCM frames are fed to the worker; partial
   transcripts replace the editor text live.
3. Recording stops — and the prompt is submitted — when you press
   **Ctrl+Space** again or stay silent for 3 s (configurable).
4. Errors (missing dependency, no microphone, busy worker) are shown as
   notifications; the pi session is never crashed.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3 + `faster-whisper` | `python3 -m pip install faster-whisper` |
| CUDA GPU *(optional)* | CUDA build of faster-whisper + cuDNN/cuBLAS; otherwise CPU `int8` fallback |
| Capture backend | `arecord` (Debian/Ubuntu: `sudo apt install alsa-utils`), or `parecord`/`ffmpeg` |
| WSL2 audio | Requires **WSLg/PulseAudio** (or an ALSA→Pulse bridge); see *Troubleshooting* |

The default model is `small` + `cuda` + `float16`, which fits comfortably in
the 4 GB VRAM of an RTX 3050. `large-v3` may OOM; use `medium`/`small` instead.

## Installation

From the `SorraAgents` checkout (the canonical source):

```bash
scripts/install_pi.sh          # symlinks pi-client/* into ~/.pi/agent/extensions/
```

The install is idempotent and sandboxes only the global pi directories. Then
reload pi (`/reload`) or restart it. To develop against this checkout directly:

```bash
pi --extension ./pi-client/voice-input
```

## Usage

| Action | Command |
|---|---|
| Toggle recording | **Ctrl+Space** (or `/voice-input`) |
| Stop + submit | Press **Ctrl+Space** again, or pause > 3 s |
| Preflight check | `/voice-input-doctor` |
| Show state | `/voice-input-status` |

Submission behaves like typing the text and pressing Enter: a normal user
message that honours pi's current steering/follow-up mode (when pi is busy the
transcript is delivered as a steering message). An empty transcript is never
submitted.

### Ctrl+Space conflicts

Terminals and Windows IMEs often intercept **Ctrl+Space**. Change the
keybinding (see below) or use the `/voice-input` command as a fallback — both
toggle the same recording.

## Configuration

Settings are resolved (lowest → highest precedence) from **defaults**, a JSON
settings file, `PI_VOICE_INPUT_*` environment variables, and explicit
overrides.

Settings files (first found wins):

1. `$PI_VOICE_INPUT_CONFIG`
2. `<project>/.pi/voice-input.json`
3. `~/.pi/agent/voice-input.json`

| Key | Env var | Default | Description |
|---|---|---|---|
| `keybinding` | `PI_VOICE_INPUT_KEYBINDING` | `ctrl+space` | Shortcut that toggles recording |
| `model` | `PI_VOICE_INPUT_MODEL` | `small` | faster-whisper model size |
| `device` | `PI_VOICE_INPUT_DEVICE` | `cuda` | `cuda`, `cpu` or `auto` |
| `computeType` | `PI_VOICE_INPUT_COMPUTE_TYPE` | `float16` | e.g. `float16`, `int8` |
| `language` | `PI_VOICE_INPUT_LANGUAGE` | *(auto)* | Force a language, e.g. `en` |
| `silenceThreshold` | `PI_VOICE_INPUT_SILENCE_THRESHOLD` | `0.01` | Normalised RMS below which a frame is silent |
| `silenceMs` | `PI_VOICE_INPUT_SILENCE_MS` | `3000` | Silence before auto-submit (ms) |
| `partialCadenceMs` | `PI_VOICE_INPUT_PARTIAL_CADENCE_MS` | `1000` | Live-partial / pause cadence (ms) |
| `captureCommand` | `PI_VOICE_INPUT_CAPTURE_COMMAND` | `arecord` | Capture executable |
| `captureArgs` | `PI_VOICE_INPUT_CAPTURE_ARGS` | `-f S16_LE -r 16000 -c 1 -t raw -q` | Capture arguments (JSON array, or whitespace-separated) |
| `python` | `PI_VOICE_INPUT_PYTHON` | `python3` | Python interpreter running the worker |
| `workerScript` | `PI_VOICE_INPUT_WORKER_SCRIPT` | *(bundled)* | Override the worker path |
| `startupTimeoutMs` | `PI_VOICE_INPUT_STARTUP_TIMEOUT_MS` | `120000` | Worker readiness timeout |

Example `~/.pi/agent/voice-input.json`:

```json
{
  "keybinding": "ctrl+alt+space",
  "model": "base",
  "device": "cpu",
  "computeType": "int8"
}
```

## Troubleshooting

- **`faster-whisper is not importable`** — install it into the interpreter named
  by `python` (`python3 -m pip install faster-whisper`). A CUDA build also
  needs cuDNN/cuBLAS.
- **`Capture command "arecord" was not found`** — install `alsa-utils`, or set
  `captureCommand`/`captureArgs` to `parecord`/`ffmpeg`.
- **No microphone under WSL2** — ensure **WSLg** is installed and PulseAudio is
  running (`pactl info`), or bridge ALSA to Pulse. Run `/voice-input-doctor` to
  see which check fails.
- **Ctrl+Space does nothing** — your terminal/IME swallowed the key. Change
  `keybinding` or use `/voice-input`.
- **CUDA out of memory** — use a smaller model (`base`/`small`) or
  `device=cpu`.
- **Slow first press** — the model loads once per session; subsequent
  recordings are immediate.

## Privacy

Transcription runs locally through the bundled Python worker. There is no
cloud/network speech-to-text; the worker never opens a network connection.

## Files

| File | Purpose |
|---|---|
| `index.ts` | pi `ExtensionAPI` adapter: shortcut, commands, lifecycle |
| `controller.js` | idle → recording → submitting state machine, submit/partials |
| `recorder.js` | capture process, PCM framing, RMS silence detection |
| `whisper-client.js` | persistent worker lifecycle + JSON protocol client |
| `whisper_worker.py` | faster-whisper worker (model loaded once per session) |
| `config.js` | settings resolution and validation |
| `doctor.js` | faster-whisper / CUDA / capture / disk preflight checks |

See [`docs/dev/voice-input.md`](../../docs/dev/voice-input.md) for the design
and protocol reference.
