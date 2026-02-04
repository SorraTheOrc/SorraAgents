Container image and run instructions (Podman-compatible)

Build:

  podman build -t ampa-daemon:local .

Run (example):

  podman run --rm -e AMPA_DISCORD_WEBHOOK="https://hooks.example" -p 8080:8080 ampa-daemon:local

Notes:
- Image runs as non-root `appuser` created in the image.
- Logs are emitted to stdout/stderr by the daemon process.
