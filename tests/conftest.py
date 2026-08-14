# Ensure repository root is on sys.path so local packages (skill, scripts) are importable
import sys
from pathlib import Path
from unittest import mock

import pytest

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


@pytest.fixture(autouse=True)
def _default_green_full_suite_cache():
    """Serve a green full-suite cache entry for audit flow tests by default.

    The audit runner's pre-flight cache gate (SA-0MSQ72BVV0011SRU) exits
    non-zero before any Phase 1 pi call when the per-repo test cache holds
    no green full-suite run at HEAD and no ``--run-tests`` / ``--green-run``
    opt-out was given. Audit flow tests at this level run ``cmd_issue``
    with fake runners and would otherwise hit the REAL per-repo cache,
    making them environment-dependent. Patch ``query_cached`` to a green
    entry so they proceed deterministically; tests that exercise cache
    behavior override it with their own ``mock.patch.object(...)``.
    """
    from skill.audit.scripts import audit_runner

    green = {
        "stdout": "5 passed in 0.03s",
        "stderr": "",
        "exit_code": 0,
        "completed_at": 1000.0,
        "command": "pytest -q -r a --disable-warnings",
        "git_state": "fingerprint",
        "cached": True,
    }
    with mock.patch.object(audit_runner, "query_cached", return_value=green):
        yield


@pytest.fixture(autouse=True)
def _default_resolvable_ownership():
    """Resolve undeterminable ownership to the launch project root.

    The undeterminable-ownership abort (SA-0MSLLGDW00098UCC) makes
    ``cmd_issue`` exit non-zero when the owning project root cannot be
    determined (no --worklog-dir, unknown item prefix, no sibling match).
    Flow tests that don't exercise ownership resolution (they audit ids
    like "SA-SKIP-ALL" or "TEST-1" without a worklog or a patched sibling
    scan) would otherwise abort for reasons unrelated to what they test.
    This autouse fixture resolves unknown prefixes to
    ``TARGET_PROJECT_ROOT`` — the launch cwd's project root — preserving
    the legacy fail-open behavior for those flows; tests that DO exercise
    ownership resolution override it with their own
    ``mock.patch.object(audit_runner, "_resolve_owning_project_root", ...)``
    — the inner patch wins while active.
    """
    from skill.audit.scripts import audit_runner

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
