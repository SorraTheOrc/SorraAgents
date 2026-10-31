"""Repo-root pytest conftest: arm the live-repo mutation guard (F5, SA-0MUG30JSE0069JEQ).

The guard mirrors the ``run_tests.py`` snapshot guard (F4) for **direct**
``pytest`` invocations: a test that mutates the checkout makes the session exit
non-zero, naming the last test if the offending test is named.

It is loaded **by file location** (``importlib.util.spec_from_file_location``)
so this conftest never mutates ``sys.path``. That ordering matters: the existing
``tests/conftest.py`` deliberately prepends the repo root and skills root in a
specific order (SA-0MTJQB2MA008HMO6), and ``import test.scripts.run_tests`` plus
``tests/test_plan_package_resolution.py`` / ``tests/test_pytest_path_resolution.py``
are the canaries for that ordering.

Untracked-skill-directory guard (SA-0MUPDDMXB0088CMM): when a concurrent agent
leaves an untracked ``skill/<new-skill>/`` directory in the main checkout, its
``tests/`` subdirectory would otherwise collide with the top-level ``tests``
package (``ModuleNotFoundError: No module named 'tests.<name>'``). This conftest
hooks ``pytest_ignore_collect`` to skip any ``skill/<name>/`` directory that
is **not** tracked by git, so the full suite always passes regardless of
concurrent WIP.

Inherited-``GIT_INDEX_FILE`` guard (SA-0MVWQ4FZ4003S0GE): ``implement.py
finish`` exports its temporary would-be-commit index for the suite it runs
(SA-0MVQHVGFR006W8LG) and that variable deliberately survives the
repository-override scrub (SA-0MVQHV9Y3001RNNW). A fixture git helper that
runs raw ``git add`` in its own throwaway repository therefore stages into the
*gate's* index, and the gate can no longer attribute its verdict — historically
reported post-hoc as ``gate evidence invalid`` with no culprit named. The
autouse fixture below polls the inherited index after every test and fails the
test that wrote it, naming the test and the remediation (the hermetic helpers
in ``skill/shared/git_sandbox.py``). It is inert without an inherited index and
is bound to the index inherited at session start, so a test that exports its
own index on purpose is unaffected.
"""

from __future__ import annotations

import functools
import importlib.util
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_GUARD_PATH = _REPO_ROOT / "skill" / "shared" / "live_repo_guard.py"
_INDEX_GUARD_PATH = _REPO_ROOT / "skill" / "shared" / "inherited_index_guard.py"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("_live_repo_guard", _GUARD_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load live-repo guard from {_GUARD_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_index_guard_module():
    spec = importlib.util.spec_from_file_location(
        "_inherited_index_guard", _INDEX_GUARD_PATH
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load index guard from {_INDEX_GUARD_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: ``dataclasses`` resolves ``cls.__module__`` through
    # ``sys.modules`` while the class body is being processed, so a module
    # loaded by location without this entry fails at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_guard = _load_guard_module()

#: Watch on the inherited ``GIT_INDEX_FILE``, armed at session start when the
#: finish gate exported one (``None`` for an ordinary ``pytest`` invocation).
_index_watch = None

#: ``pytest_configure`` can run more than once in a process (a test that calls
#: ``pytest.main`` / ``run_tests`` in-process). The baseline must stay the index
#: inherited by the *outermost* session — a nested run's own gate index (a
#: throwaway repository's) is deliberately not watched.
_index_guard_armed = False


def _arm_inherited_index_guard(config) -> None:
    """Arm the inherited-index guard for this session, when applicable."""
    global _index_watch, _index_guard_armed
    if _index_guard_armed:
        return
    if getattr(config.option, "collectonly", False):
        return
    if hasattr(config, "workerinput"):
        # An xdist worker must not fail a test for a write another worker made:
        # the file is shared, the ownership of the write is not.
        return
    _index_guard_armed = True
    _index_watch = _load_index_guard_module().IndexWatch.capture(_REPO_ROOT)


@functools.cache
def _is_tracked(path: Path) -> bool:
    """Return True if *path* is tracked by git (cached per path)."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(path)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.returncode == 0


def _ignore_untracked_skill_dirs(collection_path: Path) -> bool:
    """Return True to ignore *collection_path* if it lies under an
    untracked ``skill/<name>/`` directory (SA-0MUPDDMXB0088CMM)."""
    try:
        rel = collection_path.relative_to(_REPO_ROOT)
    except ValueError:
        return False

    parts = rel.parts
    # The skill directory is always the first two components: skill/<name>.
    if len(parts) < 2 or parts[0] != "skill":
        return False

    skill_dir = _REPO_ROOT / parts[0] / parts[1]
    if not skill_dir.is_dir():
        return False
    return not _is_tracked(skill_dir)


def _untracked_skill_dirs() -> list[str]:
    """Names of candidate skill dirs (``SKILL.md`` or ``tests/``) untracked
    by git — another agent's in-progress WIP (SA-0MUPDDMXB0088CMM).

    ``__pycache__`` and other non-skill artefacts are excluded so the warning
    stays actionable.
    """
    skills_root = _REPO_ROOT / "skill"
    if not skills_root.is_dir():
        return []
    return sorted(
        d.name
        for d in skills_root.iterdir()
        if d.is_dir()
        and ((d / "SKILL.md").exists() or (d / "tests").is_dir())
        and not _is_tracked(d)
    )


def pytest_ignore_collect(collection_path: Path, config):
    """Skip untracked skill directories during collection."""
    if _ignore_untracked_skill_dirs(collection_path):
        return True
    return None  # let pytest continue with normal collection


def pytest_configure(config):
    _guard.register(config, root=_REPO_ROOT)
    _arm_inherited_index_guard(config)
    untracked = _untracked_skill_dirs()
    if untracked:
        warnings.warn(
            "untracked skill dir(s) present (concurrent agent WIP?) — "
            "excluded from collection: " + ", ".join(untracked),
            stacklevel=1,
        )


@pytest.fixture(autouse=True)
def _inherited_index_unchanged(request):
    """Fail the test that staged into the finish gate's inherited index.

    The suite runs under ``implement.py finish`` with ``GIT_INDEX_FILE``
    pointing at the gate's temporary would-be-commit index
    (SA-0MVQHVGFR006W8LG), which deliberately survives the repository-override
    scrub (SA-0MVQHV9Y3001RNNW). A fixture that runs raw ``git add`` in its own
    repository then rewrites that index instead of its own, and the gate can no
    longer attribute its verdict. This fixture names the offending test instead
    of leaving the failure anonymous at the end of the suite
    (SA-0MVWQ4FZ4003S0GE).

    It is a no-op without an inherited index, and it watches only the index
    inherited at session start — a test that deliberately exports its own
    ``GIT_INDEX_FILE`` is exercising that contract, not violating it.
    """
    yield
    if _index_watch is None:
        return
    report = _index_watch.poll()
    if report:
        pytest.fail(f"{report}\n  offending test: {request.node.nodeid}", pytrace=False)


@pytest.fixture(autouse=True)
def _isolated_test_semaphore_dir(tmp_path_factory, monkeypatch):
    """Isolate test-time semaphore acquisitions from the host-wide pool.

    The full suite runs under an outer ``"test"`` semaphore slot held for the
    whole session. Any test that acquires a test slot in-process
    (``run_tests.main``, ``run_suite``, ``run_all``) or spawns a nested
    ``run_tests.py`` would otherwise queue behind that slot; under concurrent
    agent load two full suites each hold a slot and the nested acquire never
    proceeds (SA-0MUY6U05F008RG86). Each test gets its own namespace, so
    nested work never contends host-wide while top-level (production) runs keep
    the host-wide ceiling. Tests that exercise the semaphore directly override
    ``PI_SEMAPHORE_DIR`` (or ``ENV_LOCK_DIR``) with a directory of their own.
    """
    monkeypatch.setenv(
        "PI_SEMAPHORE_DIR", str(tmp_path_factory.mktemp("test-semaphores"))
    )
