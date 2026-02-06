#!/usr/bin/env sh
# Install a Worklog plugin into a project or user plugin directory.
# Usage: ./plugins/install-worklog-plugin.sh <source-file> [target-dir]
# Example: ./plugins/install-worklog-plugin.sh plugins/wl_ampa/ampa.mjs

set -e

DEFAULT_SRC="plugins/wl_ampa/ampa.mjs"

# If no source provided, use the canonical plugin in plugins/wl_ampa/ampa.mjs
if [ "$#" -lt 1 ]; then
  SRC="$DEFAULT_SRC"
else
  SRC="$1"
fi

TARGET_DIR=${2:-.worklog/plugins}

# Determine basename for installation
BASENAME=$(basename "$SRC")

if [ ! -f "$SRC" ]; then
  echo "Source file not found: $SRC"
  echo "If you intended to install the canonical plugin, run without arguments to use $DEFAULT_SRC"
  exit 2
fi

mkdir -p "$TARGET_DIR"
cp -f "$SRC" "$TARGET_DIR/$BASENAME"
echo "Installed Worklog plugin $SRC to $TARGET_DIR/$BASENAME"

# If the script was called without explicit target, and the target is the default
# `.worklog/plugins`, also place the installer itself into `.worklog/plugins` for
# convenience so users can run the installer from the project root.
if [ "${2:-}" = "" ] && [ "$TARGET_DIR" = ".worklog/plugins" ]; then
  cp -f "$0" "$TARGET_DIR/$(basename "$0")"
  echo "Copied installer to $TARGET_DIR/$(basename "$0")"
fi
