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

# If the repository contains a Python `ampa` package at the repo root, also
# copy it into the project's plugin dir as `.worklog/plugins/ampa_py/ampa` so
# the JS plugin can automatically run `python -m ampa.daemon`.
if [ -d "ampa" ]; then
  PY_TARGET_DIR="$TARGET_DIR/ampa_py"
  mkdir -p "$PY_TARGET_DIR"
  # Replace any existing bundle
  rm -rf "$PY_TARGET_DIR/ampa"
  cp -R "ampa" "$PY_TARGET_DIR/ampa"
  echo "Installed Python ampa package to $PY_TARGET_DIR/ampa"

  # If the copied package declares Python requirements, create a venv and
  # install them under .worklog/plugins/ampa_py/venv so the bundled daemon
  # has its dependencies available when executed via python -m ampa.daemon.
  REQ_FILE="$PY_TARGET_DIR/ampa/requirements.txt"
  if [ -f "$REQ_FILE" ]; then
    echo "Found requirements.txt; attempting to create venv and install dependencies"
    # Prefer python3, fall back to python
    PY_BIN="$(command -v python3 || command -v python || true)"
    if [ -z "$PY_BIN" ]; then
      echo "Warning: no python executable found in PATH; cannot create venv. Skipping dependency install."
    else
      VENV_DIR="$PY_TARGET_DIR/venv"
      if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtualenv at $VENV_DIR"
        "$PY_BIN" -m venv "$VENV_DIR" || {
          echo "Warning: failed to create venv with $PY_BIN -m venv. Skipping dependency install."
          VENV_DIR=
        }
      fi
      if [ -n "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]; then
        echo "Upgrading pip and installing requirements into venv"
        "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
        if "$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE" >/dev/null 2>&1; then
          echo "Installed Python dependencies into $VENV_DIR"
        else
          echo "Warning: pip install failed. You may need to run:"
          echo "  $VENV_DIR/bin/python -m pip install -r $REQ_FILE"
        fi
      fi
    fi
  fi
fi

# Note: the installer no longer copies itself into the target plugin dir.
# Keeping installer in the repo (plugins/install-worklog-plugin.sh) is preferred
# to avoid writing executable files into .worklog/ which is usually gitignored.
