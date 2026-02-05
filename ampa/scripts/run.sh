#!/usr/bin/env bash
set -euo pipefail

# Prepare XDG_RUNTIME_DIR for rootless podman (if not set)
# Avoid using the special readonly UID variable; use RUN_UID instead
RUN_UID=$(id -u)
# If XDG_RUNTIME_DIR is unset, prefer a safe per-user tmp dir. If it is set but
# cannot be created (e.g. points under /run/user on a headless system), fall
# back to /tmp/run-user-<uid> so rootless podman helper checks still work.
: "${XDG_RUNTIME_DIR:=/tmp/run-user-$RUN_UID}"
if ! mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null; then
  echo "Warning: cannot create XDG_RUNTIME_DIR='$XDG_RUNTIME_DIR' — falling back to /tmp/run-user-$RUN_UID" >&2
  XDG_RUNTIME_DIR="/tmp/run-user-$RUN_UID"
  mkdir -p "$XDG_RUNTIME_DIR"
fi
chmod 700 "$XDG_RUNTIME_DIR" || true

ENVFILE=""
[ -f .env ] && ENVFILE="--env-file .env"
CONTAINER_NAME=ampa-daemon-local

# Check whether port 8080 is already in use. If a container is publishing the
# port we will attempt to stop/remove it automatically (safe default). If some
# other process is listening, print details and exit so the user can decide.
if command -v ss >/dev/null 2>&1; then
  if ss -ltnp | grep -q ':8080'; then
    # Try to detect and remove a Docker container first
    if command -v docker >/dev/null 2>&1 && docker ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | grep -q ':8080'; then
      echo "Detected Docker container(s) publishing :8080 — stopping and removing them..."
      docker ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $1}' | xargs -r docker rm -f || true
      sleep 1
      if ss -ltnp | grep -q ':8080'; then
        echo "Port 8080 still in use after removing Docker container(s)." >&2
        ss -ltnp | sed -n '1,200p' >&2 || true
        exit 1
      fi
    elif command -v podman >/dev/null 2>&1 && podman ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | grep -q ':8080'; then
      echo "Detected Podman container(s) publishing :8080 — stopping and removing them..."
      podman ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $1}' | xargs -r podman rm -f || true
      sleep 1
      if ss -ltnp | grep -q ':8080'; then
        echo "Port 8080 still in use after removing Podman container(s)." >&2
        ss -ltnp | sed -n '1,200p' >&2 || true
        exit 1
      fi
    else
      echo "Port 8080 already in use by a non-container process; run 'ss -ltnp | grep :8080' to inspect or stop the process." >&2
      ss -ltnp | sed -n '1,200p' >&2 || true
      exit 1
    fi
  fi
elif command -v lsof >/dev/null 2>&1; then
  if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Port 8080 already in use; run 'make stop' to stop it." >&2
    lsof -iTCP:8080 -sTCP:LISTEN -P -n || true
    exit 1
  fi
else
  # Last resort: check docker/podman published ports
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | grep -q ':8080'; then
    echo "Detected Docker container(s) publishing :8080 — stopping and removing them..."
    docker ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $1}' | xargs -r docker rm -f || true
  elif command -v podman >/dev/null 2>&1 && podman ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | grep -q ':8080'; then
    echo "Detected Podman container(s) publishing :8080 — stopping and removing them..."
    podman ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $1}' | xargs -r podman rm -f || true
  fi
fi

# Run container. If an env file was supplied we should NOT pass an empty
# `-e AMPA_DISCORD_WEBHOOK=` afterwards because that would override values
# loaded from the file. Only pass the explicit -e flag when no envfile is
# present and the variable is set in the caller environment.
EXTRA_ENV=""
if [ -n "${ENVFILE:-}" ]; then
  # Avoid loading any baked-in .env inside the image when an env-file is used.
  EXTRA_ENV="-e AMPA_LOAD_DOTENV=0"
else
  if [ -n "${AMPA_DISCORD_WEBHOOK:-}" ]; then
    EXTRA_ENV="-e AMPA_DISCORD_WEBHOOK=${AMPA_DISCORD_WEBHOOK}"
  fi
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker run -d --name "$CONTAINER_NAME" $ENVFILE --rm $EXTRA_ENV -p 8080:8080 ampa-daemon:local
elif command -v podman >/dev/null 2>&1; then
  podman run -d --name "$CONTAINER_NAME" $ENVFILE --rm $EXTRA_ENV -p 8080:8080 ampa-daemon:local
else
  echo "Either a working Docker daemon or Podman is required to run the container" >&2
  exit 1
fi

echo "Started container with name $CONTAINER_NAME"
