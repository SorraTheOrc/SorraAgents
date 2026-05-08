#!/usr/bin/env bash
set -euo pipefail

DEFAULT_SRC="$HOME/projects/SorraAgents"
PROMPTS_LINK="$HOME/.pi/agent/prompts"
SKILLS_LINK="$HOME/.pi/agent/skills"
AGENTS_LINK="$HOME/.pi/agent/AGENTS.md"

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
AGENTS_SRC="$SRC_DIR/AGENTS.md"

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
create_symlink "$AGENTS_LINK" "$AGENTS_SRC"

# --- Pi global config installation / export --------------------------------
# Repo-side pi config directory (relative to repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_PI_CONFIG="$REPO_ROOT/.pi-config/agent"
DEST_PI="$HOME/.pi/agent"

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

# If the repo contains a .pi-config/agent, use it to create the global config
if [ -d "$REPO_PI_CONFIG" ]; then
  echo "Installing global pi config from repository: $REPO_PI_CONFIG -> $DEST_PI"
  mkdir -p "$DEST_PI"
  for f in settings.json models.json; do
    if [ -e "$REPO_PI_CONFIG/$f" ]; then
      copy_if_missing_or_prompt "$REPO_PI_CONFIG/$f" "$DEST_PI/$f"
    fi
  done
  # If an example auth exists in the repo, do NOT copy auth files. Instead instruct the user to login with pi
  if [ -e "$REPO_PI_CONFIG/auth.json.example" ] && [ ! -e "$DEST_PI/auth.json" ]; then
    echo "Authentication not configured. After installation run:"
    echo "  pi login"
    echo "This will open a browser and create ~/.pi/agent/auth.json for you. Do NOT store real credentials in the repository."
  fi

else
  # No repo config - if user has a local config, offer to export it into the repo for tracking
  if [ -d "$DEST_PI" ]; then
    found=0
    for f in settings.json models.json; do
      [ -e "$DEST_PI/$f" ] && found=1 || true
    done
    if [ $found -eq 1 ]; then
      read -r -e -p "Found existing ~/.pi/agent config. Would you like to copy non-secret config files into $REPO_ROOT/.pi-config/agent for tracking in git? [y/N]: " resp
      case "$resp" in
        [yY]|[yY][eE][sS])
          mkdir -p "$REPO_PI_CONFIG"
          for f in settings.json models.json; do
            if [ -e "$DEST_PI/$f" ]; then
              cp -a "$DEST_PI/$f" "$REPO_PI_CONFIG/$f" && echo "Exported $f -> $REPO_PI_CONFIG/$f"
            fi
          done
          # Do NOT export auth.json or create an auth.json.example. Instead instruct the user to run `pi login` locally to create auth credentials
          if [ -e "$DEST_PI/auth.json" ]; then
            echo "Found local auth.json. For security we will NOT export it into the repository."
            echo "Users should run:"
            echo "  pi login"
            echo "after installing the tracked config to create their own ~/.pi/agent/auth.json."
          fi
          ;;
        *) echo "Skipping export of local config." ;;
      esac
    fi
  fi
fi

# If auth.json is still missing, remind the user to login with pi to create credentials
if [ ! -e "$DEST_PI/auth.json" ]; then
  echo ""
  echo "To finish setup and authenticate, run the following command now:"
  echo "  pi login"
  echo "This will open a browser and create ~/.pi/agent/auth.json for your account."
fi

echo "Done."
