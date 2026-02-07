#!/usr/bin/env sh
# Install a Worklog plugin into a project or user plugin directory.
# Usage: ./plugins/install-worklog-plugin.sh <source-file> [target-dir]
# Example: ./plugins/install-worklog-plugin.sh plugins/wl_ampa/ampa.mjs

set -eu

# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_SRC="plugins/wl_ampa/ampa.mjs"
LOCK_DIR="/tmp/ampa_install.lock"
DECISION_LOG="/tmp/ampa_install_decisions.$$"
PID_FILE=".worklog/ampa/default/default.pid"

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

# Print message to stderr
log_error() {
  echo "$@" >&2
}

# Print message to stdout
log_info() {
  echo "$@"
}

# Get current timestamp in ISO format with timezone
get_timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

# Create a symlink to the decision log for convenience
create_decision_log_symlink() {
  ln -sf "$DECISION_LOG" /tmp/ampa_install_decisions.log || true
}

# Log a decision to the decision log file
log_decision() {
  local _ts=$(get_timestamp)
  printf "%s %s\n" "$_ts" "$1" >> "$DECISION_LOG" || true
}

# ============================================================================
# LOCKING / CONCURRENCY CONTROL
# ============================================================================

# Acquire an exclusive lock for installation
acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log_error "Another ampa install appears to be running (lock $LOCK_DIR). Try again later."
    exit 1
  fi
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
}

# ============================================================================
# ARGUMENT PARSING
# ============================================================================

# Parse command-line arguments and populate global variables.
# Global variables set: WEBHOOK, SRC, TARGET_DIR, AUTO_YES, FORCE_RESTART, FORCE_NO_RESTART
parse_args() {
  # Initialize output variables with defaults
  WEBHOOK=""
  SRC_ARG=""
  TARGET_ARG=""
  AUTO_YES=0
  FORCE_RESTART=0
  FORCE_NO_RESTART=0

  # Parse options
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --webhook|-w)
        shift
        if [ "$#" -gt 0 ]; then
          WEBHOOK="$1"
          shift
        else
          log_error "--webhook requires a value"
          exit 2
        fi
        ;;
      --yes|-y)
        AUTO_YES=1
        shift
        ;;
      --restart)
        FORCE_RESTART=1
        shift
        ;;
      --no-restart)
        FORCE_NO_RESTART=1
        shift
        ;;
      --help|-h)
        echo "Usage: $0 [--webhook <url>] [--yes] [--restart|--no-restart] [source-file] [target-dir]"
        exit 0
        ;;
      --*)
        log_error "Unknown option: $1"
        exit 2
        ;;
      *)
        # Positional argument
        if [ -z "$SRC_ARG" ]; then
          SRC_ARG="$1"
        elif [ -z "$TARGET_ARG" ]; then
          TARGET_ARG="$1"
        else
          log_error "Ignoring extra argument: $1"
        fi
        shift
        ;;
    esac
  done

  # Validate argument combinations
  if [ "$FORCE_RESTART" -eq 1 ] && [ "$FORCE_NO_RESTART" -eq 1 ]; then
    log_error "--restart and --no-restart are mutually exclusive"
    exit 2
  fi

  # Set final values with defaults
  SRC="${SRC_ARG:-$DEFAULT_SRC}"
  TARGET_DIR="${TARGET_ARG:-.worklog/plugins}"
}

# ============================================================================
# UPGRADE/INSTALL DETECTION
# ============================================================================

# Detect if an existing installation is present
detect_existing_installation() {
  local basename="$(basename "$SRC")"
  
  if [ -f "$TARGET_DIR/$basename" ] || [ -d "$TARGET_DIR/ampa_py/ampa" ]; then
    return 0  # Existing installation found
  fi
  return 1  # No existing installation
}

# Prompt user for upgrade vs abort decision
prompt_upgrade_or_abort() {
  if [ "$AUTO_YES" -eq 1 ]; then
    # In non-interactive mode, default to upgrade
    return 0
  fi

  if [ -t 0 ]; then
    printf "Existing ampa installation detected at %s\n" "$TARGET_DIR"
    printf "Choose action: [U]pgrade/Reinstall (default), [A]bort: "
    if ! read -r CHOICE; then CHOICE=""; fi
    case "$(printf "%s" "$CHOICE" | tr '[:upper:]' '[:lower:]')" in
      a)
        echo "Aborting."
        exit 1
        ;;
      *)
        return 0  # Proceed with upgrade
        ;;
    esac
  fi

  return 0  # Default to upgrade
}

# ============================================================================
# ENV FILE HANDLING
# ============================================================================

# Check if a pre-existing .env is bundled in the plugin directory
check_for_bundled_env() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    return 0  # Bundled .env found
  fi
  return 1  # No bundled .env
}

# Detect existing webhook in current install or repo
detect_existing_webhook() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_WEBHOOK/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$TARGET_DIR/ampa_py/ampa/.env" | tr -d '"' | tr -d "'"
  elif [ -f "ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_WEBHOOK/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "ampa/.env" | tr -d '"' | tr -d "'"
  fi
}

# Prompt user whether to change webhook during upgrade
prompt_webhook_change() {
  local existing_webhook="$1"
  
  if [ -z "$existing_webhook" ]; then
    # No existing webhook, allow entering new one
    if [ "$AUTO_YES" -eq 1 ]; then
      WEBHOOK=""
    else
      if [ -t 0 ]; then
        printf "Enter Discord webhook URL to use for installation (leave empty to skip): "
        if ! read -r NEW_WH; then NEW_WH=""; fi
        WEBHOOK="$NEW_WH"
      fi
    fi
  else
    # Existing webhook found
    if [ -t 0 ]; then
      printf "Existing webhook detected: %s\n" "$existing_webhook"
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
    fi
  fi
}

# Prompt user for webhook during fresh install
prompt_webhook_new() {
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
}

# Find env sample file (.env.sample or .env.samplw)
find_env_sample() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env.sample" ]; then
    echo "$TARGET_DIR/ampa_py/ampa/.env.sample"
  elif [ -f "$TARGET_DIR/ampa_py/ampa/.env.samplw" ]; then
    echo "$TARGET_DIR/ampa_py/ampa/.env.samplw"
  elif [ -f "ampa/.env.sample" ]; then
    echo "ampa/.env.sample"
  elif [ -f "ampa/.env.samplw" ]; then
    echo "ampa/.env.samplw"
  fi
}

# Back up existing .env file before removal
backup_env_file() {
  local target_env="$1"
  local backup_dir="${2:-.}"  # Use specified directory, or current directory by default
  local backup_filename=$(basename "$target_env")
  local backup_path="$backup_dir/$backup_filename.preinstall.$$"
  
  if [ -f "$target_env" ]; then
    mkdir -p "$backup_dir" 2>/dev/null || true
    if cp -a "$target_env" "$backup_path" 2>/dev/null || cp "$target_env" "$backup_path" 2>/dev/null; then
      echo "$backup_path"
      log_decision "BACKUP_ENV=$backup_path"
      return 0
    fi
  fi
  return 1
}

# Write webhook to .env file
write_webhook_to_env() {
  local env_file="$1"
  local webhook="$2"
  
  if [ -f "$env_file" ]; then
    if command -v awk >/dev/null 2>&1; then
      awk -v w="$webhook" 'BEGIN{r=0} /^AMPA_DISCORD_WEBHOOK=/ {print "AMPA_DISCORD_WEBHOOK=" w; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_WEBHOOK=" w}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
      log_info "Updated webhook in $env_file"
    else
      echo "AMPA_DISCORD_WEBHOOK=$webhook" >> "$env_file"
      log_info "Appended webhook to $env_file"
    fi
  else
    # Try to create from sample
    local sample=$(find_env_sample)
    if [ -n "$sample" ] && [ -f "$sample" ]; then
      cp -f "$sample" "$env_file"
      if command -v awk >/dev/null 2>&1; then
        awk -v w="$webhook" 'BEGIN{r=0} /^AMPA_DISCORD_WEBHOOK=/ {print "AMPA_DISCORD_WEBHOOK=" w; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_WEBHOOK=" w}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
        log_info "Copied sample and wrote webhook to $env_file"
      else
        echo "AMPA_DISCORD_WEBHOOK=$webhook" >> "$env_file"
        log_info "Copied sample and appended webhook to $env_file"
      fi
    else
      log_info "No .env or .env.sample available; skipping webhook write"
    fi
  fi
}

# Remove webhook from .env file
remove_webhook_from_env() {
  local env_file="$1"
  
  if [ -f "$env_file" ]; then
    if command -v awk >/dev/null 2>&1; then
      awk '!/^AMPA_DISCORD_WEBHOOK=/' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
    else
      grep -v '^AMPA_DISCORD_WEBHOOK=' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file" || true
    fi
    log_info "Removed AMPA_DISCORD_WEBHOOK from $env_file"
  fi
}

# Restore .env file from backup
restore_env_file() {
  local backup_path="$1"
  local target_env="$2"
  
  if [ -n "$backup_path" ] && [ -f "$backup_path" ]; then
    if mv "$backup_path" "$target_env" 2>/dev/null; then
      log_decision "RESTORED_ENV=$backup_path"
      return 0
    elif cp -p "$backup_path" "$target_env" 2>/dev/null; then
      log_decision "RESTORED_ENV_COPY=$backup_path"
      return 0
    else
      log_decision "RESTORE_FAILED=$backup_path"
      return 1
    fi
  fi
  return 1
}

# ============================================================================
# PYTHON VENV SETUP
# ============================================================================

# Find python executable (prefer python3, fall back to python)
check_python_executable() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  fi
}

# Create virtual environment
create_venv() {
  local venv_dir="$1"
  local py_bin="$2"
  
  if [ -d "$venv_dir" ]; then
    log_info "Virtual environment already exists at $venv_dir"
    return 0
  fi

  log_info "Creating virtualenv at $venv_dir"
  if ! "$py_bin" -m venv "$venv_dir" 2>&1 | tee /tmp/ampa_install_venv.log; then
    log_error "Error: failed to create venv with $py_bin -m venv. See /tmp/ampa_install_venv.log"
    return 1
  fi

  return 0
}

# Verify virtual environment is properly set up
verify_venv() {
  local venv_dir="$1"
  
  if [ ! -x "$venv_dir/bin/python" ]; then
    log_error "Error: virtualenv python not found at $venv_dir/bin/python"
    return 1
  fi

  return 0
}

# Install Python dependencies via pip
install_python_deps() {
  local venv_dir="$1"
  local req_file="$2"
  
  if [ ! -f "$req_file" ]; then
    log_info "No requirements.txt found; skipping pip install"
    return 0
  fi

  log_info "Upgrading pip and installing requirements into venv (logs: /tmp/ampa_install_pip.log)"
  "$venv_dir/bin/python" -m pip install --upgrade pip setuptools wheel 2>&1 | tee /tmp/ampa_install_pip.log || true

  if "$venv_dir/bin/python" -m pip install -r "$req_file" 2>&1 | tee -a /tmp/ampa_install_pip.log; then
    log_info "Installed Python dependencies into $venv_dir"
    return 0
  else
    log_error "Error: pip install failed. See /tmp/ampa_install_pip.log for details."
    log_error "You can try to re-run:"
    log_error "  $venv_dir/bin/python -m pip install -r $req_file"
    return 1
  fi
}

# ============================================================================
# PLUGIN INSTALLATION
# ============================================================================

# Validate that source file exists
validate_source_file() {
  if [ ! -f "$SRC" ]; then
    log_error "Source file not found: $SRC"
    log_error "If you intended to install the canonical plugin, run without arguments to use $DEFAULT_SRC"
    exit 2
  fi
}

# Install the Worklog .mjs plugin file
install_worklog_plugin() {
  local basename="$(basename "$SRC")"
  
  mkdir -p "$TARGET_DIR"
  cp -f "$SRC" "$TARGET_DIR/$basename"
  log_info "Installed Worklog plugin $SRC to $TARGET_DIR/$basename"
}

# Copy Python package into plugin directory
copy_python_package() {
   local py_target_dir="$TARGET_DIR/ampa_py"
   local env_backup=""
   local store_backup=""
   
   # Record pre-removal state
   log_decision "PRE_REMOVE_ls=$(ls -la \"$py_target_dir\" 2>/dev/null || true)"
   
   # Backup existing .env if present
   if [ -f "$py_target_dir/ampa/.env" ]; then
     env_backup=$(backup_env_file "$py_target_dir/ampa/.env")
   fi

    # Backup existing scheduler_store.json if present
    # Store backup OUTSIDE the ampa directory so it survives the rm -rf
    if [ -f "$py_target_dir/ampa/scheduler_store.json" ]; then
      store_backup=$(backup_env_file "$py_target_dir/ampa/scheduler_store.json" "$py_target_dir")
      log_decision "BACKUP_SCHEDULER_STORE=$store_backup"
    fi


   # Remove old bundle and copy new one
   mkdir -p "$py_target_dir"
   rm -rf "$py_target_dir/ampa"
   cp -R "ampa" "$py_target_dir/ampa"
   
    # Record post-copy state
    log_decision "POST_COPY_ls=$(ls -la \"$py_target_dir/ampa\" 2>/dev/null || true)"

    # Ensure scheduler_store.json exists for fresh installs
    if [ ! -f "$py_target_dir/ampa/scheduler_store.json" ]; then
      if [ -f "$py_target_dir/ampa/scheduler_store_example.json" ]; then
        cp -p "$py_target_dir/ampa/scheduler_store_example.json" "$py_target_dir/ampa/scheduler_store.json" 2>/dev/null || \
          cp "$py_target_dir/ampa/scheduler_store_example.json" "$py_target_dir/ampa/scheduler_store.json" 2>/dev/null || true
        log_info "Initialized scheduler_store.json from scheduler_store_example.json"
      else
        printf '{"commands": {}, "state": {}, "last_global_start_ts": null}\n' > "$py_target_dir/ampa/scheduler_store.json"
        log_info "Initialized empty scheduler_store.json"
      fi
    fi

   # Restore .env if we backed it up
   if [ -n "$env_backup" ]; then
     restore_env_file "$env_backup" "$py_target_dir/ampa/.env"
   fi

   # Restore scheduler_store.json if we backed it up
   if [ -n "$store_backup" ]; then
     restore_env_file "$store_backup" "$py_target_dir/ampa/scheduler_store.json"
     log_info "Preserved existing scheduler_store.json during upgrade"
   fi

   log_info "Installed Python ampa package to $py_target_dir/ampa"
}

# Set up Python package (venv and dependencies)
setup_python_package() {
  local py_target_dir="$TARGET_DIR/ampa_py"
  local req_file="$py_target_dir/ampa/requirements.txt"
  
  if [ ! -f "$req_file" ]; then
    log_info "No requirements.txt; skipping Python setup"
    return 0
  fi

  # Check for Python
  local py_bin
  py_bin=$(check_python_executable)
  if [ -z "$py_bin" ]; then
    log_error "Error: no python executable found in PATH; cannot create venv."
    exit 1
  fi

  # Create venv and install deps
  local venv_dir="$py_target_dir/venv"
  
  if ! create_venv "$venv_dir" "$py_bin"; then
    exit 1
  fi

  if ! verify_venv "$venv_dir"; then
    exit 1
  fi

  if ! install_python_deps "$venv_dir" "$req_file"; then
    exit 1
  fi

  return 0
}

# ============================================================================
# DAEMON RESTART HANDLING
# ============================================================================

# Detect if a daemon is currently running
detect_running_daemon() {
  if [ -f "$PID_FILE" ]; then
    local pid_val
    pid_val=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
    if [ -n "$pid_val" ] && kill -0 "$pid_val" 2>/dev/null; then
      echo "$pid_val"
      return 0
    fi
  fi
  return 1
}

# Prompt user whether to restart daemon
prompt_restart_daemon() {
  local running_pid="$1"
  
  if [ "$FORCE_RESTART" -eq 1 ]; then
    return 0  # Yes, restart
  fi

  if [ "$FORCE_NO_RESTART" -eq 1 ]; then
    return 1  # No, don't restart
  fi

  if [ -t 0 ]; then
    printf "Detected running daemon pid=%s. Restart automatically when installation completes? [y/N]: " "$running_pid"
    if ! read -r RESTART_ANS; then RESTART_ANS=""; fi
    case "$(printf "%s" "$RESTART_ANS" | tr '[:upper:]' '[:lower:]')" in
      y|yes)
        return 0  # Yes, restart
        ;;
      *)
        return 1  # No, don't restart
        ;;
    esac
  fi

  return 1  # Default to no restart
}

# Stop the running daemon
stop_daemon() {
  local basename="$(basename "$SRC")"
  log_info "Stopping running daemon before upgrade..."
  /usr/bin/env node "$TARGET_DIR/$basename" stop --name default || true
}

# Start the daemon
start_daemon() {
  local basename="$(basename "$SRC")"
  
  log_info "Attempting to restart daemon..."
  log_decision "Attempting restart: TARGET=$TARGET_DIR BINARY=$basename"

  if ! /usr/bin/env node "$TARGET_DIR/$basename" start --name default > /tmp/ampa_install_start.log 2>&1; then
    log_error "Warning: failed to start daemon; see /tmp/ampa_install_start.log"
    log_decision "RESTART=failed"
    return 1
  fi

  # Verify daemon started by checking PID file
  if [ -f "$PID_FILE" ]; then
    local newpid
    newpid=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
    if [ -n "$newpid" ] && kill -0 "$newpid" 2>/dev/null; then
      log_info "Started pid=$newpid"
      log_decision "RESTART=ok PID=$newpid"
      return 0
    else
      log_info "Start command executed; no running pid detected. See /tmp/ampa_install_start.log"
      log_decision "RESTART=unknown"
      return 1
    fi
  else
    log_info "Start command executed; no pid file created. See /tmp/ampa_install_start.log"
    log_decision "RESTART=no-pid-file"
    return 1
  fi
}

# ============================================================================
# MAIN FLOW
# ============================================================================

main() {
  # Setup
  local _ts=$(get_timestamp)
  acquire_lock
  create_decision_log_symlink
  
  # Parse arguments
  parse_args "$@"

  # Validate source
  validate_source_file

  # Check for existing installation
  local existing_install=0
  if detect_existing_installation; then
    existing_install=1
    if ! prompt_upgrade_or_abort; then
      exit 0
    fi
    log_decision "ACTION_PROCEED=1 EXISTING_INST=1"
  else
    log_decision "ACTION_PROCEED=1 EXISTING_INST=0"
  fi

  # Handle webhook configuration
  REMOVE_WEBHOOK=0
  SKIP_WEBHOOK_UPDATE=0
  local preserve_existing_env=0
  
  if check_for_bundled_env; then
    preserve_existing_env=1
    log_decision "PRESERVE_EXISTING_ENV_DETECTED=1 PATH=$TARGET_DIR/ampa_py/ampa/.env"
  fi

  if [ -z "$WEBHOOK" ]; then
    local existing_webhook
    existing_webhook=$(detect_existing_webhook)
    
    if [ "$existing_install" -eq 1 ]; then
      prompt_webhook_change "$existing_webhook"
    else
      prompt_webhook_new
    fi
  fi

  # Detect and possibly restart daemon
  local do_restart=0
  local running_pid
  running_pid=$(detect_running_daemon) || true
  if [ -n "$running_pid" ] && prompt_restart_daemon "$running_pid"; then
    do_restart=1
    stop_daemon
  fi

  # Mask webhook for logging
  local wh_mask="(empty)"
  if [ -n "$WEBHOOK" ]; then
    wh_mask="$(printf "%.8s" "$WEBHOOK")..."
  fi
  log_decision "SRC=$SRC TARGET=$TARGET_DIR WEBHOOK=$wh_mask"

  # Install plugin
  install_worklog_plugin

  # Install Python package if present
  if [ -d "ampa" ]; then
    copy_python_package
    setup_python_package

    # Handle .env file configuration
    if [ "$SKIP_WEBHOOK_UPDATE" -eq 1 ] || [ "$preserve_existing_env" -eq 1 ]; then
      log_info "Preserving existing .env (user requested no webhook update or pre-existing .env)"
    else
      if [ "$REMOVE_WEBHOOK" -eq 1 ]; then
        local env_file="$TARGET_DIR/ampa_py/ampa/.env"
        remove_webhook_from_env "$env_file"
      elif [ -n "$WEBHOOK" ]; then
        local env_file="$TARGET_DIR/ampa_py/ampa/.env"
        write_webhook_to_env "$env_file" "$WEBHOOK"
      else
        log_info "No webhook provided; skipping .env creation/update"
      fi
    fi
  fi

   # Restart daemon if requested, or start daemon for fresh installations
   if [ "$do_restart" -eq 1 ]; then
     start_daemon
   elif [ "$existing_install" -eq 0 ] && [ -d "ampa" ]; then
     # Fresh installation with AMPA plugin: start the daemon
     log_info "Starting daemon for fresh installation..."
     start_daemon
   fi


  log_info "Installation complete."
}
=======
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
>>>>>>> 2cdeaf9 (Add installer lock and per-run decision log to prevent concurrent installs and improve diagnostics (plugins/install-worklog-plugin.sh))

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

<<<<<<< HEAD
# Run main function with all arguments
main "$@"
=======
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
>>>>>>> 2cdeaf9 (Add installer lock and per-run decision log to prevent concurrent installs and improve diagnostics (plugins/install-worklog-plugin.sh))
