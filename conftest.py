"""Repo-root pytest conftest: arm the live-repo mutation guard (F5, SA-0MUG30JSE0069JEQ).

The guard mirrors the ``run_tests.py`` snapshot guard (F4) for **direct**
``pytest`` invocations: a test that mutates the checkout makes the session exit
non-zero and the offending test is named.

It is loaded **by file location** (``importlib.util.spec_from_file_location``)
so this conftest never mutates ``sys.path``. That ordering matters: the existing
``tests/conftest.py`` deliberately prepends the repo root and skills root in a
specific order (SA-0MTJQB2MA008HMO6), and ``import test.scripts.run_tests`` plus
``tests/test_plan_package_resolution.py`` / ``tests/test_pytest_path_resolution.py``
are the canaries for that ordering.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_GUARD_PATH = _REPO_ROOT / "skill" / "shared" / "live_repo_guard.py"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("_live_repo_guard", _GUARD_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load live-repo guard from {_GUARD_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_guard = _load_guard_module()


def pytest_configure(config):
    _guard.register(config, root=_REPO_ROOT)
