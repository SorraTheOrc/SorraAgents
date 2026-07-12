---
name: speak
description: Generate audible speech from text using a TTS API and play it back. Invokes scripts/speak.sh to convert text to a WAV file, manages a rolling buffer of 5 recent audio files, and plays the result.
---

# Speak Skill

Generate audible speech from text input using a Text-to-Speech (TTS) API,
save the resulting WAV file to `<repo-root>/.pi/speak/` with a rolling buffer
of 5 files, and attempt playback.

## Usage

Invoke the underlying bash script directly:

```bash
./scripts/speak.sh "Text to convert to speech"
```

Or set the `TTS_API_URL` environment variable to use a different endpoint:

```bash
TTS_API_URL="http://localhost:8000/v1/audio/speech" ./scripts/speak.sh "Hello"
```

## Arguments

| Argument | Description |
|----------|-------------|
| `text`   | Text string to be spoken (enclose in quotes for multi-word phrases) |
| `--help` | Show usage instructions and exit |

## Environment Variables

| Variable      | Default                                       | Description                              |
|---------------|-----------------------------------------------|------------------------------------------|
| `TTS_API_URL` | `http://100.79.231.101:8000/v1/audio/speech`  | Override the TTS API endpoint            |
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

1. `pw-play` (PipeWire) -- preferred on modern Linux desktops
2. `aplay` (ALSA) -- fallback for older Linux or minimal environments
3. `powershell.exe` (WSL) -- fallback when running under WSL
4. `cmd.exe /c start` (WSL) -- final fallback under WSL

Playback failure is **non-fatal**: the WAV file is still generated and saved
even if no audio player is available or playback fails.

## Dependencies

- **curl** (required) -- for making the TTS API request
- **pw-play** or **aplay** (optional) -- for audio playback on Linux
- **powershell.exe** or **cmd.exe** (optional) -- for audio playback under WSL

## API Format

The script calls an OpenAI-compatible `/v1/audio/speech` endpoint with a JSON
payload:

```json
{
  "model": "tts-1",
  "input": "Text to speak",
  "voice": "alloy"
}
```

A 60-second timeout is applied to the API call.

## Exit Codes

| Code | Meaning                                        |
|------|------------------------------------------------|
| 0    | Success (WAV generated; playback may have failed) |
| 1    | Error (missing argument, API failure, curl error) |

## Examples

```bash
# Basic usage
./scripts/speak.sh "Hello, world!"

# Multi-word phrase
./scripts/speak.sh 'The TTS system is now working.'

# Custom API endpoint
TTS_API_URL="http://localhost:8000/v1/audio/speech" ./scripts/speak.sh "Test"

# Custom output directory
SPEAK_DIR="/tmp/my-speech" ./scripts/speak.sh "Custom output"
```

## See Also

- `scripts/speak.sh` -- the underlying implementation script
