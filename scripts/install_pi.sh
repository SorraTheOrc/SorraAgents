#!/usr/bin/env bash
set -euo pipefail

DEFAULT_SRC="$HOME/projects/SorraAgents"
PROMPTS_LINK="$HOME/.pi/agent/prompts"
SKILLS_LINK="$HOME/.pi/agent/skills"

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

PROMPTS_SRC="$SRC_DIR/command"
SKILLS_SRC="$SRC_DIR/skill"

# Validate source subdirs exist. If missing, prompt for a different root dir until both are found.
while true; do
  missing_prompts=0
  missing_skills=0

  if [ ! -d "$PROMPTS_SRC" ]; then
    echo "Missing: prompts source directory not found: $PROMPTS_SRC" >&2
    missing_prompts=1
  fi
  if [ ! -d "$SKILLS_SRC" ]; then
    echo "Missing: skills source directory not found: $SKILLS_SRC" >&2
    missing_skills=1
  fi

  if [ $missing_prompts -eq 0 ] && [ $missing_skills -eq 0 ]; then
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
  PROMPTS_SRC="$SRC_DIR/command"
  SKILLS_SRC="$SRC_DIR/skill"
  echo "Using source directory: $SRC_DIR"

  # loop back and re-check
done

create_symlink "$PROMPTS_LINK" "$PROMPTS_SRC"
create_symlink "$SKILLS_LINK" "$SKILLS_SRC"

echo "Done."
