# OpenCode — Workflow & Skills Repository

A lightweight collection of workflow guides, command patterns, and skill templates for building and operating small automation agents.

## Purpose
- Centralize documentation and reusable "skills" for agent development and operational workflows.
- Provide templates and checklists to guide feature implementation, testing, and release.

## Repository structure
- agent/: workflow and agent-focused reference guides (e.g., [agent/forge.md](agent/forge.md)).
- command/: design, intake, implementation and review process documents (see [command/implement.md](command/implement.md)).
- skill/: skill templates and utilities to scaffold and package agent skills (see [skill/skill-creator/SKILL.md](skill/skill-creator/SKILL.md)).
- Workflow.md: high-level workflow for using this repository.
- package.json: basic metadata used by tooling.

## Prerequisites

The dev container commands (`wl ampa start-work`, `finish-work`, `list-containers`) require the following tools on the host:

- **Podman** — container runtime (rootless mode)
  - Install: https://podman.io/getting-started/installation
- **Distrobox** — manages dev containers on top of Podman
  - Install: `curl -s https://raw.githubusercontent.com/89luca89/distrobox/main/install | sudo sh`
  - Alternative methods: https://github.com/89luca89/distrobox?tab=readme-ov-file#installation
- **Git** and **wl** (Worklog CLI) — assumed to already be available

Verify the installations:

```sh
command -v podman && podman --version
command -v distrobox && distrobox version
```

### Podman runtime directory error

If `podman --version` prints an error like:

```
ERRO[0000] stat /run/user/1000/: no such file or directory
```

the XDG runtime directory expected by rootless Podman does not exist. This typically happens in environments that were not started via a full login session (containers, SSH without `pam_systemd`, WSL, etc.). Create it manually:

```sh
sudo mkdir -p /run/user/$(id -u) && sudo chown $(id -u):$(id -g) /run/user/$(id -u) && sudo chmod 700 /run/user/$(id -u)
```

After that, `podman --version` should work without errors.

### Pre-warming the container pool

The dev container workflow uses a pool of pre-warmed containers so that `wl ampa start-work` can claim one instantly instead of waiting for a slow clone. After installing the prerequisites, set everything up with a single command:

```sh
wl ampa warm-pool
```

This will:

1. **Build the container image** (`ampa-dev:latest`) from `ampa/Containerfile` if it does not already exist
2. **Create the template container** (`ampa-template`) via Distrobox and run its one-off host-integration init (this is the slowest step on first run)
3. **Fill the pool** with 3 pre-warmed containers cloned from the template

The pool is replenished automatically in the background after each `start-work`, but running `warm-pool` once up front avoids the initial wait.

If the `ampa/Containerfile` has been modified since the image was last built, `warm-pool` will automatically tear down unclaimed pool containers and the template, rebuild the image, and re-fill the pool. Simply run `wl ampa warm-pool` again — no manual cleanup is needed.

## Getting started
1. Read the main workflow: [Workflow.md](Workflow.md).
2. Pick a folder to work in (e.g., `skill/` or `agent/`).
3. Follow the appropriate guide (see files inside each folder) to implement, test, and package your work.

Daemon / scheduler note

- Some packages in this repository provide a long-running "daemon" that can
  either perform a one-off action or run a scheduler loop. By default those
  daemons often send a single heartbeat and exit; to run the scheduler loop
  you must explicitly enable it (for example: use `--start-scheduler` or set
  an environment flag like `AMPA_RUN_SCHEDULER=1`). Check the package README
  (for example `ampa/README.md`) for the exact flag and environment variables.

## Contributing
- Open an issue describing the change you'd like to make.
- Follow the relevant guide under `command/` for design and review steps.
- If adding a new skill, consider using the scripts in `skill/skill-creator/scripts` to scaffold and package it.

### AMPA plugin development

The canonical source for the AMPA Worklog plugin is:

```
skill/install-ampa/resources/ampa.mjs
```

**Do not create copies in other directories** (e.g. `plugins/`, `.worklog/plugins/`). The installer (`skill/install-ampa/scripts/install-worklog-plugin.sh`) deploys the canonical source into `.worklog/plugins/ampa.mjs` at install time. To develop or modify the plugin:

1. Edit `skill/install-ampa/resources/ampa.mjs` directly.
2. Run `node --test tests/node/test-ampa.mjs tests/node/test-ampa-devcontainer.mjs` to verify.
3. Re-install with `skill/install-ampa/scripts/install-worklog-plugin.sh --yes` to deploy changes locally.

## Next steps / Suggestions
- Add a CI workflow to validate new skills and docs.
- Add example usage for each skill in `skill/` to make onboarding easier.

## License
See individual files for licenses. Some folders include a LICENSE.txt (for example: [skill/skill-creator/LICENSE.txt](skill/skill-creator/LICENSE.txt)).

---
If you'd like, I can commit this file, add a short changelog entry, or expand any section into more detailed docs.
