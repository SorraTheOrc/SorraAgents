"""Shared fixtures for the audit skill test suite.

The pre-flight full-suite cache gate (SA-0MSQ72BVV0011SRU) makes
``cmd_issue`` exit non-zero before any Phase 1 pi call when the per-repo
test cache holds no green full-suite run at HEAD and no ``--run-tests`` /
``--green-run`` opt-out was given. Audit flow tests that do not exercise
the gate would otherwise hit the REAL per-repo cache, making them
environment-dependent (green only when a suite run happens to be cached at
the test run's git state).

The autouse fixture below patches ``query_cached`` to serve a green entry
by default so flow tests proceed deterministically; tests that exercise
cache behavior (the gate matrix, auto-verification diagnostics) override
it with their own ``mock.patch.object(audit_runner, "query_cached", ...)``
— the inner patch wins while active.
"""

from __future__ import annotations

from unittest import mock

import pytest

from skill.audit.scripts import audit_runner

_GREEN_CACHE_ENTRY = {
    "stdout": "5 passed in 0.03s",
    "stderr": "",
    "exit_code": 0,
    "completed_at": 1000.0,
    "command": "pytest -q -r a --disable-warnings",
    "git_state": "fingerprint",
    "cached": True,
}


@pytest.fixture(autouse=True)
def _default_green_full_suite_cache():
    """Serve a green full-suite cache entry by default (see module docstring)."""
    with mock.patch.object(
        audit_runner, "query_cached", return_value=_GREEN_CACHE_ENTRY
    ):
        yield
