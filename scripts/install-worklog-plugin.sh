#!/usr/bin/env sh
# Install a Worklog plugin into a project or user plugin directory.
# Usage: ./scripts/install-worklog-plugin.sh <source-file> [target-dir]
# Example: ./scripts/install-worklog-plugin.sh examples/ampa.mjs

set -e

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <source-file> [target-dir]"
  exit 2
fi

SRC="$1"
TARGET_DIR=${2:-.worklog/plugins}

if [ ! -f "$SRC" ]; then
  echo "Source file not found: $SRC"
  exit 2
fi

mkdir -p "$TARGET_DIR"
cp -f "$SRC" "$TARGET_DIR/$(basename "$SRC")"
echo "Installed Worklog plugin $SRC to $TARGET_DIR/$(basename "$SRC")"
