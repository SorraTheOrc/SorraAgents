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
  - [skill/planall/](skill/planall/): PlanAll — automated batch planning for intake_complete work items.
  - [skill/intakeall/](skill/intakeall/): IntakeAll — automated batch intake for idea-stage work items.
  - [skill/implementall/](skill/implementall/): ImplementAll — automated batch implementation for plan_complete work items.
- plugins/: local agent framework plugins used by this repository (includes `ralph` compaction plugin).
- docs/dev/: development and release process documentation ([release-process.md](docs/dev/release-process.md), [release-tests.md](docs/dev/release-tests.md)).
- Workflow.md: high-level workflow for using this repository.
- package.json: basic metadata used by tooling.

## Ralph compaction plugin

This repository includes a local plugin at `plugins/ralph.js` that
implements `experimental.session.compacting` to preserve original session intent
during compaction.

- If the original prompt matches override patterns (for example `implement <id>`),
  `ralph` can provide a derived compaction prompt (for example `audit <id> ...`).
- If no override applies, it appends the original prompt to compaction context.

Behavior, configuration options, and test references are documented in
`docs/ralph-compaction-plugin.md`.

## Ralph orchestration loop

The repository also includes the Ralph implement→audit loop for Worklog items.
Use `/home/rgardler/.pi/agent/skills/ralph/ralph <work-item-id>` to launch a background run and `/home/rgardler/.pi/agent/skills/ralph/ralph status` to inspect the current process.
When the target has children, Ralph runs a per-child implement→audit loop using `implement-single` for each child and finishes with a parent-level integration audit.
Ralph runs non-interactively by default, includes a stream watchdog so a delegated `pi` process that keeps stdout open too long fails with a clear error instead of hanging forever, and can stop early with a structured `producer_input_required` result when the model cannot safely continue without producer input.

When the target work item has children, Ralph iterates over each child independently: implementing, auditing, and remediating each child before moving to the next, followed by a final parent-level integration audit. For single work items (no children), Ralph uses the classic implement→audit→remediate loop.

See `docs/ralph.md` for the full command reference and operational guidance.

## PlanAll — Automated Batch Planning

The PlanAll skill (`skill/planall/`) provides automated batch planning for work items
in `intake_complete` status. It discovers all eligible items, invokes `/skill:plan` for
each sequentially, detects items that require producer input, and produces a
summary report.

```bash
# Process all intake_complete items
python3 skill/planall/scripts/planall.py

# JSON output for programmatic use
python3 skill/planall/scripts/planall.py --json

# Post summary as a comment on a parent epic
python3 skill/planall/scripts/planall.py --parent-id SA-0MQA6ECEU003GUKH

# Process at most 10 items
python3 skill/planall/scripts/planall.py --max 10

# Set per-item timeout to 300 seconds
python3 skill/planall/scripts/planall.py --item-timeout 300

# Combine --max and --item-timeout
python3 skill/planall/scripts/planall.py --max 5 --item-timeout 120
```

See [skill/planall/SKILL.md](skill/planall/SKILL.md) for full documentation.

## IntakeAll — Automated Batch Intake

The IntakeAll skill (`skill/intakeall/`) provides automated batch intake for work items
in `idea` stage. It discovers all eligible items, auto-completes well-defined items,
marks items lacking sufficient detail as `needs_input` (skipping the interactive
`/intake` subprocess that would block in batch mode), attempts error recovery, and
produces a summary report.

```bash
# Process all idea-stage items
python3 skill/intakeall/scripts/intakeall.py

# JSON output for programmatic use
python3 skill/intakeall/scripts/intakeall.py --json

# Dry run (simulate without changes)
python3 skill/intakeall/scripts/intakeall.py --dry-run

# Post summary as a comment on a parent epic
python3 skill/intakeall/scripts/intakeall.py --parent-id SA-0MQK9SWN6008DWVQ

# Process at most 10 items
python3 skill/intakeall/scripts/intakeall.py --max 10

# Set per-item timeout to 300 seconds
python3 skill/intakeall/scripts/intakeall.py --item-timeout 300

# Combine --max and --item-timeout
python3 skill/intakeall/scripts/intakeall.py --max 5 --item-timeout 120
```

See [skill/intakeall/SKILL.md](skill/intakeall/SKILL.md) for full documentation.

## ImplementAll — Automated Batch Implementation

The ImplementAll skill (`skill/implementall/`) provides automated batch implementation
for work items in `plan_complete` stage. It discovers all eligible items, invokes
`/skill:implement` for each sequentially, detects items that require producer input,
attempts error recovery on failures, and produces a summary report.

```bash
# Process all plan_complete items
python3 skill/implementall/scripts/implementall.py

# JSON output for programmatic use
python3 skill/implementall/scripts/implementall.py --json

# Dry run (simulate without changes)
python3 skill/implementall/scripts/implementall.py --dry-run

# Process at most 5 items
python3 skill/implementall/scripts/implementall.py --max 5

# Set per-item timeout to 300 seconds
python3 skill/implementall/scripts/implementall.py --item-timeout 300

# Combine --max and --item-timeout
python3 skill/implementall/scripts/implementall.py --max 3 --item-timeout 120

# Post summary as a comment on a parent epic
python3 skill/implementall/scripts/implementall.py --parent-id SA-0MQO6YMZ3006N5MG
```

See [skill/implementall/SKILL.md](skill/implementall/SKILL.md) for full documentation.

A useful debugging pattern is to focus Ralph on a single direct child work item:

```sh
python3 /home/rgardler/.pi/agent/skills/ralph/scripts/ralph_loop.py <parent-id> --child <child-id> --json
```

## CI Workflows

This repository uses GitHub Actions to validate changes. The following workflows are available:

| Workflow | File | Trigger | Description |
|---|---|---|---|
| CI | `.github/workflows/ci.yml` | push, pull_request | Full unit test suite on every push and PR. Required status check for `main`. |
| Cleanup dry-run | `.github/workflows/cleanup.yml` | pull_request, workflow_dispatch | Validates cleanup scripts in dry-run mode. |
| Dev smoke | `.github/workflows/dev-smoke.yml` | push to `dev` | Runs smoke and critical tests on the `dev` branch. Results appear in commit checks. |
| Dev full suite | `.github/workflows/dev-full-suite.yml` | workflow_dispatch, push to `release-candidate/**` | Runs the full test suite. Artifacts (JUnit XML, HTML report, logs) are uploaded for reviewer use. |

### Re-running jobs

- **Dev smoke**: Pushes to `dev` trigger automatically. Results are visible in the Actions tab and on commit check pages.
- **Dev full suite**: Go to **Actions → dev-full-suite → Run workflow** and select the branch. Optionally provide a reason. This is intended for release managers to run before merging `dev` → `main`.

### Release process

The `dev-full-suite` workflow result is used as a gate in the release process:

1. Changes are integrated into `dev` via feature branch pushes.
2. The release manager triggers `dev-full-suite` manually on the release candidate.
3. If the full suite passes, the release manager reviews uploaded artifacts and proceeds with the `dev` → `main` merge.
4. If the full suite fails, the release is blocked until failures are resolved.

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

## CI Workflows

Two CI workflows validate changes on the `dev` branch:

- **`dev-smoke`** — runs smoke and critical tests on every push to `dev`, providing fast pass/fail feedback in the commit checks UI.
- **`dev-full-suite`** — runs the full test suite, triggered manually via `workflow_dispatch` or on release-candidate tags. This workflow acts as a gating check before merging to `main`.

See [release-tests.md](docs/dev/release-tests.md) for test commands and CI configuration details.

## Release Process

Promoting changes from `dev` to `main` requires a human-reviewed merge.
See the full [release process documentation](docs/dev/release-process.md) for
the checklist and role definition.

### For Release Managers

```sh
# Run a dry-run to preview the merge
bash scripts/release/merge-dev-to-main.sh --dry-run

# Execute the merge (after confirming CI is green)
bash scripts/release/merge-dev-to-main.sh [--work-item-id <id>]

# Override CI gate in exceptional circumstances (--force logs a warning)
bash scripts/release/merge-dev-to-main.sh --force [--work-item-id <id>]
```

The script enforces **two hard gates** before executing the release:

1. **`dev-full-suite` CI gate** — aborts if CI is not green (use `--force` to
   bypass in exceptional circumstances).
2. **Audit readiness gate** — checks all `in_review` / `completed` work items
   for passing audits (exit code 6; use `--skip-checks` to bypass).

After both gates pass, the script merges `dev` into `main`, pushes the result,
and records an audit comment in the worklog.

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

- Add a CI workflow to validate new skills and docs.
- Add example usage for each skill in `skill/` to make onboarding easier.

## License

See individual files for licenses. Some folders include a LICENSE.txt (for example: [skill/skill-creator/LICENSE.txt](skill/skill-creator/LICENSE.txt)).

---
If you'd like, I can commit this file, add a short changelog entry, or expand any section into more detailed docs.
