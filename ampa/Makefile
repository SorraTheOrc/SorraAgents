.PHONY: build run stop lint

build:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; if command -v podman >/dev/null 2>&1; then podman build --no-cache --isolation chroot -t ampa-daemon:local .; else docker build --no-cache -t ampa-daemon:local .; fi



run:
	@UID=$$(id -u); [ -n "$$XDG_RUNTIME_DIR" ] && [ -d "$$XDG_RUNTIME_DIR" ] || { export XDG_RUNTIME_DIR="/tmp/run-user-$$UID"; mkdir -p "$$XDG_RUNTIME_DIR"; chmod 700 "$$XDG_RUNTIME_DIR"; }; ENVFILE=; [ -f .env ] && ENVFILE="--env-file .env"; CONTAINER_NAME=ampa-daemon-local; \
	# Use helper script to avoid Makefile quoting issues
	./scripts/run.sh
	  if command -v ss >/dev/null 2>&1; then
	    ss -ltn | grep -q ":8080" && { echo "Port 8080 already in use; run \"make stop\" to stop it."; exit 1; }
	  elif command -v lsof >/dev/null 2>&1; then
	    lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1 && { echo "Port 8080 already in use; run \"make stop\" to stop it."; exit 1; }
	  else
	    if command -v docker >/dev/null 2>&1 && docker ps --format "{{.ID}} {{.Ports}}" | grep -q ":8080"; then
	      echo "Port 8080 already in use; run \"make stop\" to stop it."; exit 1
	    fi
	    if command -v podman >/dev/null 2>&1 && podman ps --format "{{.Ports}}" | grep -q ":8080"; then
	      echo "Port 8080 already in use; run \"make stop\" to stop it."; exit 1
	    fi
	  fi
	  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
	    docker run -d --name "$$CONTAINER_NAME" $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local
	  elif command -v podman >/dev/null 2>&1; then
	    podman run -d --name "$$CONTAINER_NAME" $$ENVFILE --rm -e AMPA_DISCORD_WEBHOOK="$$AMPA_DISCORD_WEBHOOK" -p 8080:8080 ampa-daemon:local
	  else
	    echo "Either a working Docker daemon or Podman is required to run the container"; exit 1
	  fi
	'




stop:
	@CONTAINER_NAME=ampa-daemon-local; \
	if command -v podman >/dev/null 2>&1; then \
		CID=$$(podman ps -q --filter name=$$CONTAINER_NAME); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID)"; podman stop $$CID >/dev/null 2>&1 || true; podman rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(podman ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $$1}'); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do podman stop $$id >/dev/null 2>&1 || true; podman rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	elif command -v docker >/dev/null 2>&1; then \
		CID=$$(docker ps -q --filter name=$$CONTAINER_NAME); \
		if [ -n "$$CID" ]; then \
			echo "Stopping container $$CONTAINER_NAME ($$CID)"; docker stop $$CID >/dev/null 2>&1 || true; docker rm $$CID >/dev/null 2>&1 || true; echo "Stopped."; exit 0; \
		fi; \
		PIDS=$$(docker ps --format '{{.ID}} {{.Ports}}' | awk '/:8080/{print $$1}'); \
		if [ -n "$$PIDS" ]; then for id in $$PIDS; do docker stop $$id >/dev/null 2>&1 || true; docker rm $$id >/dev/null 2>&1 || true; done; echo "Stopped."; exit 0; fi; \
	else \
		echo "Neither podman nor docker is available to stop containers"; exit 1; \
	fi; \
	echo "No running ampa container found and nothing publishing port 8080."; exit 0
