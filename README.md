# Workflow & Skills Repository

A lightweight collection of workflow guides, command patterns, and skill templates for building and operating small automation agents.

## Purpose

- Centralize documentation and reusable "skills" for agent development and operational workflows.
- Provide templates and checklists to guide feature implementation, testing, and release.

## Terminology Policy

"Acceptance Criteria" is the canonical term for work-item requirements. "Success Criteria" is an accepted synonym and may be used interchangeably. Both terms are recognized by validation logic in the workflow invariants (`docs/workflow/workflow.json`, `docs/workflow/workflow.yaml`). When defining work items, prefer the heading **Acceptance Criteria** (synonym: Success Criteria) for consistency.

## Repository structure

- agent/: workflow and agent-focused reference guides (e.g., [agent/forge.md](agent/forge.md), [agent/ship.md](agent/ship.md)).
- command/: design, intake, implementation and review process documents (see [command/implement.md](command/implement.md)).
- skill/: skill templates and utilities to scaffold and package agent skills (see [skill/skill-creator/SKILL.md](skill/skill-creator/SKILL.md)).
  - [skill/skills-script-paths.md](skill/skills-script-paths.md): Best practices for referencing scripts and assets from skills.
- plugins/: local agent framework plugins used by this repository.
- docs/dev/: development and release process documentation ([release-process.md](docs/dev/release-process.md), [release-tests.md](docs/dev/release-tests.md)).
- Workflow.md: high-level workflow for using this repository.
- package.json: basic metadata used by tooling.

## Prerequisites

The dev container commands (`wl ampa start-work`, `finish-work`, `list-containers`) require the following tools on the host:

- **Podman** — container runtime (rootless mode)
  - Install: <https://podman.io/getting-started/installation>
- **Distrobox** — manages dev containers on top of Podman
  - Install: `curl -s https://raw.githubusercontent.com/89luca89/distrobox/main/install | sudo sh`
  - Alternative methods: <https://github.com/89luca89/distrobox?tab=readme-ov-file#installation>
- **Git** and **wl** (Worklog CLI) — assumed to already be available

Verify the installations:

```sh
command -v podman && podman --version
command -v distrobox && distrobox version
```

### Code Quality Linters (Optional)

The automated code quality review feature supports the following linters. Install them to enable quality scanning during audits:

| Language | Linter | Install command |
|----------|--------|-----------------|
| Python | [ruff](https://docs.astral.sh/ruff/) | `pip install ruff` |
| TypeScript/JavaScript | [ESLint](https://eslint.org/) | `npm install -g eslint` |
| Markdown | [markdownlint-cli](https://github.com/igmpaul/markdownlint-cli) | `npm install -g markdownlint-cli` |
| Shell | [ShellCheck](https://shellcheck.net/) | `apt install shellcheck` or `brew install shellcheck` |
| C# | [dotnet-format](https://github.com/dotnet/format) | Install [.NET SDK](https://dotnet.microsoft.com/download) |

If a linter is not available, the code quality check skips that language gracefully without errors.

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

1. **Build the container image** (`ampa-dev:latest`) from the AMPA repository's Containerfile if it does not already exist
2. **Create the template container** (`ampa-template`) via Distrobox and run its one-off host-integration init (this is the slowest step on first run)
3. **Fill the pool** with 3 pre-warmed containers cloned from the template

The pool is replenished automatically in the background after each `start-work`, but running `warm-pool` once up front avoids the initial wait.

Pool state (`pool-state.json`, `pool-cleanup.json`, `pool-replenish.log`) is stored globally at `~/.config/pi/.worklog/ampa/` so that container claims and cleanup records are shared across all projects on the host. Per-project config (`.env`, `scheduler_store.json`, daemon PID/log) remains under `<project>/.worklog/ampa/`.

If the AMPA Containerfile has been modified since the image was last built, `warm-pool` will automatically tear down unclaimed pool containers and the template, rebuild the image, and re-fill the pool. Simply run `wl ampa warm-pool` again — no manual cleanup is needed.

See the AMPA container pool reference for full details.

### Browser test capability

The AMPA development image may include pinned Playwright and the Chromium browser runtime to support running browser smoke tests inside claimed containers. When present:

- The pinned Playwright version is recorded in the AMPA repository's Containerfile via an `ARG PLAYWRIGHT_VERSION` comment.
- To run the repository's browser smoke test inside a claimed container:

  ```sh
  wl ampa start-work <work-item-id>
  # inside the container
  cd /workdir/project
  npm ci --include=dev
  node --test tests/node/test-browser-smoke.mjs
  ```

Note: the container includes the browser runtime but not your project's node_modules; install dev dependencies inside the container before running tests.

### Installer: automatic post-install warm-pool

The installer for the AMPA Worklog plugin will attempt to pre-warm the container pool automatically when it detects the required host tooling (`podman` and `distrobox`) and when the install is running non-interactively or with `--yes`.

- Configure the target pool size with the `WL_AMPA_POOL_SIZE` environment variable or pass `--pool-size <n>` to the installer script (`skill/install-ampa/scripts/install-worklog-plugin.sh --pool-size 5`).
- The warm-pool CLI also accepts `--size <n>` to override the configured pool size for a single invocation: `wl ampa warm-pool --non-interactive --size 4`.
- The warm-pool CLI supports `--non-interactive` so installers can run it without prompts.
- The installer skips the automatic warm-pool when running in CI (when `CI=true` is set) so CI jobs are not delayed by long-running container initialization.

If the installer cannot find `podman` or `distrobox`, it will continue installation but print one-line actionable guidance explaining how to install the missing tools and how to run `wl ampa warm-pool` manually. Warm-pool failures are treated as non-fatal by the installer; it records the decision and prints short guidance and where the captured output is stored.

Installer output capture and logs:

- When the installer runs warm-pool it captures stdout/stderr to `/tmp/ampa_warm_pool.out` and `/tmp/ampa_warm_pool.err` and prints a short snippet on completion or a one-line actionable error on failure.

Examples:

```sh
# set default pool size for this install
WL_AMPA_POOL_SIZE=4 skill/install-ampa/scripts/install-worklog-plugin.sh --yes

# run warm-pool manually (non-interactive, size 3)
wl ampa warm-pool --non-interactive --size 3
```

## Release Process

Promoting changes from `dev` to `main` requires a human-reviewed merge.
See the full [release process documentation](docs/dev/release-process.md) for
the checklist and role definition.

### For Release Managers

The canonical release script lives under the ship skill at
`skill/ship/scripts/release/merge-dev-to-main.sh` and is invoked via
`node skill/ship/scripts/run-release.js` (see [skill/ship/SKILL.md](skill/ship/SKILL.md)).

```sh
# Run a dry-run to preview the merge
node skill/ship/scripts/run-release.js --dry-run

# Execute the merge
node skill/ship/scripts/run-release.js
```

Before merging, the release process runs local gates (worklog refs, audit
readiness, critical items, producer review). Tests are run locally before
release; there is no CI pipeline. After the gates pass, the script merges
`dev` into `main`, pushes the result, and records an audit comment in the
worklog.

While a release runs, the ship skill sets a **Code Freeze marker** at
`.worklog/code-freeze.json` (contract WL-0MSBU4KMA004PKSR). The marker is
written before the gating checks and cleared on every exit path (success,
failure, abort, dry-run) via `try/finally` in `run-release.js` and an `EXIT`
trap in `merge-dev-to-main.sh`. While the marker is present, `implement.py`
refuses to start new implementation work (no `--force` bypass; fail-open on
missing/corrupt marker). A stale marker from a crashed release can be removed
manually by deleting `.worklog/code-freeze.json`.

## Getting started

1. Read the main workflow: [Workflow.md](Workflow.md).
2. Pick a folder to work in (e.g., `skill/` or `agent/`).
3. Follow the appropriate guide (see files inside each folder) to implement, test, and package your work.

## PR-based audit flow (Pi)

The audit helper supports PR mode in addition to work-item mode:

- Input can be a WL id (`SA-...`) or GitHub PR reference (`https://github.com/<owner>/<repo>/pull/<n>` or `<owner>/<repo>#<n>`).
- In PR mode, the helper resolves the related WL item from PR title/body (or uses explicit `--wl-id`, or optionally `--allow-create-wl`).
- The helper can prepare an ephemeral checkout, run autodetected build/tests, run audit via `pi -p --mode json "/audit <wl-id>"` (non-interactive, JSON-stream mode), and record audit text using `wl update --audit-text`.
- If build/tests and audit pass, it can present a merge offer and only merges when explicitly confirmed.

Daemon / scheduler note

- The AMPA Worklog plugin provides a long-running "daemon" that can either
  perform a one-off action or run a scheduler loop. By default the daemon
  sends a single heartbeat and exits; to run the scheduler loop you must
  explicitly enable it (for example: use `--start-scheduler` or set an
  environment flag like `AMPA_RUN_SCHEDULER=1`). Check the AMPA repository
  README for the exact flags and environment variables.

## Contributing

- Open an issue describing the change you'd like to make.
- Follow the relevant guide under `command/` for design and review steps.
- If adding a new skill, consider using the scripts in `skill/skill-creator/scripts` to scaffold and package it.

### AMPA Development

The AMPA Worklog plugin has been moved to its own independent repository:

The `skill/install-ampa/resources/ampa.mjs` file in this repository is a runtime loader that delegates to the installed AMPA package. To develop or modify AMPA:

1. Clone the AMPA repository
2. Make changes in the AMPA repository
3. Run tests in the AMPA repository (see its README for test commands)
4. Re-install with `skill/install-ampa/scripts/install-worklog-plugin.sh --yes` to get the latest version

See the [Migration Guide](docs/AMPA_MIGRATION.md) for information about transitioning from the old bundled installation to the new repository-based installation.

## Next steps / Suggestions

- Add example usage for each skill in `skill/` to make onboarding easier.

## License

See individual files for licenses. Some folders include a LICENSE.txt (for example: [skill/skill-creator/LICENSE.txt](skill/skill-creator/LICENSE.txt)).

---
If you'd like, I can commit this file, add a short changelog entry, or expand any section into more detailed docs.
