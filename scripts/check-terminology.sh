#!/usr/bin/env bash
# check-terminology.sh — Verification baseline for neutral terminology cleanup.
#
# Scans the repository for framework-specific "opencode"/"OpenCode" mentions
# and classifies them as PRIMARY (agent/**, command/**, skill/**) or SECONDARY
# (all other paths). Technical identifiers that must be preserved are checked
# against an allowlist; violations are reported as actionable output.
#
# Usage:
#   ./scripts/check-terminology.sh            # full scan, exit 0 with report
#   ./scripts/check-terminology.sh --strict   # exit 1 if any non-allowed primary-scope match exists
#
# Exit codes:
#   0 — scan complete; no non-allowed primary matches (or --strict not set)
#   1 — --strict mode and non-allowed primary matches found
#   2 — usage error

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

STRICT=0
for arg in "$@"; do
  case "$arg" in
    --strict) STRICT=1 ;;
    -h|--help)
      echo "Usage: $0 [--strict]"
      echo ""
      echo "  --strict  Exit 1 if any non-allowed primary-scope (agent/**, command/**, skill/**)"
      echo "            opencode/OpenCode mention is found."
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Allowlist: patterns that are exempt from neutralisation because they are
# technical identifiers, external URLs, package names, schema URLs, config
# file names, or other tokens that must not be changed.
#
# Keep this allowlist conservative: only include clear technical tokens. Do
# not include generic prose references like 'OpenCode' which should be
# detected as non-allowed in strict mode.
# ---------------------------------------------------------------------------
ALLOWLIST=(
  # --- Package / module names ---
  'opencode_ai'
  'opencode-ai'
  '@opencode-ai/plugin'
  '@opencode-ai/sdk'

  # --- Schema / config file URLs and names ---
  'https://opencode\.ai/'
  'opencode\.json'

  # --- Environment variable names ---
  'OPENCODE_BASE_URL'
  'OPENCODE_PROVIDER_ID'
  'OPENCODE_MODEL_ID'
  'AMPA_DELEGATION_OPENCODE_TIMEOUT'

  # --- CLI binary / command literal ---
  'opencode -c'
  'opencode run'
  'opencode skill run'

  # --- External repository URLs ---
  'github\.com/opencode/ampa'

  # --- Directory paths ---
  '[.]config/opencode'
  '\.opencode/'

  # --- Node modules / registry ---
  'node_modules/@opencode-ai/'
  'registry\.npmjs\.org.*opencode'

  # --- Model provider identifiers ---
  'opencode-go/'
  'opencode/claude-'
  'opencode/gpt-'

  # --- Misc technical tokens ---
  'opencode-patch-'
  'OpenCodeRunDispatcher'
  'Opencode[(]'
  'scripts/check-terminology\.sh'
  'tests/test_terminology_check'
)

# Build the combined allowlist regex
ALLOWLIST_RE=""
for pat in "${ALLOWLIST[@]}"; do
  if [ -z "$ALLOWLIST_RE" ]; then
    ALLOWLIST_RE="$pat"
  else
    ALLOWLIST_RE="${ALLOWLIST_RE}|${pat}"
  fi
done

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
echo "=== Terminology Verification Scan ==="
echo "Repository: $REPO_ROOT"
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# Gather all matches (line numbers + content)
MATCHES_FILE=$(mktemp)
# Prefer ripgrep (rg) for speed and hidden file support; fall back to grep
if command -v rg >/dev/null 2>&1; then
  rg -n --hidden --glob '!.git/**' --glob '!package-lock.json' -i 'opencode' . > "$MATCHES_FILE" 2>/dev/null || true
else
  # Grep fallback that mirrors the intent of the rg invocation
  grep -Rni --exclude-dir=.git --exclude=package-lock.json -e 'opencode' . > "$MATCHES_FILE" 2>/dev/null || true
fi

TOTAL=$(wc -l < "$MATCHES_FILE")
echo "Total raw matches: $TOTAL"
echo ""

# Classify matches
PRIMARY_COUNT=0
SECONDARY_COUNT=0
ALLOWED_COUNT=0
VIOLATIONS=0

PRIMARY_FILE=$(mktemp)
SECONDARY_FILE=$(mktemp)
VIOLATIONS_FILE=$(mktemp)

while IFS= read -r line; do
  [ -z "$line" ] && continue

  # Extract path (before first colon)
  filepath="${line%%:*}"

  # Check if path matches a primary-scope directory
  is_primary=0
  case "$filepath" in
    ./agent/*|./command/*|./skill/*) is_primary=1 ;;
  esac

  # Check if the line matches an allowlist pattern
  is_allowed=0
  if echo "$line" | grep -qE "$ALLOWLIST_RE"; then
    is_allowed=1
  fi

  if [ "$is_primary" -eq 1 ]; then
    PRIMARY_COUNT=$((PRIMARY_COUNT + 1))
    if [ "$is_allowed" -eq 0 ]; then
      VIOLATIONS=$((VIOLATIONS + 1))
      echo "$line" >> "$VIOLATIONS_FILE"
    fi
  else
    SECONDARY_COUNT=$((SECONDARY_COUNT + 1))
    if [ "$is_allowed" -eq 0 ]; then
      VIOLATIONS=$((VIOLATIONS + 1))
      echo "$line" >> "$VIOLATIONS_FILE"
    fi
  fi
done < "$MATCHES_FILE"

ALLOWED_COUNT=$((TOTAL - VIOLATIONS))

echo "--- Classification Summary ---"
echo "Primary scope (agent/**, command/**, skill/**): $PRIMARY_COUNT"
echo "Secondary scope (all other paths):            $SECONDARY_COUNT"
echo "Allowed (technical identifiers / exceptions): $ALLOWED_COUNT"
echo "Non-allowed (requires review/neutralisation):  $VIOLATIONS"
echo ""

if [ "$VIOLATIONS" -gt 0 ]; then
  echo "--- Non-Allowed Matches (requiring neutralisation or exception review) ---"
  cat "$VIOLATIONS_FILE"
  echo ""
fi

# Cleanup temp files
rm -f "$MATCHES_FILE" "$PRIMARY_FILE" "$SECONDARY_FILE" "$VIOLATIONS_FILE"

echo "--- Exception Categories ---"
echo "The following categories are preserved (allowlisted):"
echo "  1. Package/module names: opencode_ai, opencode-ai, @opencode-ai/*"
echo "  2. Schema/config URLs: https://opencode.ai/*"
echo "  3. Config filenames: opencode.json, tui.json"
echo "  4. Environment variables: OPENCODE_BASE_URL, OPENCODE_PROVIDER_ID, OPENCODE_MODEL_ID"
echo "  5. CLI binary/command literals: 'opencode run', 'opencode -c', 'opencode skill run'"
echo "  6. External repo URLs: github.com/opencode/ampa"
echo "  7. Directory paths: .config/opencode/, .opencode/"
echo "  8. npm packages/lockfiles: @opencode-ai/plugin, @opencode-ai/sdk"
echo "  9. Model provider prefixes: opencode-go/*, opencode/*"
echo " 10. Agent instance naming: opencode-patch-*"
echo ""

if [ "$STRICT" -eq 1 ] && [ "$VIOLATIONS" -gt 0 ]; then
  echo "RESULT: FAIL — $VIOLATIONS non-allowed match(es) found in strict mode."
  exit 1
fi

echo "RESULT: PASS — scan complete ($VIOLATIONS non-allowed match(es) flagged for review)."
exit 0
