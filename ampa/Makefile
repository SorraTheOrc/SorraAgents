.PHONY: build run stop lint

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --no-cache --isolation chroot -t ampa-daemon:local .; else docker build --no-cache -t ampa-daemon:local .; fi


run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; \
	ENVFILE=; [ -f .env ] && ENVFILE="--env-file .env"; \
	CONTAINER_NAME=ampa-daemon-local; \
	if command -v podman >/dev/null 2>&1; then RUNTIME=podman; else RUNTIME=docker; fi; \
	# If some container is already listening on the host port 8080, instruct the user to stop it first.
	if $$RUNTIME ps --format '{{.ID}} {{.Names}} {{.Ports}}' | grep -q ':8080'; then \
		echo "Port 8080 already in use by a running container; run 'make stop' to stop it first."; exit 1; \
	fi; \
	# Run the container with a stable name so it can be stopped with 'make stop'
	$$RUNTIME run --name $$CONTAINER_NAME $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local


stop:
	@# Stop and remove the running ampa container if present; fall back to stopping any container
	@# that is publishing host port 8080.
	if command -v podman >/dev/null 2>&1; then RUNTIME=podman; else RUNTIME=docker; fi; \
	CONTAINER_NAME=ampa-daemon-local; \
	CID="$$($$RUNTIME ps -q --filter name=$$CONTAINER_NAME)"; \
	if [ -n "$$CID" ]; then \
		echo "Stopping container $$CONTAINER_NAME ($$CID)"; $$RUNTIME stop $$CID >/dev/null 2>&1 || true; $$RUNTIME rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
	fi; \
	# No container with our name found — look for any container exposing port 8080 and stop it
	PIDS="$$($$RUNTIME ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $$1}')"; \
	if [ -n "$$PIDS" ]; then \
		echo "Stopping container(s) publishing port 8080: $$PIDS"; for id in $$PIDS; do $$RUNTIME stop $$id >/dev/null 2>&1 || true; $$RUNTIME rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; \
	fi; \
	echo "No running ampa container found and nothing publishing port 8080."; exit 0
