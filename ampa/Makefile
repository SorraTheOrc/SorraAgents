.PHONY: build run stop lint

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --no-cache --isolation chroot -t ampa-daemon:local .; else docker build --no-cache -t ampa-daemon:local .; fi




run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; ENVFILE=; [ -f .env ] && ENVFILE="--env-file .env"; ./scripts/run.sh




stop:
	@CONTAINER_NAME=ampa-daemon-local; \
	# Try docker (non-sudo)
	if command -v docker >/dev/null 2>&1; then \
		CID=$$(docker ps -q --filter name=$$CONTAINER_NAME 2>/dev/null || true); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID)"; docker stop $$CID >/dev/null 2>&1 || true; docker rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(docker ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | awk '/:8080/{print $$1}' || true); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do docker stop $$id >/dev/null 2>&1 || true; docker rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	fi; \
	# Try podman (non-sudo)
	if command -v podman >/dev/null 2>&1; then \
		CID=$$(podman ps -q --filter name=$$CONTAINER_NAME 2>/dev/null || true); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID)"; podman stop $$CID >/dev/null 2>&1 || true; podman rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(podman ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | awk '/:8080/{print $$1}' || true); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do podman stop $$id >/dev/null 2>&1 || true; podman rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	fi; \
	# Try docker/podman with sudo (for root-owned containers)
	if command -v docker >/dev/null 2>&1; then \
		CID=$$(sudo docker ps -q --filter name=$$CONTAINER_NAME 2>/dev/null || true); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID) with sudo"; sudo docker stop $$CID >/dev/null 2>&1 || true; sudo docker rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(sudo docker ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | awk '/:8080/{print $$1}' || true); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do sudo docker stop $$id >/dev/null 2>&1 || true; sudo docker rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	fi; \
	if command -v podman >/dev/null 2>&1; then \
		CID=$$(sudo podman ps -q --filter name=$$CONTAINER_NAME 2>/dev/null || true); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID) with sudo"; sudo podman stop $$CID >/dev/null 2>&1 || true; sudo podman rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(sudo podman ps --format '{{.ID}} {{.Ports}}' 2>/dev/null | awk '/:8080/{print $$1}' || true); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do sudo podman stop $$id >/dev/null 2>&1 || true; sudo podman rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	fi; \
	echo "No running ampa container found and nothing publishing port 8080."; exit 0
