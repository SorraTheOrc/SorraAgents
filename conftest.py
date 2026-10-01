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
"""

from __future__ import annotations

import functools
import importlib.util
import subprocess
import warnings
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
    untracked = _untracked_skill_dirs()
    if untracked:
        warnings.warn(
            "untracked skill dir(s) present (concurrent agent WIP?) — "
            "excluded from collection: " + ", ".join(untracked),
            stacklevel=1,
        )
