# Worklog AMPA Plugin

This plugin adds a `wl ampa` command with `start`, `stop`, `status`, and `run` subcommands.
It manages daemon PID/log files under `.worklog/ampa/<name>.pid` and `.worklog/ampa/<name>.log`.

## Install

Run the installer from the repo root:

```
plugins/install-worklog-plugin.sh
```

By default it installs `plugins/wl_ampa/ampa.mjs` to `.worklog/plugins/` and bundles
the Python `ampa` package (if present) into `.worklog/plugins/ampa_py` for fallback
execution.

## Usage

```
wl ampa start [--cmd <cmd>] [--name <name>] [--foreground]
wl ampa stop [--name <name>]
wl ampa status [--name <name>]
wl ampa run <command-id>
```

If no command is supplied, the plugin resolves it in this order:

1. `--cmd` argument
2. `WL_AMPA_CMD` environment variable
3. `worklog.json` "ampa" entry
4. `package.json` script `ampa`
5. `scripts/ampa` or `scripts/daemon` in the project
6. Bundled Python package under `.worklog/plugins/ampa_py` (runs `python -m ampa.daemon`)
