#!/usr/bin/env bash
set -euo pipefail
# Simple helper to run the opencode summarizer on a string or file.
# Usage:
#   ./scripts/opencode-summarize.sh "long text to summarize"
#   ./scripts/opencode-summarize.sh -f path/to/file.txt

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 \"text to summarize\"  or: $0 -f file"
  exit 2
fi

if [ "$1" = "-f" ]; then
  if [ -z "${2-}" ]; then
    echo "Error: missing file path after -f"
    exit 2
  fi
  if [ ! -f "$2" ]; then
    echo "Error: file not found: $2"
    exit 2
  fi
  content=$(cat "$2")
else
  # Join all arguments as the content
  content="$*"
fi

# Build the prompt safely and run opencode. We use printf to preserve
# newlines and ensure the content is passed as a single argument.
prompt=$(printf "summarize this content in under 1000 characters: %s" "$content")

exec opencode run "$prompt"
