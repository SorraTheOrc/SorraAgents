.PHONY: build run stop lint

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --no-cache --isolation chroot -t ampa-daemon:local .; else docker build --no-cache -t ampa-daemon:local .; fi



run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; \
	ENVFILE=; [ -f .env ] && ENVFILE="--env-file .env"; \
	CONTAINER_NAME=ampa-daemon-local; \
	# Check whether port 8080 is free by attempting to bind to it with Python.
	python3 - <<'PY'
import socket,sys
s=socket.socket()
try:
    s.bind(('127.0.0.1',8080))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
	if [ $$? -ne 0 ]; then \
		echo "Port 8080 already in use; run 'make stop' to stop the container that is using it."; exit 1; \
	fi; \
	if command -v podman >/dev/null 2>&1; then \
		podman run --name $$CONTAINER_NAME $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; \
	elif command -v docker >/dev/null 2>&1; then \
		docker run --name $$CONTAINER_NAME $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local; \
	else \
		echo "Either podman or docker is required to run the container"; exit 1; \
	fi



stop:
	@# Stop and remove the running ampa container if present; fall back to stopping any container
	@# that is publishing host port 8080.
	CONTAINER_NAME=ampa-daemon-local; \
	if command -v podman >/dev/null 2>&1; then \
		if [ -n "$$(podman ps -q --filter name=$$CONTAINER_NAME)" ]; then \
			CID=$$(podman ps -q --filter name=$$CONTAINER_NAME); echo "Stopping container $$CONTAINER_NAME ($$CID)"; podman stop $$CID >/dev/null 2>&1 || true; podman rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		# look for container exposing port 8080
		PIDS=$$(podman ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $$1}'); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do podman stop $$id >/dev/null 2>&1 || true; podman rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	elif command -v docker >/dev/null 2>&1; then \
		if [ -n "$$(docker ps -q --filter name=$$CONTAINER_NAME)" ]; then \
			CID=$$(docker ps -q --filter name=$$CONTAINER_NAME); echo "Stopping container $$CONTAINER_NAME ($$CID)"; docker stop $$CID >/dev/null 2>&1 || true; docker rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(docker ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $$1}'); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do docker stop $$id >/dev/null 2>&1 || true; docker rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	else \
		echo "Neither podman nor docker is available to stop containers"; exit 1; \
	fi; \
	echo "No running ampa container found and nothing publishing port 8080."; exit 0
