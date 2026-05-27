# Dev Branch Smoke & Critical Tests

Fast, high-value tests that run on every push to the `dev` branch to catch critical problems before they reach reviewers.

## Test Tiers

| Tier | File | Runtime | Purpose |
|---|---|---|---|
| Smoke | `tests/dev/smoke.mjs` | < 5 min | Fast sanity checks — repo structure, lint, tooling |
| Critical | `tests/dev/critical.mjs` | < 10 min | Deeper checks — pytest pass, skill integrity, YAML validity |

## Smoke Tests

Smoke tests are designed to be **fast** (under 5 minutes) and exercise the highest-value checks: repository structure, terminology compliance, test discovery, tooling availability, and agent frontmatter validation.

### Files

| File | Purpose |
|---|---|
| `tests/dev/smoke.mjs` | Node.js test suite using the built-in `node:test` runner |
| `tests/dev/critical.mjs` | Deeper critical-path tests (pytest pass, skill integrity, etc.) |
| `.github/workflows/dev-smoke.yml` | GitHub Actions workflow — runs both smoke and critical tests on every push to `dev` |

### What the smoke tests check

1. **Repository structure** — Key files and directories exist (`AGENTS.md`, `skill/`, `tests/conftest.py`, etc.)
2. **Terminology lint** — `scripts/check-terminology.sh` passes (no neutralisation violations)
3. **Python test discovery** — `pytest --collect-only` can discover tests
4. **Worklog CLI** — `wl` command is available on PATH
5. **Agent frontmatter lint** — `scripts/agent_frontmatter_lint.py` validates agent YAML frontmatter (skipped if `pyyaml` is unavailable)

### What the critical tests check

1. **Full pytest collection** — The entire test suite collects without errors
2. **Python test subset passes** — A representative subset of Python tests actually pass
3. **Skill integrity** — All skill directories have valid `SKILL.md` files with frontmatter
4. **CI workflow YAML validity** — All `.github/workflows/*.yml` files parse correctly
5. **Agent guidance consistency** — `AGENTS.md` and `Workflow.md` reference consistent terminology
6. **Worklog CLI functional** — `wl list` returns structured, valid JSON
7. **Essential scripts present** — Key scripts exist and shell scripts are executable

### Running locally

From the repository root:

```bash
# Run the full smoke test suite
node --test tests/dev/smoke.mjs

# Run the full critical test suite
node --test tests/dev/critical.mjs

# Run both suites together
node --test tests/dev/smoke.mjs tests/dev/critical.mjs

# Run a single test by name
node --test --test-name-pattern="repository structure" tests/dev/smoke.mjs
```

### CI integration

The `dev-smoke` workflow triggers automatically on every push to the `dev` branch. Results appear as a status check on the commit and in the GitHub Actions tab.

To verify the CI workflow file itself is valid YAML:

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/dev-smoke.yml'))"
```

## Running inside the dev container

If you are working inside the AMPA dev container (via `wl ampa start-work`):

1. Ensure Node.js 18+ and Python 3.10+ are available (they should be pre-installed).
2. Ensure `ripgrep` is installed (required by the terminology scan):

   ```bash
   sudo apt-get update && sudo apt-get install -y ripgrep
   ```

3. Run the smoke and critical tests:

   ```bash
   node --test tests/dev/smoke.mjs
   node --test tests/dev/critical.mjs
   ```

## Expectations

- Smoke tests **must pass** on every push to `dev`.
- If a smoke test fails, the push is considered **broken** and should be fixed before further integration work.
- Smoke tests are **not** a substitute for the full test suite — they are a first-line sanity check. Run `pytest` for comprehensive testing.
