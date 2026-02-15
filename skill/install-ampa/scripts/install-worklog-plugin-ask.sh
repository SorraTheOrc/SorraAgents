#!/usr/bin/env sh
# Interactive wrapper around install-worklog-plugin.sh to request a Discord webhook
set -eu

SCRIPT_DIR=$(dirname "$0")
INSTALL_SH="$SCRIPT_DIR/install-worklog-plugin.sh"

if [ ! -x "$INSTALL_SH" ] && [ ! -f "$INSTALL_SH" ]; then
  echo "installer not found: $INSTALL_SH" >&2
  exit 2
fi

WEBHOOK=""
AUTO_YES=0
EXTRA_ARGS=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y)
      AUTO_YES=1
      shift
      ;;
    --webhook)
      shift
      WEBHOOK="$1"
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--webhook <url>] [--yes] [-- <installer-args>]"
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS="$*"
      break
      ;;
    *)
      # pass-through positional arguments to installer
      EXTRA_ARGS="$EXTRA_ARGS $1"
      shift
      ;;
  esac
done

# Simple Discord webhook validation
is_valid_webhook() {
  case "$1" in
    *"/webhooks/"*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ -z "$WEBHOOK" ] && [ "$AUTO_YES" -ne 1 ]; then
  printf "Enter Discord webhook URL to use for AMPA notifications (leave empty to skip): \n> "
  if ! read -r WEBHOOK; then WEBHOOK=""; fi
  WEBHOOK=$(printf "%s" "$WEBHOOK" | sed 's/^\s*//;s/\s*$//')
  if [ -n "$WEBHOOK" ] && ! is_valid_webhook "$WEBHOOK"; then
    printf "Note: the value you entered does not look like a Discord webhook. Continue anyway? [y/N]: "
    if ! read -r CH; then CH=""; fi
    case "$(printf "%s" "$CH" | tr '[:upper:]' '[:lower:]')" in
      y|yes) ;;
      *)
        echo "Aborting; no webhook configured." >&2
        WEBHOOK=""
        ;;
    esac
  fi
fi

CMD="$INSTALL_SH"
if [ -n "$WEBHOOK" ]; then
  CMD="$CMD --webhook $WEBHOOK"
fi

if [ -n "$EXTRA_ARGS" ]; then
  CMD="$CMD $EXTRA_ARGS"
fi

echo "Running installer..."
sh -c "$CMD"
