.PHONY: build run lint

# Build the container image. If XDG_RUNTIME_DIR is missing (common in CI or
# headless environments), create a temporary runtime dir under /tmp for the
# duration of the build to allow rootless podman to function. Prefer podman
# when available, fallback to docker if not installed.

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --isolation chroot -t ampa-daemon:local .; else docker build -t ampa-daemon:local .; fi

# Run the container image. This target also ensures XDG_RUNTIME_DIR is
# available for podman in rootless environments.

run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman run --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; else docker run --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; fi
