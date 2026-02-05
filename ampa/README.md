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

See `ampa/scheduler_schema.md` for the command field schema, store layout, and tuning guidance.

Configuration via .env

Place a `.env` file in the `ampa/` directory (it is ignored by git by default). The daemon will load `ampa/.env` if present and values there override environment variables.

Example `ampa/.env` contents:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/...."
AMPA_HEARTBEAT_MINUTES=1

Environment variables are used if `.env` is not present. The daemon prefers values from the `.env` file when available.

Installing dependencies (if running locally)

Add the runtime dependencies and install them in your environment:

```sh
 pip install -r ampa/requirements.txt
```

Run as a daemon

   # Run in the foreground (use system tools to daemonize if needed)
   AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.scheduler
