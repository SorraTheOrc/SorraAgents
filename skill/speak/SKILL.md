---
name: speak
disable-model-invocation: true
description: "Generate audible speech from text via TTS and play it back. Use when asked to speak text aloud."
---

# Speak Skill

Generate audible speech from text input using a Text-to-Speech (TTS) API,
save the resulting WAV file to `<repo-root>/.pi/speak/` with a rolling buffer
of 5 files, and attempt playback.

## Usage

Invoke the underlying bash script directly:

```bash
$(skill_path speak)/scripts/speak.sh "Text to convert to speech"
```

Or set the `TTS_API_URL` environment variable to use a different endpoint
(`--endpoint` takes precedence when both are given):

```bash
TTS_API_URL="http://localhost:8000/v1/audio/speech" $(skill_path speak)/scripts/speak.sh "Hello"

# Or pass the endpoint as an explicit flag (takes precedence over TTS_API_URL):
$(skill_path speak)/scripts/speak.sh --endpoint http://127.0.0.1:8000/v1/audio/speech "Hello"
```

### Timing wrapper (recommended)

For a per-invocation timing report, call the Python wrapper instead of
`speak.sh` directly (per SA-0MT319YGQ002E801 AC4 — shell scripts are not
instrumented internally; callers wrap the call in timing):

```bash
python3 $(skill_path speak)/scripts/speak.py "Text to convert to speech"
```

The wrapper passes all arguments through unchanged and emits a timing report
(step name, elapsed seconds, percentage, total) on **stderr**, keeping stdout
clean for `--stream` mode.

## Arguments

| Argument | Description |
|----------|-------------|
| `text`   | Text string to be spoken (enclose in quotes for multi-word phrases) |
| `--voice <name>` | Server-side voice name to synthesise with (default: `$TTS_VOICE` or `ryan`). Use any voice the endpoint exposes, e.g. `nova`, `ginny_di`, `default`. |
| `--endpoint <url>` | TTS API endpoint to POST to (default: `$TTS_API_URL` or the remote URL below). Takes precedence over `TTS_API_URL`. |
| `--stream` | Write raw audio to stdout (pipe over SSH to a remote player) |
| `--help` | Show usage instructions and exit |

## Environment Variables

| Variable      | Default                                       | Description                              |
|---------------|-----------------------------------------------|------------------------------------------|
| `TTS_API_URL` | `http://100.79.231.101:8000/v1/audio/speech`  | Default TTS API endpoint when `--endpoint` is not given (an explicit `--endpoint` takes precedence) |
| `TTS_VOICE`   | `ryan`                                        | Default voice name when `--voice` is not given (an explicit `--voice` takes precedence) |
| `SPEAK_DIR`   | `<repo-root>/.pi/speak/`                      | Override the output directory            |

## Rolling Buffer

Generated WAV files are stored in `.pi/speak/` with the following naming:

- `speech.wav` (most recent)
- `speech.1.wav`
- `speech.2.wav`
- `speech.3.wav`
- `speech.4.wav` (oldest retained)

When a 6th file is generated, `speech.4.wav` is removed first, ensuring at
most 5 files are retained at any time.

## Playback

The script attempts playback in the following order:

1. `termux-media-player` (Termux/Android) -- preferred when running under Termux
2. `pw-play` (PipeWire) -- preferred on modern Linux desktops
3. `aplay` (ALSA) -- fallback for older Linux or minimal environments
4. `powershell.exe` (WSL) -- fallback when running under WSL
5. `cmd.exe /c start` (WSL) -- final fallback under WSL

Playback failure is **non-fatal**: the WAV file is still generated and saved
even if no audio player is available or playback fails.

## Dependencies

- **curl** (required) -- for making the TTS API request
- **termux-media-player** (optional) -- for audio playback on Termux/Android
- **pw-play** or **aplay** (optional) -- for audio playback on Linux
- **powershell.exe** or **cmd.exe** (optional) -- for audio playback under WSL

## API Format

The script calls an OpenAI-compatible `/v1/audio/speech` endpoint with a JSON
payload:

```json
{
  "model": "tts-1",
  "input": "Text to speak",
  "voice": "ryan"
}
```

The `voice` field defaults to `ryan` for backwards compatibility and can be
selected per invocation with `--voice <name>` or globally with the
`TTS_VOICE` environment variable (`--voice` wins). The endpoint is selected
per invocation with `--endpoint <url>`, else `TTS_API_URL`, else the remote
default (`--endpoint` wins). The local CosyVoice server exposes `ryan`,
`default`, `nova`, and `ginny_di`; any other OpenAI-compatible endpoint may
expose different names.

A 60-second timeout is applied to the API call.

When the endpoint returns a non-2xx status, the script prints the HTTP status
**and** the server's response body on stderr, so an unknown voice reports the
available voices instead of an opaque HTTP error.

## Exit Codes

| Code | Meaning                                        |
|------|------------------------------------------------|
| 0    | Success (WAV generated; playback may have failed) |
| 1    | Error (missing argument, API failure, curl error) |
| 0    | Stream mode: audio written to stdout (pipe to aplay etc.) |

## Examples

```bash
# Basic usage (with timing wrapper)
python3 $(skill_path speak)/scripts/speak.py "Hello, world!"

# Basic usage (direct — no timing report)
$(skill_path speak)/scripts/speak.sh "Hello, world!"

# Multi-word phrase
$(skill_path speak)/scripts/speak.py 'The TTS system is now working.'

# Stream raw audio to stdout (pipe over SSH to local player)
$(skill_path speak)/scripts/speak.py --stream "Hello" | aplay
ssh user@host "$(skill_path speak)/scripts/speak.py --stream 'hi'" | aplay

# Custom API endpoint (environment variable)
TTS_API_URL="http://localhost:8000/v1/audio/speech" $(skill_path speak)/scripts/speak.py "Test"

# Custom API endpoint (explicit flag -- takes precedence over TTS_API_URL)
$(skill_path speak)/scripts/speak.py --endpoint http://127.0.0.1:8000/v1/audio/speech "Hello from the local server."

# Select a specific server-side voice
$(skill_path speak)/scripts/speak.py --voice nova "Hi, I'm Nova."

# Default voice via the environment (an explicit --voice overrides it)
TTS_VOICE=ginny_di $(skill_path speak)/scripts/speak.py "Hello from ginny_di."

# Voice selection also works with --stream
$(skill_path speak)/scripts/speak.py --voice nova --stream "Hello" | aplay

# Custom output directory
SPEAK_DIR="/tmp/my-speech" $(skill_path speak)/scripts/speak.py "Custom output"
```

## See Also

- `$(skill_path speak)/scripts/speak.sh` -- the underlying implementation script (not modified by the timing wrapper)
- `$(skill_path speak)/scripts/speak.py` -- caller-side timing wrapper (SA-0MTACU3CQ0085KVO)
