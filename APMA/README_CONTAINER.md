Container image and run instructions (Podman-compatible)

Build (recommended via Makefile):

  # from repo root
  make -C APMA build

  # or from inside the APMA directory
  cd APMA && make build

Run (recommended via Makefile):

  make -C APMA run

Notes:
- The Makefile wraps podman/docker and will create a temporary XDG_RUNTIME_DIR
  when needed so rootless podman works in headless/CI environments.
- Image runs as non-root `appuser` created in the image.
- Logs are emitted to stdout/stderr by the daemon process.
