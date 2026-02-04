.PHONY: build run lint

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --no-cache --isolation chroot -t ampa-daemon:local .; else docker build --no-cache -t ampa-daemon:local .; fi

run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; \
	ENVFILE=; [ -f .env ] && ENVFILE="--env-file .env"; \
	if command -v podman >/dev/null 2>&1; then podman run $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; else docker run $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; fi
