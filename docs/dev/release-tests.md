# Release Tests

This document covers the test commands and expectations for the release process.

## Test Levels

### 1. Smoke Tests

Quick sanity checks to verify the project builds and core functionality works. Run these locally before pushing to `dev`.

```sh
# Build the project
npm run build

# Run smoke tests (if available)
npm run test:smoke
# or
node --test tests/node/*smoke*
```

### 2. Critical Tests

Tests for high-priority features and known failure points. These must pass on `dev` before a release can be triggered.

```sh
# Run all tests (critical tests are included)
npm test
# or with verbose output
npm --silent test
```

### 3. Full Test Suite

The complete test suite must pass before the `dev` → `main` merge. This catches regressions that smoke and critical tests may miss.

```sh
# Full test suite with coverage (if available)
npm run test:coverage
# or
npm test -- --coverage
```

## Running Tests Locally

### Prerequisites

Ensure dev dependencies are installed:

```sh
npm ci --include=dev
```

### Build

Always build before running tests:

```sh
npm run build
```

### Test Commands

| Command | Description |
|---------|-------------|
| `npm test` | Run the full test suite |
| `npm --silent test` | Run tests with minimal output |
| `npm run test:smoke` | Run smoke tests only (if available) |
| `npm run test:coverage` | Run tests with coverage report |
| `node --test tests/node/test-*.mjs` | Run specific test files |

### Lint

Run lint checks before committing:

```sh
npm run lint
```

## CI Expectations

CI on `dev` must run at minimum:

- Build verification
- Smoke tests
- Critical tests

Before the `dev` → `main` merge, CI (or a local run) must confirm the **full test suite** passes.

## Troubleshooting

### Failing Tests

1. Identify the failing test(s) from the CI or local output.
2. Check if the failure is related to recent changes on `dev`.
3. If the test appears unrelated to your changes, create a test-failure work-item using the triage process.
4. Do not merge to `main` while tests are failing.

### Flaky Tests

- Document flaky tests in a work-item with the `flaky-test` tag.
- Do not disable tests without creating a work-item and getting reviewer approval.
- If a flaky test blocks a release, note it in the release checklist and proceed only if the reviewer agrees the failure is unrelated to the release.
