Container image and run instructions (Podman-compatible)

Build (recommended via Makefile):

  # from repo root
  make -C APMA build

  # or from inside the APMA directory
  cd APMA && make build

Run (recommended via Makefile):

  make -C APMA run

Example manual commands (if you prefer to run without Make):

  # Build with podman (ensure XDG_RUNTIME_DIR exists when running rootless)
  cd APMA && podman build -t ampa-daemon:local .

  # Run with a non-root numeric UID (container is configured to run as UID 1000)
  podman run --rm -e AMPA_DISCORD_WEBHOOK="https://hooks.example" -p 8080:8080 ampa-daemon:local

  # Or run as your user (map host UID to container runtime uid)
  podman run --rm --user $$(id -u):$$(id -g) -e AMPA_DISCORD_WEBHOOK="https://hooks.example" -p 8080:8080 ampa-daemon:local

Notes:
- The Makefile wraps podman/docker and will create a temporary XDG_RUNTIME_DIR
  when needed so rootless podman works in headless/CI environments.
- The image files are owned by UID 1000 and the container runs as numeric UID 1000
  by default to remain compatible with rootless builders. You can override the
  runtime user with `--user` if you need a different mapping.
- Logs are emitted to stdout/stderr by the daemon process.
