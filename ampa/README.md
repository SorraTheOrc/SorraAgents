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

Run as a daemon

  # Run in the foreground (use system tools to daemonize if needed)
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon

  # Run as a background container (systemd example shown below)
  make -C APMA build
  podman run -d --name ampa-daemon --restart=always \
    -e AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" -p 8080:8080 ampa-daemon:local

Injecting `.env` into the container

  # Mount the ampa/.env into the container so the process reads the same
  # environment variables as when run locally. This avoids passing many -e
  # flags and keeps secrets out of VCS.
  cd APMA
  podman run --rm --env-file ../ampa/.env -p 8080:8080 ampa-daemon:local

  # Example systemd unit (save as /etc/systemd/system/ampa.service):
  # [Unit]
  # Description=AMPA Heartbeat Daemon
  # After=network.target
  #
  # [Service]
  # ExecStart=/usr/bin/podman run --name ampa-daemon --rm \
  #   -e AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" -p 8080:8080 ampa-daemon:local
  # Restart=always
  #
  # [Install]
  # WantedBy=multi-user.target
