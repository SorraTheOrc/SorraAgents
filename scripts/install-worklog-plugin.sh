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

# If the source is inside plugins/ or examples/, prefer copying only the basename
# into the target dir under the same name (e.g. plugins/wl_ampa/ampa.mjs -> ampa.mjs)
if [ "${SRC#plugins/}" != "$SRC" ] || [ "${SRC#examples/}" != "$SRC" ]; then
  BASENAME=$(basename "$SRC")
else
  BASENAME=$(basename "$SRC")
fi

if [ ! -f "$SRC" ]; then
  echo "Source file not found: $SRC"
  exit 2
fi

mkdir -p "$TARGET_DIR"
cp -f "$SRC" "$TARGET_DIR/$BASENAME"
echo "Installed Worklog plugin $SRC to $TARGET_DIR/$BASENAME"

# If the script was called without explicit target, and the target is the default
# `.worklog/plugins`, also place the installer itself into `.worklog/plugins` for
# convenience so users can run the installer from the project root.
if [ "${2:-}" = "" ] && [ "$TARGET_DIR" = ".worklog/plugins" ]; then
  cp -f "$0" "$TARGET_DIR/install-worklog-plugin.sh"
  echo "Copied installer to $TARGET_DIR/install-worklog-plugin.sh"
fi
