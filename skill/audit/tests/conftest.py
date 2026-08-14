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


@pytest.fixture(autouse=True)
def _default_resolvable_ownership():
    """Resolve undeterminable ownership to the launch project root.

    The undeterminable-ownership abort (SA-0MSLLGDW00098UCC) makes
    ``cmd_issue`` exit non-zero when the owning project root cannot be
    determined (no --worklog-dir, unknown item prefix, no sibling match).
    Flow tests that don't exercise ownership resolution (they audit the
    synthetic id "TEST-1" without a worklog or a patched sibling scan)
    would otherwise abort for reasons unrelated to what they test. This
    autouse fixture resolves unknown prefixes to ``TARGET_PROJECT_ROOT``
    — the launch cwd's project root — preserving the legacy fail-open
    behavior for those flows; tests that DO exercise ownership resolution
    (the launch-context suite) override it with their own
    ``mock.patch.object(audit_runner, "_resolve_owning_project_root", ...)``
    — the inner patch wins while active.
    """
    real_resolve = audit_runner._resolve_owning_project_root

    def _resolvable(issue_id, worklog_dir=None):
        root = real_resolve(issue_id, worklog_dir=worklog_dir)
        if root is not None:
            return root
        return audit_runner.TARGET_PROJECT_ROOT

    with mock.patch.object(
        audit_runner, "_resolve_owning_project_root", side_effect=_resolvable
    ):
        yield
