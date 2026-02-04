AMPA Core Heartbeat Sender

Run:

AMPA_DISCORD_WEBHOOK must be set. Example:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon

Configuration via .env

Place a `.env` file in the `ampa/` directory (it is ignored by git by default). The daemon will load `ampa/.env` if present and values there override environment variables.

Example `ampa/.env` contents:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/...."
AMPA_HEARTBEAT_MINUTES=1

Environment variables are used if `.env` is not present. The daemon prefers values from the `.env` file when available.

Installing dependencies

Add the runtime dependencies and install them in your environment:

```sh
pip install -r requirements.txt
```

`python-dotenv` enables loading of `ampa/.env`.
