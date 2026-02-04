.PHONY: build run lint

build:
	podman build -t ampa-daemon:local .

run:
	podman run --rm -e AMPA_DISCORD_WEBHOOK="$${AMPA_DISCORD_WEBHOOK}" -p 8080:8080 ampa-daemon:local
