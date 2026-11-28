# Pre-Merge Full-Suite Testing

## Overview

Before any merge from `dev` → `main` is performed, the **full test suite**
must pass locally. This ensures `main` always contains releasable, verified
code.

This project operates fully locally — there is no CI pipeline and no GitHub
Actions workflows. All validation happens on the developer's machine.

## Local test commands

Run the full test suite:

```bash
python3 -m pytest -q
```

Or via the cached test runner (recommended — repeated verification at the
same git state is served from a per-repo cache instead of re-running the
whole suite; see [`skill/test_cache.py`](../../skill/test_cache.py) and
SA-0MSGN5OJ4002OZKY):

```bash
python3 skill/test/scripts/run_tests.py --json        # full suite, cached
python3 skill/test/scripts/run_tests.py --summary     # cached summary lines only
python3 skill/test/scripts/run_tests.py --force       # fresh run, refresh cache
```

Run only the smoke tests (fast, high-confidence checks):

```bash
python3 -m pytest -m smoke -q
```

Run only the critical tests (essential validation that must always pass):

```bash
python3 -m pytest -m critical -q
```

For verbose output on a single group:

```bash
python3 -m pytest tests/dev/test_smoke.py -v -k smoke
python3 -m pytest tests/dev/test_smoke.py -v -k critical
```

## How the merge is gated on success

1. The developer/Release Manager runs the full test suite locally.
2. All tests must pass:
   - **Green** → proceed with the merge to `main`.
   - **Red** → investigate failures, fix them, and re-run the full suite.
3. The merge to `main` should **only** be performed when the full suite
   reports success.

## The full suite does not type-check

A green full suite is **not** evidence that the tree compiles. Every test
runner used here (vitest, `node --test`, pytest) transpiles or interprets the
source without type-checking, so a TypeScript tree with unresolved imports or
type errors can still be full suite green. The release therefore runs a
separate **fatal build/type-check gate** on the merged dev→main tree
(`scripts/release/assert-build.mjs`, invoked by `merge-dev-to-main.sh`): it
runs the repository's build (`package.json` `scripts.build`) and blocks the
release on failure, before any tag is created (SA-0MWKQ5TXL005CLX5). It is a
no-op for repositories without a `scripts.build`; `SHIP_BUILD_SKIP=1` bypasses
it (logged; not recommended). See
[release-process.md — Build / Type-Check Gate](./release-process.md#build--type-check-gate).

Because the gate can only assert the project's build script, that script must
cover the **whole** source tree, package subtrees included. A root `tsc` whose
`tsconfig.json` `include` is `["src/**/*"]` does **not** cover
`packages/*/src/**`, so type errors there would pass both gates. A build that
misses package subtrees is a **project-side configuration error**, not a
silently accepted release; fix the build/typecheck configuration rather than
bypassing the gate.

## Hang abort vs host contention

The runner that executes the project suite bounds every stage with a
wall-clock timeout: a stage that never returns is aborted with exit code
**124** and a `[hang-timeout]` diagnostic instead of stalling the gate
indefinitely. That abort is **not** a red suite and is **never auto-retried**
— a hang must surface rather than be masked.

The `[hang-timeout]` block reports the aborted **stage**, the configured
**bound**, the **elapsed** wall time, the **delta** over the bound, and the
host **load average** at abort plus the **core count**. Read the load first:

- **Load far above the core count** (e.g. `load 38.4 over 16 cores`) ⇒ host
  contention, not a code defect. A green suite can exceed its bound purely
  because the machine is saturated (concurrent suite runs, a Monte-Carlo
  sweep, etc.). The abort is deliberately not retried; re-run the suite once
  the host is quiet rather than bisecting a green tree.
- **Load near zero with a large overrun** ⇒ a genuine hang. Investigate the
  named stage: a browser-stage hang is usually a starved
  `requestAnimationFrame` loop or an undestroyed `Phaser.Game`; a unit-stage
  hang is a synchronous loop in test/engine code that `testTimeout` cannot
  preempt.

So a contended-host abort means **re-run on a quiet host**, and a genuine
hang means **fix the cause**. Neither is a "red suite" failure to chase with
test bisection. Tracked as SA-0MWEURUYL004I470.

## Resuming after a hang abort (green cache preservation)

The release gate runs the full suite through the **cached runner**
(`run_tests.py`), keyed on command + git state with a 2-hour TTL. A
hang/abort (exit **124**, or a non-zero exit with no parsable test result)
used to overwrite the entry for that tree with `exit_code: 124`, so a retry
re-ran every stage from scratch (SA-0MWEURY420040OMD). It no longer does:

- A hang/abort **never destroys a green entry** for the same command + git
  state that is still within the TTL. The green verdict is kept and the abort
  is recorded in a side-car (`abort.json`) in the entry directory.
- A **re-run against an unchanged tree resumes green**: the preserved green
  entry is served without executing `npm --silent test`, *and* the earlier
  abort is reported with the `green_preserved_after_abort` notice — the gate
  never silently accepts an abort as green.
- The preservation **never masks a real failure**: a red verdict, a changed
  tree or an expired TTL still executes and stores freshly. A non-zero run
  that yields a parsable verdict (a failing test, or the retry runner's
  `coverage-failed` line) is red, not an abort.

Operator summary: **the tree is unchanged within the TTL — a re-run resumes
from the preserved green entry; an aborted run is always named.** If you want
to force a fresh execution regardless, pass `--force` (or wait for the TTL to
expire).

## Re-running after a fix

If the suite fails:

1. Review the failure output.
2. Fix the underlying issues on the `dev` branch.
3. Re-run the full suite locally.
4. Do **not** merge until the full suite passes.
