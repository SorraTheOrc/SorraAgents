#!/usr/bin/env sh
# Install a Worklog plugin into a project or user plugin directory.
# Usage: ./plugins/install-worklog-plugin.sh <source-file> [target-dir]
# Example: ./plugins/install-worklog-plugin.sh plugins/wl_ampa/ampa.mjs

set -eu

# Prevent concurrent installs: use an atomic mkdir-based lock in /tmp.
LOCK_DIR="/tmp/ampa_install.lock"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
else
  echo "Another ampa install appears to be running (lock $LOCK_DIR). Try again later." >&2
  exit 1
fi

# Use a unique per-run decision log to avoid interleaved entries from
# concurrent or previous runs. Create a stable symlink for convenience.
DECISION_LOG="/tmp/ampa_install_decisions.$$"
ln -sf "$DECISION_LOG" /tmp/ampa_install_decisions.log || true
_ts=$(date +"%Y-%m-%dT%H:%M:%S%z")

DEFAULT_SRC="plugins/wl_ampa/ampa.mjs"

# Basic arg parsing: allow an optional --webhook|-w <url> and two positional
# args: [source] [target-dir]. We keep parsing simple and robust under /bin/sh.
WEBHOOK=""
SRC_ARG=""
TARGET_ARG=""
AUTO_YES=0
FORCE_RESTART=0
FORCE_NO_RESTART=0
DO_RESTART=0
WEBHOOK_ASKED=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --webhook|-w)
      shift
      if [ "$#" -gt 0 ]; then
        WEBHOOK="$1"
        shift
      else
        echo "--webhook requires a value" >&2
        exit 2
      fi
      ;;
    --yes|-y)
      AUTO_YES=1; shift;;
    --restart)
      FORCE_RESTART=1; shift;;
    --no-restart)
      FORCE_NO_RESTART=1; shift;;
    --help|-h)
      echo "Usage: $0 [--webhook <url>] [--yes] [--restart|--no-restart] [source-file] [target-dir]" && exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [ -z "$SRC_ARG" ]; then
        SRC_ARG="$1"
      elif [ -z "$TARGET_ARG" ]; then
        TARGET_ARG="$1"
      else
        echo "Ignoring extra argument: $1" >&2
      fi
      shift
      ;;
  esac
done

# (webhook prompt moved later so upgrade/ restart questions come first)

# Set source path
if [ -n "$SRC_ARG" ]; then
  SRC="$SRC_ARG"
else
  SRC="$DEFAULT_SRC"
fi
TARGET_DIR=${TARGET_ARG:-.worklog/plugins}

# Determine basename for installation
BASENAME=$(basename "$SRC")

EXISTING_INST=0
DO_RESTART=0
ACTION_PROCEED=1
EXISTING_WEBHOOK=""
SKIP_WEBHOOK_UPDATE=0
REMOVE_WEBHOOK=0
PRESERVE_EXISTING_ENV=0

# If the project already has a bundled .env under the plugin dir, prefer to
# preserve it and never overwrite it during install.
if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
  PRESERVE_EXISTING_ENV=1
  printf "%s PRESERVE_EXISTING_ENV_DETECTED=1 PATH=%s\n" "$_ts" "$TARGET_DIR/ampa_py/ampa/.env" >> "$DECISION_LOG" || true
fi

if [ -f "$TARGET_DIR/$BASENAME" ] || [ -d "$TARGET_DIR/ampa_py/ampa" ]; then
  EXISTING_INST=1
  if [ "$AUTO_YES" -eq 1 ]; then
    # Non-interactive: accept default (upgrade/reinstall)
    ACTION_PROCEED=1
  else
    if [ -t 0 ]; then
      printf "Existing ampa installation detected at %s\n" "$TARGET_DIR"
      printf "Choose action: [U]pgrade/Reinstall (default), [A]bort: "
      if ! read -r CHOICE; then CHOICE=""; fi
      case "$(printf "%s" "$CHOICE" | tr '[:upper:]' '[:lower:]')" in
        a)
          echo "Aborting."; exit 1;
          ;;
        *)
          echo "Proceeding with upgrade/reinstall...";
          ACTION_PROCEED=1;
          ;;
      esac
    else
      ACTION_PROCEED=1
    fi
# Close outer EXISTING_INST check
# Close the EXISTING_INST block
  fi
fi

# (Webhook prompt moved to later where sample/.env handling occurs so it's
# presented only when relevant and avoids duplicate prompts.)

if [ "$ACTION_PROCEED" -ne 1 ]; then
  exit 0
fi

# After upgrade/skip/abort decision, prompt for webhook according to flow:
# - If no existing install: ask for webhook (unless provided via CLI or --yes)
# - If existing install and proceeding: ask whether to change webhook (enter
#   new one) or keep existing (do nothing). CLI --webhook wins.
if [ -z "$WEBHOOK" ]; then
  # detect existing webhook in current install or repo
  EXISTING_WEBHOOK=""
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    EXISTING_WEBHOOK="$(awk -F= '/AMPA_DISCORD_WEBHOOK/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$TARGET_DIR/ampa_py/ampa/.env" | tr -d '"' | tr -d "'" )"
  elif [ -f "ampa/.env" ]; then
    EXISTING_WEBHOOK="$(awk -F= '/AMPA_DISCORD_WEBHOOK/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "ampa/.env" | tr -d '"' | tr -d "'" )"
  fi

  if [ "$EXISTING_INST" -eq 1 ]; then
    # Existing installation: after upgrade decision, ask about changing webhook
    if [ -t 0 ]; then
      if [ -n "$EXISTING_WEBHOOK" ]; then
        printf "Existing webhook detected: %s\n" "$EXISTING_WEBHOOK"
        printf "Change webhook? [y/N]: "
        if ! read -r CHW; then CHW=""; fi
        case "$(printf "%s" "$CHW" | tr '[:upper:]' '[:lower:]')" in
          y|yes)
            printf "Enter new webhook (leave empty to reuse existing, or '-' to remove): "
            if ! read -r NEW_WH; then NEW_WH=""; fi
            if [ "$NEW_WH" = "-" ]; then
              WEBHOOK=""
              REMOVE_WEBHOOK=1
            elif [ -n "$NEW_WH" ]; then
              WEBHOOK="$NEW_WH"
            else
              SKIP_WEBHOOK_UPDATE=1
            fi
            ;;
          *)
            SKIP_WEBHOOK_UPDATE=1
            ;;
        esac
      else
        # Existing install but no webhook recorded; allow empty (skip)
        if [ "$AUTO_YES" -eq 1 ]; then
          WEBHOOK=""
        else
          printf "Enter Discord webhook URL to use for installation (leave empty to skip): "
          if ! read -r NEW_WH; then NEW_WH=""; fi
          WEBHOOK="$NEW_WH"
        fi
      fi
    fi
  else
    # Fresh install: require webhook (unless AUTO_YES)
    if [ -t 0 ]; then
      if [ "$AUTO_YES" -eq 1 ]; then
        WEBHOOK=""
      else
        while true; do
          printf "Enter Discord webhook URL to use for installation: "
          if ! read -r NEW_WH; then NEW_WH=""; fi
          if [ -n "$NEW_WH" ]; then
            WEBHOOK="$NEW_WH"
            break
          else
            printf "Webhook is required for a new installation.\n"
          fi
        done
      fi
    fi
  fi
fi

# If there is a running daemon pid, ask whether to restart automatically.
PID_FILE=".worklog/ampa/default/default.pid"
if [ -f "$PID_FILE" ]; then
  PID_VAL="$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$PID_VAL" ]; then
    if kill -0 "$PID_VAL" 2>/dev/null; then
      if [ "$FORCE_RESTART" -eq 1 ]; then
        DO_RESTART=1
      elif [ "$FORCE_NO_RESTART" -eq 1 ]; then
        DO_RESTART=0
      elif [ -t 0 ]; then
        printf "Detected running daemon pid=%s. Restart automatically when installation completes? [y/N]: " "$PID_VAL"
        if ! read -r RESTART_ANS; then RESTART_ANS=""; fi
        case "$(printf "%s" "$RESTART_ANS" | tr '[:upper:]' '[:lower:]')" in
          y|yes)
            DO_RESTART=1;
            ;;
          *)
            DO_RESTART=0;
            ;;
        esac
      else
        DO_RESTART=0
      fi
    fi
  fi
fi

BASENAME=$(basename "$SRC")

echo "Installing to $TARGET_DIR"

# Ensure DECISION_LOG is set (may have been created earlier for locking)
if [ -z "${DECISION_LOG-}" ]; then
  DECISION_LOG="/tmp/ampa_install_decisions.log"
  ln -sf "$DECISION_LOG" /tmp/ampa_install_decisions.log || true
fi
_ts=$(date +"%Y-%m-%dT%H:%M:%S%z")
if [ -n "$WEBHOOK" ]; then
  _wh_mask="$(printf "%.8s" "$WEBHOOK")..."
else
  _wh_mask="(empty)"
fi
printf "%s ACTION_PROCEED=%s EXISTING_INST=%s DO_RESTART=%s WEBHOOK=%s SRC=%s TARGET=%s\n" "$_ts" "$ACTION_PROCEED" "$EXISTING_INST" "$DO_RESTART" "$_wh_mask" "$SRC" "$TARGET_DIR" >> "$DECISION_LOG" || true

# Webhook prompting is performed later after copying the Python package so
# we can accurately detect and update the package's .env in-place.

##

if [ ! -f "$SRC" ]; then
  echo "Source file not found: $SRC" >&2
  echo "If you intended to install the canonical plugin, run without arguments to use $DEFAULT_SRC" >&2
  exit 2
fi

mkdir -p "$TARGET_DIR"
cp -f "$SRC" "$TARGET_DIR/$BASENAME"
  echo "Installed Worklog plugin $SRC to $TARGET_DIR/$BASENAME"

  # If requested, attempt to stop the running daemon now to allow upgrade
  if [ "$DO_RESTART" -eq 1 ]; then
    echo "Stopping running daemon before upgrade..."
    node "$TARGET_DIR/$BASENAME" stop --name default || true
  fi

# If the repository contains a Python `ampa` package at the repo root, also
# copy it into the project's plugin dir as `.worklog/plugins/ampa_py/ampa` so
# the JS plugin can automatically run `python -m ampa.daemon`.
if [ -d "ampa" ]; then
  PY_TARGET_DIR="$TARGET_DIR/ampa_py"
  mkdir -p "$PY_TARGET_DIR"
  # Replace any existing bundle but preserve an existing .env if present
  ENV_BACKUP=""
  TMP_BACKUP="$PY_TARGET_DIR/.env.preinstall.$$"
  # If an .env exists, create a durable backup before we remove the bundle.
  if [ -f "$PY_TARGET_DIR/ampa/.env" ]; then
    ENV_BACKUP="$TMP_BACKUP"
    # use cp -a/p to preserve permissions where possible
    cp -a "$PY_TARGET_DIR/ampa/.env" "$ENV_BACKUP" 2>/dev/null || cp "$PY_TARGET_DIR/ampa/.env" "$ENV_BACKUP" || true
    printf "%s BACKUP_ENV=%s\n" "$_ts" "$ENV_BACKUP" >> "$DECISION_LOG" || true
  fi
  # record directory contents before removal for diagnostics
  printf "%s PRE_REMOVE_ls=%s\n" "$_ts" "$(ls -la "$PY_TARGET_DIR" 2>/dev/null || true)" >> "$DECISION_LOG" || true
  rm -rf "$PY_TARGET_DIR/ampa"
  cp -R "ampa" "$PY_TARGET_DIR/ampa"
  # record directory contents after copy for diagnostics
  printf "%s POST_COPY_ls=%s\n" "$_ts" "$(ls -la "$PY_TARGET_DIR/ampa" 2>/dev/null || true)" >> "$DECISION_LOG" || true

  # If we backed up a pre-existing .env, restore it unconditionally. If the
  # restore fails, log it so we can debug.
  if [ -n "$ENV_BACKUP" ] && [ -f "$ENV_BACKUP" ]; then
    if mv "$ENV_BACKUP" "$PY_TARGET_DIR/ampa/.env" 2>/dev/null; then
      printf "%s RESTORED_ENV=%s\n" "$_ts" "$ENV_BACKUP" >> "$DECISION_LOG" || true
    elif cp -p "$ENV_BACKUP" "$PY_TARGET_DIR/ampa/.env" 2>/dev/null; then
      printf "%s RESTORED_ENV_COPY=%s\n" "$_ts" "$ENV_BACKUP" >> "$DECISION_LOG" || true
    else
      printf "%s RESTORE_FAILED=%s\n" "$_ts" "$ENV_BACKUP" >> "$DECISION_LOG" || true
    fi
  else
    # No pre-existing env; if the user asked to skip webhook updates, remove
    # any .env copied from the repo so we don't create/overwrite a silent file.
    if [ "$SKIP_WEBHOOK_UPDATE" -eq 1 ]; then
      if [ -f "$PY_TARGET_DIR/ampa/.env" ]; then
        rm -f "$PY_TARGET_DIR/ampa/.env" || true
        printf "%s REMOVED_COPIED_ENV=1\n" "$_ts" >> "$DECISION_LOG" || true
      fi
    fi
  fi
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
      echo "Error: no python executable found in PATH; cannot create venv." >&2
      exit 1
    fi
    VENV_DIR="$PY_TARGET_DIR/venv"
    if [ ! -d "$VENV_DIR" ]; then
      echo "Creating virtualenv at $VENV_DIR"
      if ! "$PY_BIN" -m venv "$VENV_DIR" 2>&1 | tee /tmp/ampa_install_venv.log; then
        echo "Error: failed to create venv with $PY_BIN -m venv. See /tmp/ampa_install_venv.log" >&2
        exit 1
      fi
    fi
    if [ ! -x "$VENV_DIR/bin/python" ]; then
      echo "Error: virtualenv python not found at $VENV_DIR/bin/python" >&2
      exit 1
    fi
    echo "Upgrading pip and installing requirements into venv (logs: /tmp/ampa_install_pip.log)"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel 2>&1 | tee /tmp/ampa_install_pip.log || true
    if "$VENV_DIR/bin/python" -m pip install -r "$REQ_FILE" 2>&1 | tee -a /tmp/ampa_install_pip.log; then
      echo "Installed Python dependencies into $VENV_DIR"
    else
      echo "Error: pip install failed. See /tmp/ampa_install_pip.log for details." >&2
      echo "You can try to re-run:" >&2
      echo "  $VENV_DIR/bin/python -m pip install -r $REQ_FILE" >&2
      exit 1
    fi
  fi
fi

# Note: the installer no longer copies itself into the target plugin dir.
# Keeping installer in the repo (plugins/install-worklog-plugin.sh) is preferred
# to avoid writing executable files into .worklog/ which is usually gitignored.

# If a Discord webhook was not supplied on the command line, prompt interactively
# (only when running in a TTY). The webhook will be injected into the copied
# Python package's .env file derived from a .env.samplw template.
if [ -d "ampa" ] || [ -d "$PY_TARGET_DIR/ampa" ]; then
  # The copied package location
  SAMPLE_SRC=""
  # Support common sample filenames: .env.sample (most projects) and the
  # previously used .env.samplw (legacy/typo). Prefer .env.sample when present.
  if [ -f "$PY_TARGET_DIR/ampa/.env.sample" ]; then
    SAMPLE_SRC="$PY_TARGET_DIR/ampa/.env.sample"
    SAMPLE_DST="$PY_TARGET_DIR/ampa/.env"
  elif [ -f "$PY_TARGET_DIR/ampa/.env.samplw" ]; then
    SAMPLE_SRC="$PY_TARGET_DIR/ampa/.env.samplw"
    SAMPLE_DST="$PY_TARGET_DIR/ampa/.env"
  elif [ -f "ampa/.env.sample" ]; then
    # Fallback to original repo sample if not present in copied bundle
    SAMPLE_SRC="ampa/.env.sample"
    SAMPLE_DST="$PY_TARGET_DIR/ampa/.env"
  elif [ -f "ampa/.env.samplw" ]; then
    SAMPLE_SRC="ampa/.env.samplw"
    SAMPLE_DST="$PY_TARGET_DIR/ampa/.env"
  fi

  # Unified webhook application logic: update existing .env, or copy a sample
  # into place and write the webhook. This runs after the Python package has
  # been copied into $PY_TARGET_DIR/ampa.
  # If the user chose to skip webhook updates during upgrade, or if the
  # project already has an .env in place, preserve it and do not modify it.
  if [ "$SKIP_WEBHOOK_UPDATE" -eq 1 ] || [ "$PRESERVE_EXISTING_ENV" -eq 1 ]; then
    echo "Preserving existing .env (user requested no webhook update or pre-existing .env)"
    printf "%s PRESERVE_EXISTING_ENV=%s\n" "$_ts" "$PRESERVE_EXISTING_ENV" >> "$DECISION_LOG" || true
  else
  # Targets:
  # - If REMOVE_WEBHOOK=1: remove AMPA_DISCORD_WEBHOOK from existing .env
  # - Else if WEBHOOK is non-empty: update existing .env (if present) or try
  #   to copy a sample into place and set the var.
  if [ "$REMOVE_WEBHOOK" -eq 1 ]; then
    ENV_FILE="$PY_TARGET_DIR/ampa/.env"
    if [ -f "$ENV_FILE" ]; then
      if command -v awk >/dev/null 2>&1; then
        awk '!/^AMPA_DISCORD_WEBHOOK=/' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
      else
        grep -v '^AMPA_DISCORD_WEBHOOK=' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE" || true
      fi
      echo "Removed AMPA_DISCORD_WEBHOOK from $ENV_FILE"
    else
      echo "No .env present to remove webhook from; skipping."
    fi
  elif [ -n "$WEBHOOK" ]; then
    ENV_FILE="$PY_TARGET_DIR/ampa/.env"
    if [ -f "$ENV_FILE" ]; then
      if command -v awk >/dev/null 2>&1; then
        awk -v w="$WEBHOOK" 'BEGIN{r=0} /^AMPA_DISCORD_WEBHOOK=/ {print "AMPA_DISCORD_WEBHOOK=" w; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_WEBHOOK=" w}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
        echo "Updated webhook in $ENV_FILE"
      else
        echo "AMPA_DISCORD_WEBHOOK=$WEBHOOK" >> "$ENV_FILE"
        echo "Appended webhook to $ENV_FILE"
      fi
    else
      # try to find a sample to copy from common locations
      if [ -f "$PY_TARGET_DIR/ampa/.env.sample" ]; then
        cp -f "$PY_TARGET_DIR/ampa/.env.sample" "$ENV_FILE"
        SRC_SAMPLE="$PY_TARGET_DIR/ampa/.env.sample"
      elif [ -f "ampa/.env.sample" ]; then
        cp -f "ampa/.env.sample" "$ENV_FILE"
        SRC_SAMPLE="ampa/.env.sample"
      else
        SRC_SAMPLE=""
      fi
      if [ -n "$SRC_SAMPLE" ]; then
        if command -v awk >/dev/null 2>&1; then
          awk -v w="$WEBHOOK" 'BEGIN{r=0} /^AMPA_DISCORD_WEBHOOK=/ {print "AMPA_DISCORD_WEBHOOK=" w; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_WEBHOOK=" w}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
          echo "Copied sample and wrote webhook to $ENV_FILE"
        else
          echo "AMPA_DISCORD_WEBHOOK=$WEBHOOK" >> "$ENV_FILE"
          echo "Copied sample and appended webhook to $ENV_FILE"
        fi
      else
        echo "No .env or .env.sample available to create .env; skipping webhook write"
      fi
    fi
  else
    echo "No webhook provided; skipping .env creation/update"
  fi
  fi
fi

# If requested, attempt to restart the daemon after installation
# Reconcile: if user asked to skip webhook updates but the restore failed
# for any reason, attempt a final restore of the backed-up .env so we
# preserve the user's file.
if [ "$SKIP_WEBHOOK_UPDATE" -eq 1 ] && [ -n "$PY_TARGET_DIR" ]; then
  # ENV_BACKUP may be set during the copy/backup logic above; if the file
  # exists in the parent location and the target .env is missing, restore it.
  if [ -n "$ENV_BACKUP" ] && [ -f "$ENV_BACKUP" ] && [ ! -f "$PY_TARGET_DIR/ampa/.env" ]; then
    mv "$ENV_BACKUP" "$PY_TARGET_DIR/ampa/.env" 2>/dev/null || cp -p "$ENV_BACKUP" "$PY_TARGET_DIR/ampa/.env" 2>/dev/null || true
    printf "%s RECONCILED_RESTORED_ENV=%s\n" "$_ts" "$ENV_BACKUP" >> "$DECISION_LOG" || true
  fi
fi

if [ "$DO_RESTART" -eq 1 ]; then
  echo "Attempting to restart daemon..."
  printf "%s Attempting restart: TARGET=%s BINARY=%s\n" "$_ts" "$TARGET_DIR" "$BASENAME" >> "$DECISION_LOG" || true
  # Use env to locate node
  if ! /usr/bin/env node "$TARGET_DIR/$BASENAME" start --name default > /tmp/ampa_install_start.log 2>&1; then
    echo "Warning: failed to start daemon; see /tmp/ampa_install_start.log" >&2
    printf "%s RESTART=failed\n" "$_ts" >> "$DECISION_LOG" || true
  else
    # If start returned successfully, try to read pid file for confirmation
    if [ -f "$PID_FILE" ]; then
      NEWPID="$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)"
      if [ -n "$NEWPID" ] && kill -0 "$NEWPID" 2>/dev/null; then
        echo "Started pid=$NEWPID"
        printf "%s RESTART=ok PID=%s\n" "$_ts" "$NEWPID" >> "$DECISION_LOG" || true
      else
        echo "Start command executed; no running pid detected. See /tmp/ampa_install_start.log"
        printf "%s RESTART=unknown\n" "$_ts" >> "$DECISION_LOG" || true
      fi
    else
      echo "Start command executed; no pid file created. See /tmp/ampa_install_start.log"
      printf "%s RESTART=no-pid-file\n" "$_ts" >> "$DECISION_LOG" || true
    fi
  fi
fi
