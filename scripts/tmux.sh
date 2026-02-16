#!/usr/bin/env bash
set -euo pipefail

# Creates a tmux session with three panes per window:
# - Left pane: full height, 50% width
# - Right top: 50% of right column height
# - Right bottom: 50% of right column height

SESSION="Dev"
DEFAULT_WINDOW="Agents"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/tmux.windows.conf"

create_three_pane_layout() {
  local target_window="$1"
  local left_cmd="$2"
  local top_right_cmd="$3"
  local pane_dir="$4"

  local left_pane
  local right_pane
  local bottom_right_pane

  left_pane="$(tmux display-message -p -t "$target_window" '#{pane_id}')"
  right_pane="$(tmux split-window -h -p 50 -P -F '#{pane_id}' -t "$target_window")"
  bottom_right_pane="$(tmux split-window -v -p 50 -P -F '#{pane_id}' -t "$right_pane")"

  if [[ -n "$pane_dir" ]]; then
    tmux send-keys -t "$left_pane" "cd $pane_dir" C-m
    tmux send-keys -t "$right_pane" "cd $pane_dir" C-m
    tmux send-keys -t "$bottom_right_pane" "cd $pane_dir" C-m
  fi

  if [[ -n "$left_cmd" ]]; then
    tmux send-keys -t "$left_pane" "$left_cmd" C-m
  fi

  if [[ -n "$top_right_cmd" ]]; then
    tmux send-keys -t "$right_pane" "$top_right_cmd" C-m
  fi

  tmux select-pane -t "$left_pane"
}

# If the session already exists, attach to it
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux attach-session -t "$SESSION"
  exit 0
fi

windows=()
dirs=()
left_cmds=()
top_cmds=()

if [[ -f "$CONFIG_FILE" ]]; then
  while IFS='|' read -r name dir left_cmd top_cmd; do
    if [[ -z "$name" || "$name" == \#* ]]; then
      continue
    fi

    windows+=("$name")
    dirs+=("$dir")
    left_cmds+=("$left_cmd")
    top_cmds+=("$top_cmd")
  done < "$CONFIG_FILE"
fi

if [[ ${#windows[@]} -eq 0 ]]; then
  windows=("$DEFAULT_WINDOW")
  dirs=("$HOME/.config/opencode")
  left_cmds=("opencode -c")
  top_cmds=("wl tui")
fi

# Ensure the first window (Agents) uses the user's opencode config dir.
# If the first window is the default "Agents" window and no explicit dir
# was provided in the config, make it work in $HOME/.config/opencode.
if [[ "${windows[0]:-}" == "$DEFAULT_WINDOW" ]]; then
  dirs[0]="${dirs[0]:-$HOME/.config/opencode}"
fi

# Start a new detached session with a single pane
tmux new-session -d -s "$SESSION" -n "${windows[0]}"

# Tmux appearance configuration
tmux set -g base-index 1
tmux setw -g window-status-current-style fg=black,bg=green
tmux set -g window-style bg=colour235
tmux set -g window-active-style bg=colour234

# Default pane border colors
tmux set -g pane-border-style fg=colour238
tmux set -g pane-active-border-style fg=colour45

set_window_colors() {
  local target="$1"
  local border="$2"
  local active="$3"

  tmux set -w -t "$target" pane-border-style "fg=$border"
  tmux set -w -t "$target" pane-active-border-style "fg=$active"
}

apply_window_colors() {
  local index="$1"
  local target="$2"

  case "$index" in
    1) set_window_colors "$target" colour28 colour46 ;;
    2) set_window_colors "$target" colour160 colour196 ;;
    3) set_window_colors "$target" colour26 colour39 ;;
    4) set_window_colors "$target" colour94 colour130 ;;
    5) set_window_colors "$target" colour61 colour98 ;;
    6) set_window_colors "$target" colour22 colour34 ;;
    7) set_window_colors "$target" colour52 colour88 ;;
    8) set_window_colors "$target" colour17 colour25 ;;
    9) set_window_colors "$target" colour178 colour214 ;;
    10) set_window_colors "$target" colour24 colour37 ;;
    *) set_window_colors "$target" colour238 colour45 ;;
  esac
}

create_three_pane_layout "$SESSION:${windows[0]}" "${left_cmds[0]}" "${top_cmds[0]}" "${dirs[0]}"
apply_window_colors 1 "$SESSION:${windows[0]}"

for i in "${!windows[@]}"; do
  if [[ $i -eq 0 ]]; then
    continue
  fi

  tmux new-window -t "$SESSION" -n "${windows[$i]}"
  create_three_pane_layout "$SESSION:${windows[$i]}" "${left_cmds[$i]}" "${top_cmds[$i]}" "${dirs[$i]}"
  apply_window_colors $((i + 1)) "$SESSION:${windows[$i]}"
done

# Attach to the session
tmux attach-session -t "$SESSION"
