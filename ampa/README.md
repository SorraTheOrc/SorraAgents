AMPA Core Heartbeat Sender

Run locally:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon

Scheduler

The scheduler runs periodic commands with a normalized-lateness algorithm and stores state on disk.

Run locally:

  python -m ampa.scheduler

Example scheduler store:

  python -m ampa.scheduler

Key config knobs (env):

- AMPA_SCHEDULER_STORE: path to the JSON scheduler store
- AMPA_SCHEDULER_POLL_INTERVAL_SECONDS: poll interval in seconds
- AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS: minimum gap between command starts
- AMPA_SCHEDULER_PRIORITY_WEIGHT: priority multiplier weight
- AMPA_LLM_HEALTHCHECK_URL: LLM availability probe URL
- AMPA_SCHEDULER_MAX_RUN_HISTORY: number of run history entries to keep
 - AMPA_VERIFY_PR_WITH_GH: when set to 1/true, enable verification of GitHub PR merge status
   via the `gh` CLI before auto-completing work items. Defaults to enabled when not set;
   per-command metadata `verify_pr_with_gh` can override this behavior.

See `ampa/scheduler_schema.md` for the command field schema, store layout, and tuning guidance.

Configuration via .env

Place a `.env` file in the `ampa/` directory (it is ignored by git by default). The daemon will load `ampa/.env` if present and values there override environment variables.

Example `ampa/.env` contents:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/...."
AMPA_HEARTBEAT_MINUTES=1
AMPA_VERIFY_PR_WITH_GH=1

Environment variables are used if `.env` is not present. The daemon prefers values from the `.env` file when available.

Installing dependencies (if running locally)

Add the runtime dependencies and install them in your environment:

```sh
 pip install -r ampa/requirements.txt
```

Run as a daemon

The daemon defaults to sending a single heartbeat and exiting. To run the
scheduler loop under the daemon runtime you must explicitly enable it either
with the `--start-scheduler` flag or the `AMPA_RUN_SCHEDULER` environment
variable.

Examples:

  # Run daemon in the foreground and start the scheduler loop (recommended for testing)
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon --start-scheduler

  # Enable scheduler via environment variable instead of the CLI flag
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" AMPA_RUN_SCHEDULER=1 python -m ampa.daemon

  # Send a single heartbeat and exit
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon --once

Notes:

- The scheduler uses the current working directory as the `command_cwd` for
  commands it runs, so start the daemon from the directory you want commands
  to execute in.
- Ensure `AMPA_DISCORD_WEBHOOK` is set (or `ampa/.env` is present) before
  starting the daemon; missing webhook will cause the daemon to exit.
- Install runtime dependencies when running locally:

  pip install -r ampa/requirements.txt

Observability
-------------

AMPA exposes a lightweight observability surface intended for scraping by
Prometheus-compatible systems. The package provides a combined `/metrics` and
`/health` HTTP endpoint. By default these endpoints are served on port `8000`.

Environment variables:

- `AMPA_DISCORD_WEBHOOK` (required by the daemon): when unset or empty the
  `/health` endpoint returns `503 Service Unavailable` to indicate fatal
  misconfiguration.
- `AMPA_METRICS_PORT` (optional): port to serve `/metrics` and `/health` on
  (defaults to `8000`).

Metrics exported:

- `ampa_heartbeat_sent_total` (counter) — number of successful heartbeat sends
- `ampa_heartbeat_failure_total` (counter) — number of failed heartbeat sends
- `ampa_last_heartbeat_timestamp_seconds` (gauge) — epoch seconds of last
  successful heartbeat

Quick manual test

1. Install dev dependencies: `pip install -r ampa/requirements.txt`
2. Run the metrics server in Python REPL or a tiny script:

```python
from ampa.metrics import start_metrics_server
start_metrics_server(port=8000)
```

3. Verify health: `curl -sSf http://127.0.0.1:8000/health` (returns HTTP 200 when
   `AMPA_DISCORD_WEBHOOK` is set).
4. Verify metrics: `curl http://127.0.0.1:8000/metrics | grep ampa_heartbeat`

Integration test example (pytest)

The repository includes integration tests that start the server on an ephemeral
port. Run them with `pytest -q` and confirm the new `tests/test_metrics_and_health.py`
passes.

Scheduler admin CLI

  Use the scheduler CLI for admin tasks (listing, adding, updating commands):

    python -m ampa.scheduler list

  Run a command immediately by id:

    python -m ampa.scheduler run-once <command-id>

Live delegation

  Delegation runs as part of triage-audit and only when `audit_only` is false.
  It also requires no in-progress work items. When idle, it selects the top
  `wl next` candidate and dispatches the appropriate workflow:

  - stage `idea`: runs `/intake <id>`
  - stage `intake_complete`: runs `/plan <id>`
  - stage `plan_complete`: runs `work on <id> using the implement skill`


Delegation report

Generate a report listing in-progress items, candidates from `wl next`,
and the top candidate with rationale. This command also runs idle delegation
when the system has no in-progress items. Set `audit_only` metadata to true
to skip dispatch.

  python -m ampa.scheduler delegation

Send the report to Discord (requires `AMPA_DISCORD_WEBHOOK`):

  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.scheduler delegation --discord

Candidate selection

The candidate selection service calls `wl next --json` and returns the top
candidate from that response.
