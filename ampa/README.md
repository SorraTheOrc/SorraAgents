AMPA Core Heartbeat Sender

Run locally:

  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon

Run in container (preferred for reproducible runs):

  # Build and run using the repository Makefile
  make -C APMA build
  make -C APMA run

  # Or manually from APMA/
  cd APMA && podman build -t ampa-daemon:local .
  podman run --rm -e AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" -p 8080:8080 ampa-daemon:local

Configuration via .env

Place a `.env` file in the `ampa/` directory (it is ignored by git by default). The daemon will load `ampa/.env` if present and values there override environment variables.

Example `ampa/.env` contents:

AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/...."
AMPA_HEARTBEAT_MINUTES=1

Environment variables are used if `.env` is not present. The daemon prefers values from the `.env` file when available.

Installing dependencies (if running locally)

Add the runtime dependencies and install them in your environment:

```sh
pip install -r APMA/requirements.txt
```

`python-dotenv` enables loading of `ampa/.env`.
