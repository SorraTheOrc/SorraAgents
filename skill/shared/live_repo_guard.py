"""Pytest plugin: fail a session that mutates its own checkout (F5, SA-0MUG30JSE0069JEQ).

This is the inner net for **direct** ``pytest`` invocations (the ``run_tests.py``
guard in F4 is the outer net for the release path). It is deliberately a
*reusable plugin module* — not a conftest — so the repo-root ``conftest.py`` and
a test's ``tmp_path`` conftest can arm the **same hooks** without a
self-referential simulation.

Behaviour:

- ``pytest_configure`` → ``register(config, root=...)`` snapshots the checkout.
- ``pytest_runtest_teardown`` → compares a cheap **refs + local-config**
  fingerprint and fails the offending test immediately, naming its node id.
- ``pytest_sessionfinish`` → compares the **full** surface (refs, config,
  tracked/untracked working tree) and exits non-zero, naming the last test if
  only the file surface changed.

Safety / no-false-positive rules (F5 AC2/AC5/AC6/AC7):

- loaded by **file location** so it never mutates ``sys.path``;
- a recursion marker env var makes nested pytest/``run_tests.py`` stand down;
- a no-op under ``--collect-only`` and under xdist workers;
- a no-op for non-git roots;
- refs checked out in ``.worklog/worktrees/`` (agent worktrees) are excluded
  (derived from ``git worktree list --porcelain``, never a ``wl-*`` pattern).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

#: Marker propagated to subprocesses so nested runs stand down.
MARKER_ENV = "LIVE_REPO_GUARD_ACTIVE"


def _load_git_sandbox() -> Any:
    """Load ``git_sandbox.py`` by file location (stdlib-only, no sys.path)."""
    path = Path(__file__).resolve().parent / "git_sandbox.py"
    spec = importlib.util.spec_from_file_location("_live_repo_git_sandbox", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load git sandbox helper from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveRepoMutationGuard:
    """A registered pytest plugin that fails a session which mutates its root."""

    def __init__(self, root: Path) -> None:
        self._gs = _load_git_sandbox()
        self.root = Path(root).resolve()
        self.before = self._gs.snapshot_repo_state(self.root)
        self._baseline = self._gs.fingerprint(
            self._gs.snapshot_repo_state(self.root, include_status=False)
        )
        self.active = bool(self.before.get("git"))
        self.last_test: str | None = None
        self.offender: str | None = None

    # -- hooks ---------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.last_test = nodeid

    def pytest_runtest_teardown(self, item: Any, nextitem: Any) -> None:
        if not self.active:
            return
        current = self._gs.snapshot_repo_state(self.root, include_status=False)
        if self._gs.fingerprint(current) == self._baseline:
            return
        diff = self._gs.diff_snapshots(self.before, self._gs.snapshot_repo_state(self.root))
        self.offender = item.nodeid
        # Reset the baseline so a single mutation is reported once.
        self.before = self._gs.snapshot_repo_state(self.root)
        self._baseline = self._gs.fingerprint(
            self._gs.snapshot_repo_state(self.root, include_status=False)
        )
        pytest.fail(
            f"live-repo mutation detected during {item.nodeid}:\n"
            + self._gs.describe_diff(diff),
            pytrace=False,
        )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        if not self.active:
            return
        after = self._gs.snapshot_repo_state(self.root)
        diff = self._gs.diff_snapshots(self.before, after)
        if not diff["changed"]:
            return
        named = self.offender or self.last_test or "<unknown test>"
        session.exitstatus = 1
        print(
            "ERROR: live-repo mutation detected at session end"
            f" (last recorded test: {named}):\n" + self._gs.describe_diff(diff),
            file=sys.stderr,
        )


def register(config: Any, root: str | os.PathLike[str] | None = None) -> Any:
    """Arm the guard for *config* unless a stand-down condition applies.

    Returns the guard instance when armed, else None. Called by the repo-root
    ``conftest.py`` and by test fixtures that arm it inside a ``tmp_path`` repo.
    """
    if os.environ.get(MARKER_ENV):
        return None
    if config.getoption("collectonly", False):
        return None
    if hasattr(config, "workerinput"):  # xdist worker: controller owns the check
        return None

    resolved_root = Path(root) if root is not None else _default_root(config)
    guard = LiveRepoMutationGuard(resolved_root)
    if not guard.active:
        return None
    os.environ[MARKER_ENV] = "1"
    config.pluginmanager.register(guard, "live_repo_mutation_guard")
    return guard


def _default_root(config: Any) -> Path:
    for attr in ("rootpath", "rootdir"):
        value = getattr(config, attr, None)
        if value:
            return Path(str(value))
    return Path.cwd()
