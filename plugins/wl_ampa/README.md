# wl ampa plugin

This repository contains a Worklog (wl) plugin that provides a minimal, project-scoped daemon helper: start, stop and status commands for local development daemons.

Location
- Canonical plugin file: plugins/wl_ampa/ampa.mjs
- Installer: plugins/install-worklog-plugin.sh

Quick summary
- Install the plugin into a project's plugin directory (.worklog/plugins/) so the wl CLI will discover it.
- Commands added: `wl ampa start`, `wl ampa stop`, `wl ampa status`.

Installation
1. From the repository root run:

   ./plugins/install-worklog-plugin.sh plugins/wl_ampa/ampa.mjs

   This copies the plugin into `.worklog/plugins/ampa.mjs`. The installer will also copy itself into `.worklog/plugins/` when used with the default target for convenience.

2. Alternatively, copy the file manually into your project:

   mkdir -p .worklog/plugins
   cp plugins/wl_ampa/ampa.mjs .worklog/plugins/ampa.mjs

How wl discovers plugins
- wl loads plain ESM modules (`.js` or `.mjs`) located under the configured plugin directory (default `.worklog/plugins/`). Each plugin must default-export a `register(ctx)` function; the loader will call it and the plugin should register commands on `ctx.program`.

Usage
- Start a daemon (detached by default):

  wl ampa start --name mydaemon --cmd "node ./scripts/dev-server.js"

- Start in foreground (no detach):

  wl ampa start --name mydaemon --cmd "node ./scripts/dev-server.js" --foreground

- Stop a daemon:

  wl ampa stop --name mydaemon

- Check status:

  wl ampa status --name mydaemon

Configuration and command resolution
- Command resolution priority (highest → lowest):
  1. CLI `--cmd` argument
  2. Environment variable `WL_AMPA_CMD`
  3. `worklog.json` field `ampa` (string or array)
  4. `package.json` script `scripts.ampa`
  5. Executable `./scripts/ampa` or `./scripts/daemon` in project root

Files produced
- PID file: `.worklog/ampa/<name>/<name>.pid`
- Log file: `.worklog/ampa/<name>/<name>.log`

Behavior and signals
- On Unix, the plugin launches daemons detached and attempts to signal the process group (negative PID) for graceful shutdown (SIGTERM) and escalation (SIGKILL) if necessary.
- On Windows the negative-PID process group semantics are not available; the plugin falls back to sending signals to the single PID. Windows behavior is therefore more limited; daemons should handle termination gracefully.

Testing
- Node lifecycle test: run the Node test added to the repo:

  node --test tests/node/test-ampa.mjs

CI integration
- Ensure the plugin is present under `.worklog/plugins/` in CI before invoking wl or running the verification tests. Example CI step (shell):

  mkdir -p .worklog/plugins
  cp plugins/wl_ampa/ampa.mjs .worklog/plugins/ampa.mjs
  node --test tests/node/test-ampa.mjs

Security
- Plugins run with the same permissions as the `wl` process. Review plugin code before installing into your environment. Avoid installing third-party plugins you do not trust.

Notes and limitations
- Windows support is partial; signal semantics differ and may require wrapper scripts on Windows systems.

Contributing
- Improvements, bug fixes and tests are welcome. Please open a PR against this branch and reference work item SA-0MLB8IQL60QZI3F0.
