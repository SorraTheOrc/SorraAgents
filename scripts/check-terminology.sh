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
# Each line is an extended regex anchored to a specific exemption category.
# ---------------------------------------------------------------------------
ALLOWLIST=(
  # --- Package / module names ---
  'opencode_ai'                     # Python SDK import
  'opencode-ai'                     # PyPI package name
  '@opencode-ai/plugin'             # npm package
  '@opencode-ai/sdk'                # npm package

  # --- Schema / config file URLs and names ---
  'https://opencode\.ai/'           # opencode.ai schema/config URLs
  'opencode\.json'                  # opencode.json config filename
  'tui\.json.*opencode\.ai'         # tui.json schema reference

  # --- Environment variable names ---
  'OPENCODE_BASE_URL'
  'OPENCODE_PROVIDER_ID'
  'OPENCODE_MODEL_ID'
  'AMPA_DELEGATION_OPENCODE_TIMEOUT'

  # --- CLI binary / command literal ---
  'opencode -c'                     # CLI invocation flag
  'opencode run '                   # CLI run command (workflow dispatch)
  'opencode skill run'              # CLI skill command

  # --- External repository URLs (historical / migration references) ---
  'github\.com/opencode/ampa'       # AMPA repo URL
  'github\.com/opencode/opencode'   # OpenCode repo URL

  # --- Directory paths (XDG config conventions) ---
  '\.config/opencode/'              # User config directory path
  '\$XDG_CONFIG_HOME/opencode/'     # Variable-based config path
  'XDG_CONFIG_HOME.*opencode'       # Config path reference

  # --- Dot-config directories ---
  '\.opencode/'                     # .opencode directory (tool config root)
  '\.opencode"'                     # .opencode in JSON
  '\.opencode,'                     # .opencode in JSON

  # --- Node modules / lockfiles ---
  'node_modules/@opencode-ai/'      # Installed dependency path
  'registry\.npmjs\.org.*opencode'  # npm registry URL
  '"name": "opencode"'              # package.json name field
  '"name":"opencode"'               # package.json name field (minified)

  # --- Model provider identifiers ---
  'opencode-go/'                    # Model provider prefix (e.g. opencode-go/glm-5.1)
  'opencode/claude-'                # Model provider prefix
  'opencode/gpt-'                   # Model provider prefix

  # --- Historical / attribution references ---
  'Author: OpenCode'                # Audit report author attribution
  'Author \| opencode'              # PRD author field
  'opencode-patch-'                 # Agent instance naming convention
  'OpenCode agent'                  # Agent type reference in PRD
  'OpenCode server'                 # Server reference in error hint
  'OpenCode Python SDK'             # SDK reference in examples
  'OpenCode repository'             # Historical repo references
  'OpenCode documentation'          # Historical doc references
  'OpenCode config'                 # Config references
  'OpenCode ignore'                 # Ignore policy references
  'OpenCode file-include'           # File include rule reference
  'OpenCode compaction'             # Plugin description
  'OpenCode command'                # Command authoring reference
  'OpenCode best practices'         # Standards reference
  'OpenCode TUI'                    # TUI reference
  'legacy.*opencode'                # Legacy variable references
  'OpenCode test'                   # Test naming

  # --- Specific file-level exceptions ---
  'opencode\.json\.tui-migration\.bak'  # Backup filename
  'install_opencode\.sh'            # Installer script filename
  'test_implement_skill_doc_hygiene.*opencode'  # Test that checks opencode path hygiene

  # --- Class / function names in code and docs ---
  'OpenCodeRunDispatcher'           # Dispatcher class name in engine PRD
  'Opencode('                       # Python SDK class constructor

  # --- Path assertions in tests ---
  "'opencode', '.worklog'"           # Test path join assertions
  '"opencode", ".worklog"'           # Test path join assertions (JS)
  "path.join.*'opencode'"            # JS path.join with 'opencode' segment

  # --- The check script itself references ---
  'scripts/check-terminology\.sh'   # Self-reference in scan script
  'check-terminology\.sh:'          # Self-reference in scan script output

  # --- Test files: docstrings and test string literals ---
  'tests/test_terminology_check'    # The test file itself

  # --- Migration doc prose about the OpenCode repo as a historical entity ---
  'in OpenCode'                     # "in OpenCode" historical references
  'not OpenCode'                    # "not OpenCode" historical references
  'Updating OpenCode Documentation' # Migration doc section
  'OpenCode README'                 # Migration doc link text

  # --- delegation-control.md ---
  'delegated .opencode run. process' # delegation-control refers to opencode run as a process

  # --- Reports ---
  'Repository root.*\.config/opencode'  # Historical audit report

  # --- README title and plugin reference ---
  '# OpenCode'                     # README title
  'local OpenCode plugins'          # README plugin reference

  # --- skill files: comment.txt ---
  'opencode/wl CLI'                # Skill comment about CLI
  'wl/opencode CLI'                # Skill comment about CLI

  # --- triage-audit.md ---
  'handler / opencode'             # triage-audit doc shorthand

  # --- compatibility metadata ---
  'compatibility: opencode'        # resolve-pr-comments SKILL.md metadata

  # --- test assertions ---
  'Expected remote model to contain opencode/qwen'  # test assertion text

  # --- author-command SKILL.md ---
  'placeholders supported by OpenCode'  # Special placeholder reference

  # --- session_block.py ---
  'opencode_tool_output'           # Default temp directory name

  # --- workflow examples ---
  'opencode run, Discord bot'      # README examples reference
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
rg -n --hidden --glob '!.git/**' --glob '!package-lock.json' -i 'opencode|open.code' . > "$MATCHES_FILE" 2>/dev/null || true

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
echo "  6. External repo URLs: github.com/opencode/ampa, github.com/opencode/opencode"
echo "  7. Directory paths: .config/opencode/, .opencode/, \$XDG_CONFIG_HOME/opencode/"
echo "  8. npm packages/lockfiles: @opencode-ai/plugin, @opencode-ai/sdk"
echo "  9. Model provider prefixes: opencode-go/*, opencode/*"
echo " 10. Historical/attribution references in PRD and migration docs"
echo " 11. Agent instance naming: opencode-patch-*"
echo ""

if [ "$STRICT" -eq 1 ] && [ "$VIOLATIONS" -gt 0 ]; then
  echo "RESULT: FAIL — $VIOLATIONS non-allowed match(es) found in strict mode."
  exit 1
fi

echo "RESULT: PASS — scan complete ($VIOLATIONS non-allowed match(es) flagged for review)."
exit 0
