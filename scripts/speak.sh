#!/bin/bash
#
# speak.sh — Text-to-Speech (TTS) audio generation and playback
#
# Calls an OpenAI-compatible TTS API to convert text into a WAV audio file,
# saves it to <repo-root>/.pi/speak/ with a rolling buffer of 5 files, and
# attempts playback.
#
# Usage:
#   ./scripts/speak.sh "Text to speak"
#   ./scripts/speak.sh --help
#
# Dependencies:
#   - curl (required for API call)
#   - pw-play or aplay (optional, for playback)
#
# Environment:
#   Works in native Linux (ALSA/PipeWire) and WSL environments.
#
# Rolling buffer:
#   Files are stored in .pi/speak/ with the following naming:
#   - speech.wav   (most recent)
#   - speech.1.wav
#   - speech.2.wav
#   - speech.3.wav
#   - speech.4.wav (oldest retained)
#
#   When a 6th file would be created, speech.4.wav is removed first,
#   ensuring at most 5 files are retained at any time.
#
# See also: skill/speak/SKILL.md

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TTS_API_URL="${TTS_API_URL:-http://100.79.231.101:8000/v1/audio/speech}"
CURL_TIMEOUT=60

# Determine script directory and repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SPEAK_DIR="${SPEAK_DIR:-$REPO_ROOT/.pi/speak}"

# ---------------------------------------------------------------------------
# Help / Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $(basename "$0") <text>

Convert text to speech using the TTS API and play the resulting audio.

Arguments:
  text    Text string to be spoken (enclose in quotes for multi-word phrases)

Options:
  --help  Show this help message and exit

Environment variables:
  SPEAK_DIR    Override the output directory (default: <repo-root>/.pi/speak/)
  TTS_API_URL  Override the TTS API endpoint (default: $TTS_API_URL)

Examples:
  $(basename "$0") "Hello, world!"
  $(basename "$0") 'The TTS system is now working.'
  TTS_API_URL="http://localhost:8000/v1/audio/speech" $(basename "$0") "Test"
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

if [[ $# -eq 0 ]]; then
    echo "Error: No text argument provided." >&2
    usage >&2
    exit 1
fi

if [[ "$1" == "--help" ]]; then
    usage
    exit 0
fi

TEXT="$1"

# ---------------------------------------------------------------------------
# Ensure speak directory exists
# ---------------------------------------------------------------------------

mkdir -p "$SPEAK_DIR"

# ---------------------------------------------------------------------------
# Rotate rolling buffer (max 5 files)
# ---------------------------------------------------------------------------
# The buffer supports: speech.wav (newest), speech.1.wav, ..., speech.4.wav (oldest)
# Rotation: speech.3.wav → speech.4.wav, speech.2.wav → speech.3.wav,
#           speech.1.wav → speech.2.wav, speech.wav → speech.1.wav

_rotate_buffer() {
    local dir="$1"

    # Remove oldest file if it exists (speech.4.wav)
    if [[ -f "$dir/speech.4.wav" ]]; then
        rm -f "$dir/speech.4.wav"
    fi

    # Shift files up: 3→4, 2→3, 1→2, wav→1 (in reverse order to avoid overwrites)
    if [[ -f "$dir/speech.3.wav" ]]; then
        mv "$dir/speech.3.wav" "$dir/speech.4.wav"
    fi
    if [[ -f "$dir/speech.2.wav" ]]; then
        mv "$dir/speech.2.wav" "$dir/speech.3.wav"
    fi
    if [[ -f "$dir/speech.1.wav" ]]; then
        mv "$dir/speech.1.wav" "$dir/speech.2.wav"
    fi
    if [[ -f "$dir/speech.wav" ]]; then
        mv "$dir/speech.wav" "$dir/speech.1.wav"
    fi
}

_rotate_buffer "$SPEAK_DIR"

# ---------------------------------------------------------------------------
# Generate speech via TTS API
# ---------------------------------------------------------------------------

OUTPUT_FILE="$SPEAK_DIR/speech.wav"

echo "Generating speech for: $TEXT" >&2
echo "Calling TTS API: $TTS_API_URL" >&2

# Build JSON payload (OpenAI-compatible audio/speech format)
JSON_PAYLOAD=$(cat <<EOF
{
  "model": "tts-1",
  "input": $(echo "$TEXT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))"),
  "voice": "alloy"
}
EOF
)

# Use curl to call the TTS API
# Temporarily disable set -e for the curl call to handle failures gracefully
set +e
HTTP_STATUS=$(curl -s -w "%{http_code}" -X POST "$TTS_API_URL" \
    -H "Content-Type: application/json" \
    -d "$JSON_PAYLOAD" \
    --max-time "$CURL_TIMEOUT" \
    -o "$OUTPUT_FILE" 2>/dev/null)
CURL_EXIT=$?
set -e

if [[ $CURL_EXIT -ne 0 ]]; then
    echo "Error: curl failed (exit code $CURL_EXIT)" >&2
    rm -f "$OUTPUT_FILE"
    exit 1
fi

# Check HTTP status
if [[ -z "$HTTP_STATUS" || "$HTTP_STATUS" -lt 200 || "$HTTP_STATUS" -ge 300 ]]; then
    echo "Error: TTS API returned HTTP $HTTP_STATUS" >&2
    # Remove empty output file if request failed
    rm -f "$OUTPUT_FILE"
    exit 1
fi

echo "Speech generated: $OUTPUT_FILE ($(stat -c%s "$OUTPUT_FILE" 2>/dev/null || echo "unknown") bytes)" >&2

# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
# Try pw-play first (PipeWire), fall back to aplay (ALSA)

# Playback (non-fatal — WAV file is still generated even if playback fails)
set +e
_play_audio() {
    if command -v pw-play &>/dev/null; then
        echo "Playing via pw-play..." >&2
        pw-play "$1" &>/dev/null && return 0
        echo "Warning: pw-play playback failed" >&2
    fi

    if command -v aplay &>/dev/null; then
        echo "Playing via aplay..." >&2
        aplay "$1" &>/dev/null && return 0
        echo "Warning: aplay playback failed" >&2
    fi

    # WSL fallback: try Windows audio player
    if command -v powershell.exe &>/dev/null; then
        echo "Playing via Windows Media Player (WSL)..." >&2
        WIN_PATH=$(wslpath -w "$1" 2>/dev/null || echo "$1")
        powershell.exe -Command "Start-Process '$WIN_PATH'" &>/dev/null && return 0 || true
    fi

    if command -v cmd.exe &>/dev/null; then
        echo "Playing via cmd.exe start (WSL)..." >&2
        WIN_PATH=$(wslpath -w "$1" 2>/dev/null || echo "$1")
        cmd.exe /c start "" "$WIN_PATH" &>/dev/null && return 0 || true
    fi

    echo "Warning: No audio player found. Speech file saved at $1" >&2
    return 1
}

_play_audio "$OUTPUT_FILE" || true
set -e

echo "Done." >&2
