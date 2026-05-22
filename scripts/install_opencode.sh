#!/usr/bin/env bash
set -euo pipefail

DEFAULT_SRC="$HOME/projects/SorraAgents"
OPENCODE_LINK="$HOME/.config/opencode"
OPENCODE_COMMANDS_LINK="$OPENCODE_LINK/command"
OPENCODE_SKILLS_LINK="$OPENCODE_LINK/skill"
OPENCODE_AGENTS_LINK="$OPENCODE_LINK/AGENTS.md"

# expand leading ~ if present
expand_path() {
  local p="$1"
  if [[ "$p" == ~* ]]; then
    printf "%s" "${p/#\~/$HOME}"
  else
    printf "%s" "$p"
  fi
}

create_symlink() {
  local link="$1" target="$2"
  mkdir -p "$(dirname "$link")"

  # get canonical target if possible
  if command -v readlink >/dev/null 2>&1; then
    target_canon=$(readlink -f "$target" 2>/dev/null || true)
  else
    target_canon="$target"
  fi

  if [ -L "$link" ]; then
    # existing symlink
    existing=$(readlink -f "$link" 2>/dev/null || true)
    if [ -n "$existing" ] && [ "$existing" = "$target_canon" ]; then
      echo "OK: symlink already correct: $link -> $existing"
      return 0
    fi
  fi

  # If a non-symlink file/dir exists, back it up
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    backup="${link}.bak.$(date +%s)"
    echo "Backing up existing $link -> $backup"
    mv "$link" "$backup"
  fi

  ln -sfn "$target" "$link"
  echo "Created symlink: $link -> $target"
}

# Determine source directory
if [ -d "$DEFAULT_SRC" ]; then
  SRC_DIR="$DEFAULT_SRC"
  echo "Using source directory: $SRC_DIR"
else
  echo "Default source directory not found: $DEFAULT_SRC"
  while true; do
    read -r -e -p "Enter the SorraAgents project folder to link from (or 'q' to quit): " user_input
    if [ "$user_input" = "q" ] || [ "$user_input" = "Q" ]; then
      echo "Aborted by user." >&2
      exit 1
    fi
    user_input=$(expand_path "$user_input")
    if [ -d "$user_input" ]; then
      SRC_DIR="$user_input"
      break
    fi
    echo "Directory not found: $user_input"
  done
fi

COMMANDS_SRC="$SRC_DIR/command"
SKILLS_SRC="$SRC_DIR/skill"
AGENTS_SRC="$SRC_DIR/AGENTS.md"

# Validate source subdirs exist. If missing, prompt for a different root dir until both are found.
while true; do
  missing_commands=0
  missing_skills=0

  if [ ! -d "$COMMANDS_SRC" ]; then
    echo "Missing: commands source directory not found: $COMMANDS_SRC" >&2
    missing_commands=1
  fi
  if [ ! -d "$SKILLS_SRC" ]; then
    echo "Missing: skills source directory not found: $SKILLS_SRC" >&2
    missing_skills=1
  fi

  if [ $missing_commands -eq 0 ] && [ $missing_skills -eq 0 ]; then
    break
  fi

  echo ""
  read -r -e -p "Enter the SorraAgents project folder that contains 'command' and 'skill' (or 'q' to quit): " user_root
  if [ -z "$user_root" ]; then
    echo "No directory entered. Aborted."; exit 1
  fi
  if [ "$user_root" = "q" ] || [ "$user_root" = "Q" ]; then
    echo "Aborted by user." >&2
    exit 1
  fi
  user_root=$(expand_path "$user_root")
  if [ ! -d "$user_root" ]; then
    echo "Directory not found: $user_root" >&2
    continue
  fi

  SRC_DIR="$user_root"
  COMMANDS_SRC="$SRC_DIR/command"
  SKILLS_SRC="$SRC_DIR/skill"
  echo "Using source directory: $SRC_DIR"

  # loop back and re-check
done

mkdir -p "$OPENCODE_LINK"

create_symlink "$OPENCODE_COMMANDS_LINK" "$COMMANDS_SRC"
create_symlink "$OPENCODE_SKILLS_LINK" "$SKILLS_SRC"
create_symlink "$OPENCODE_AGENTS_LINK" "$AGENTS_SRC"

# --- opencode global config installation / export ---------------------------
# Repo-side opencode config files (relative to repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_CONFIG="$REPO_ROOT/opencode.json"
DEST_CONFIG="$OPENCODE_LINK/opencode.json"

# Helper to copy file with optional overwrite prompt
copy_if_missing_or_prompt() {
  local src="$1" dst="$2"
  if [ -e "$dst" ]; then
    read -r -e -p "File exists: $dst. Overwrite? [y/N]: " resp
    case "$resp" in
      [yY]|[yY][eE][sS]) cp -f "$src" "$dst" && echo "Overwrote $dst" ;;
      *) echo "Skipped $dst" ;;
    esac
  else
    cp -a "$src" "$dst" && echo "Installed $dst"
  fi
}

# If the repo has an opencode.json, offer to install it globally
if [ -e "$REPO_CONFIG" ]; then
  echo ""
  echo "Repository opencode.json found at: $REPO_CONFIG"
  copy_if_missing_or_prompt "$REPO_CONFIG" "$DEST_CONFIG"
fi

# Offer to export local opencode config back into the repo for sharing
if [ -e "$DEST_CONFIG" ] && [ ! "$DEST_CONFIG" -ef "$REPO_CONFIG" ]; then
  read -r -e -p "Would you like to copy your opencode config back to $REPO_CONFIG for tracking in git? [y/N]: " resp
  case "$resp" in
    [yY]|[yY][eE][sS])
      cp -a "$DEST_CONFIG" "$REPO_CONFIG" && echo "Exported opencode.json -> $REPO_CONFIG"
      ;;
    *) echo "Skipped export of local config." ;;
  esac
fi

echo ""
echo "Done. opencode features installed from: $SRC_DIR"
echo "  command -> $OPENCODE_COMMANDS_LINK"
echo "  skill   -> $OPENCODE_SKILLS_LINK"
echo "  AGENTS.md -> $OPENCODE_AGENTS_LINK"
