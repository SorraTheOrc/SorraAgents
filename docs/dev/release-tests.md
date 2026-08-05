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

## Re-running after a fix

If the suite fails:

1. Review the failure output.
2. Fix the underlying issues on the `dev` branch.
3. Re-run the full suite locally.
4. Do **not** merge until the full suite passes.
