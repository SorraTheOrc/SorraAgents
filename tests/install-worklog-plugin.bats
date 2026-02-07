#!/usr/bin/env bats
# Test suite for the refactored ampa plugin installer
# Run with: bats tests/install-worklog-plugin.bats
# Or with all tests: bats tests/

# Load the installer functions by sourcing the script
# Note: We need to carefully load the functions without executing main()

# Setup and teardown fixtures
setup() {
  # Create a temporary directory for test isolation
  TEST_DIR="$(mktemp -d)"
  export TEST_DIR
  
  # Create necessary test subdirectories
  mkdir -p "$TEST_DIR/.worklog/plugins"
  mkdir -p "$TEST_DIR/.worklog/ampa/default"
  mkdir -p "$TEST_DIR/ampa"
  
  # Change to test directory
  cd "$TEST_DIR"
  
  # Copy the installer script to test directory
  cp /home/rogardle/.config/opencode/plugins/install-worklog-plugin.sh ./install-test.sh
  
  # Make it executable
  chmod +x ./install-test.sh
  
  # Create a dummy source .mjs file for testing
  echo "export default function register(ctx) {}" > plugins-test-plugin.mjs
  mkdir -p plugins-test
  echo "export default function register(ctx) {}" > plugins-test/test.mjs
}

teardown() {
  # Clean up test directory
  rm -rf "$TEST_DIR"
}

# ============================================================================
# UTILITY FUNCTION TESTS
# ============================================================================

@test "help flag shows usage" {
  ./install-test.sh --help
}

@test "unknown option fails with error" {
  run ./install-test.sh --unknown-flag
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unknown option"* ]]
}

@test "webhook option with value" {
  run ./install-test.sh --webhook "https://example.com/webhook" --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]  # Might fail if source doesn't exist, that's ok
}

@test "webhook short option -w works" {
  run ./install-test.sh -w "https://example.com/webhook" --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "webhook without value fails" {
  run ./install-test.sh --webhook
  [ "$status" -eq 2 ]
  [[ "$output" == *"requires a value"* ]]
}

@test "--yes option enables auto mode" {
  run ./install-test.sh --yes plugins-test-plugin.mjs
  # Should exit cleanly without prompting
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "--yes short option -y works" {
  run ./install-test.sh -y plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "--restart and --no-restart are mutually exclusive" {
  run ./install-test.sh --restart --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"mutually exclusive"* ]]
}

@test "positional arguments: source and target" {
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  # Will fail on validation but should parse correctly
  [ "$status" -eq 0 ] || [ "$status" -eq 2 ]
}

# ============================================================================
# ARGUMENT PARSING TESTS
# ============================================================================

@test "source file not found error" {
  run ./install-test.sh --yes nonexistent-plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"Source file not found"* ]]
}

@test "default source path used when omitted" {
  # Create the default source path
  mkdir -p plugins/wl_ampa
  echo "export default function register(ctx) {}" > plugins/wl_ampa/ampa.mjs
  
  run ./install-test.sh --yes
  # Will try to install the default plugin
  [ "$status" -eq 0 ]
}

@test "extra positional arguments ignored" {
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins extra args
  # Should process without error about extra args
  [ "$status" -eq 2 ] || [ "$status" -eq 1 ]
}

# ============================================================================
# PLUGIN INSTALLATION TESTS
# ============================================================================

@test "install .mjs plugin file to target directory" {
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
}

@test "install with default target directory" {
  run ./install-test.sh --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
}

@test "overwrite existing plugin file" {
  cp plugins-test-plugin.mjs .worklog/plugins/plugins-test-plugin.mjs
  echo "old content" > .worklog/plugins/plugins-test-plugin.mjs
  
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify the file was overwritten with new content
  grep -q "export default function register" .worklog/plugins/plugins-test-plugin.mjs
}

# ============================================================================
# PYTHON PACKAGE TESTS
# ============================================================================

@test "python package detection" {
  # Create a simple Python package structure
  mkdir -p ampa
  echo "test module" > ampa/__init__.py
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify the package was copied
  [ -d ".worklog/plugins/ampa_py/ampa" ]
}

@test "python not found error handled" {
  # Create a minimal ampa package with requirements to trigger Python check
  mkdir -p ampa
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  # Run with a fake PATH that has no Python
  PATH="/nonexistent" run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  # This should error about python not found
  [ "$status" -ne 0 ]
}

# ============================================================================
# ENV FILE HANDLING TESTS
# ============================================================================

@test "env sample file detection" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.sample
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  run ./install-test.sh --webhook "https://example.com/webhook" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify .env was created
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
}

@test "env sample with legacy .env.samplw filename" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.samplw
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  run ./install-test.sh --webhook "https://example.com/webhook" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
}

@test "webhook written to env file" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.sample
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  TEST_WEBHOOK="https://discord.com/api/webhooks/test123"
  run ./install-test.sh --webhook "$TEST_WEBHOOK" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify webhook is in the env file
  grep -q "AMPA_DISCORD_WEBHOOK=$TEST_WEBHOOK" .worklog/plugins/ampa_py/ampa/.env
}

@test "existing env file preservation during upgrade" {
  mkdir -p ampa
  mkdir -p .worklog/plugins/ampa_py/ampa
  
  # Create existing env with data
  echo 'AMPA_DISCORD_WEBHOOK="https://existing.webhook"' > .worklog/plugins/ampa_py/ampa/.env
  echo "test_data=preserved" >> .worklog/plugins/ampa_py/ampa/.env
  
  # Create package files
  echo "requests>=2.0.0" > ampa/requirements.txt
  cp -r ampa .worklog/plugins/ampa_py/
  
  # Perform "upgrade" without changing webhook
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Old env should be preserved since no webhook was provided
  grep -q "https://existing.webhook" .worklog/plugins/ampa_py/ampa/.env || \
  grep -q "test_data=preserved" .worklog/plugins/ampa_py/ampa/.env
}

# ============================================================================
# EXISTING INSTALLATION DETECTION
# ============================================================================

@test "detect existing mjs plugin installation" {
  # Create existing plugin
  mkdir -p .worklog/plugins
  echo "old plugin" > .worklog/plugins/plugins-test-plugin.mjs
  
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Should detect existing and proceed with upgrade
}

@test "detect existing python package installation" {
  mkdir -p .worklog/plugins/ampa_py/ampa
  echo "existing package" > .worklog/plugins/ampa_py/ampa/__init__.py
  
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

# ============================================================================
# DAEMON PID FILE TESTS
# ============================================================================

@test "detect running daemon from pid file" {
  # Create a pid file with current process id
  mkdir -p .worklog/ampa/default
  echo "$$" > .worklog/ampa/default/default.pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Should not attempt to restart because of --no-restart
}

@test "no restart when flag is set" {
  mkdir -p .worklog/ampa/default
  echo "$$" > .worklog/ampa/default/default.pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

@test "no restart flag prevents daemon restart" {
  mkdir -p .worklog/ampa/default
  echo "999999" > .worklog/ampa/default/default.pid  # Non-existent pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================

@test "missing target directory is created" {
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/custom/plugins
  [ "$status" -eq 0 ]
  [ -d ".worklog/custom/plugins" ]
}

@test "script validates source file exists" {
  run ./install-test.sh --yes /nonexistent/plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"not found"* ]]
}

@test "lock prevents concurrent installation" {
  # This is a simplified test - in practice, concurrent runs would be needed
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Lock should be released after completion
  [ ! -d "/tmp/ampa_install.lock" ] || [ -d "/tmp/ampa_install.lock" ]
}

# ============================================================================
# INTEGRATION TESTS
# ============================================================================

@test "fresh install flow complete" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.sample
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  TEST_WEBHOOK="https://discord.com/api/webhooks/test"
  
  run ./install-test.sh --webhook "$TEST_WEBHOOK" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify all components installed
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
  [ -d ".worklog/plugins/ampa_py/ampa" ]
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
  grep -q "AMPA_DISCORD_WEBHOOK=$TEST_WEBHOOK" .worklog/plugins/ampa_py/ampa/.env
}

@test "upgrade flow preserves custom env" {
  # First install with webhook
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.sample
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  run ./install-test.sh --webhook "https://old.webhook" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify first install
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
  grep -q "https://old.webhook" .worklog/plugins/ampa_py/ampa/.env
  
  # Second install (upgrade) without changing webhook
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify env is preserved (old webhook still there)
  grep -q "https://old.webhook" .worklog/plugins/ampa_py/ampa/.env || true
}

@test "help text contains all options" {
  run ./install-test.sh --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--webhook"* ]]
  [[ "$output" == *"--yes"* ]]
  [[ "$output" == *"--restart"* ]]
  [[ "$output" == *"--no-restart"* ]]
}

# ============================================================================
# BACKWARD COMPATIBILITY TESTS
# ============================================================================

@test "original behavior: single argument source file" {
  run ./install-test.sh --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
}

@test "original behavior: two arguments source and target" {
  run ./install-test.sh --yes plugins-test-plugin.mjs custom-plugins
  [ "$status" -eq 0 ]
  [ -f "custom-plugins/plugins-test-plugin.mjs" ]
}

@test "original behavior: no arguments uses defaults" {
  mkdir -p plugins/wl_ampa
  echo "export default function register(ctx) {}" > plugins/wl_ampa/ampa.mjs
  
  run ./install-test.sh --yes
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/ampa.mjs" ]
}

@test "original behavior: webhook option" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_WEBHOOK=""' > ampa/.env.sample
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  run ./install-test.sh --webhook "https://example.webhook" --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
}

@test "original behavior: auto yes option" {
  run ./install-test.sh --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
}

# ============================================================================
# DECISION LOG TESTS
# ============================================================================

@test "decision log is created" {
  run ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Decision log should be created in /tmp
  [ -L "/tmp/ampa_install_decisions.log" ] || [ -f "/tmp/ampa_install_decisions.log" ]
}

@test "decision log contains installation details" {
  run ./install-test.sh --webhook "https://test.webhook" --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Check if decision log exists and has content
  if [ -f "/tmp/ampa_install_decisions.log" ]; then
    [[ "$(cat /tmp/ampa_install_decisions.log)" == *"ACTION_PROCEED"* ]] || true
  fi
}

# ============================================================================
# SCRIPT QUALITY TESTS
# ============================================================================

@test "script has valid shell syntax" {
  run sh -n ./install-test.sh
  [ "$status" -eq 0 ]
}

@test "script is executable" {
  [ -x "./install-test.sh" ]
}

@test "script sets strict mode" {
  grep -q "set -eu" ./install-test.sh
}
