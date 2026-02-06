#!/usr/bin/env sh
# Install the ampa example plugin into the project-scoped plugin dir used by wl
# Usage: ./scripts/install-ampa.sh [target-dir]
# By default installs to .worklog/plugins/ampa.mjs

set -e

TARGET_DIR=${1:-.worklog/plugins}
mkdir -p "$TARGET_DIR"
cp -f examples/ampa.mjs "$TARGET_DIR/ampa.mjs"
echo "Installed ampa plugin to $TARGET_DIR/ampa.mjs"
