#!/usr/bin/env bash
set -euo pipefail

# Prepare XDG_RUNTIME_DIR for rootless podman (if not set)
UID=$(id -u)
: "${XDG_RUNTIME_DIR:=/tmp/run-user-$UID}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

ENVFILE=""
[ -f .env ] && ENVFILE="--env-file .env"
CONTAINER_NAME=ampa-daemon-local

# Check whether port 8080 is already in use
if command -v ss >/dev/null 2>&1; then
  if ss -ltn | grep -q ':8080'; then
    echo "Port 8080 already in use; run 'make stop' to stop it." >&2
    exit 1
  fi
elif command -v lsof >/dev/null 2>&1; then
  if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Port 8080 already in use; run 'make stop' to stop it." >&2
    exit 1
  fi
else
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.ID}} {{.Ports}}' | grep -q ':8080'; then
    echo "Port 8080 already in use; run 'make stop' to stop it." >&2
    exit 1
  fi
  if command -v podman >/dev/null 2>&1 && podman ps --format '{{.Ports}}' | grep -q ':8080'; then
    echo "Port 8080 already in use; run 'make stop' to stop it." >&2
    exit 1
  fi
fi

# Run container
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker run -d --name "$CONTAINER_NAME" $ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="${AMPA_DISCORD_WEBHOOK:-}" -p 8080:8080 ampa-daemon:local
elif command -v podman >/dev/null 2>&1; then
  podman run -d --name "$CONTAINER_NAME" $ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="${AMPA_DISCORD_WEBHOOK:-}" -p 8080:8080 ampa-daemon:local
else
  echo "Either a working Docker daemon or Podman is required to run the container" >&2
  exit 1
fi

echo "Started container with name $CONTAINER_NAME"
