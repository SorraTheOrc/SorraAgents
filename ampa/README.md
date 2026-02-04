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

Quick test (single heartbeat)

  # Using curl (simple JSON payload)
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" \
    curl -H "Content-Type: application/json" \
    -d '{"content":"AMPA heartbeat test from $(hostname)"}' "$${AMPA_DISCORD_WEBHOOK}"

  # Or a Python one-liner that sends a single heartbeat (requires requests):
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python - <<'PY'
  import os,requests,socket,datetime
  w=os.environ.get('AMPA_DISCORD_WEBHOOK')
  if not w:
      raise SystemExit('set AMPA_DISCORD_WEBHOOK')
  payload={'content': f"heartbeat host={socket.gethostname()} time={datetime.datetime.now().isoformat()}"}
  r=requests.post(w, json=payload)
  print('status', r.status_code)
  PY

Run as a daemon

  # Run in the foreground (use system tools to daemonize if needed)
  AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" python -m ampa.daemon

  # Run as a background container (systemd example shown below)
  make -C APMA build
  podman run -d --name ampa-daemon --restart=always \
    -e AMPA_DISCORD_WEBHOOK="https://discord.com/api/webhooks/XXX" -p 8080:8080 ampa-daemon:local

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
