"""Shared fixtures for the test-churn skill tests.

The analyser lives in ``scripts/`` and is deliberately not a package, so it is
loaded by path with a name that does **not** match pytest's ``test_*``
collection pattern (which would otherwise try to import the script itself).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_NAME = "test_churn_analysis_under_test"


@pytest.fixture(scope="session")
def churn():
    """Load and return the analyser module under test."""
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, SCRIPTS_DIR / "test_churn_analysis.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def init_repo(repo: Path) -> None:
    """Initialise a repository with a deterministic identity and branch."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "dev")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Test Author")


def write(repo: Path, relpath: str, content: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def commit(
    repo: Path, message: str, date: str = "2026-01-01T12:00:00+00:00"
) -> str:
    """Stage everything and create a commit with a fixed date."""
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-qm", message, env=env)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Provide a small repository exercising A/M/D/R statuses."""
    repo = tmp_path / "repo"
    init_repo(repo)

    # Commit 1: add source + test (A).
    write(repo, "src/app.py", "def add(a, b):\n    return a + b\n")
    write(
        repo,
        "tests/test_app.py",
        "from src.app import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
    )
    commit(repo, "feat: add app", "2026-01-01T12:00:00+00:00")

    # Commit 2: modify test + source (M, a rewrite).
    write(
        repo,
        "src/app.py",
        "def add(a, b):\n    return a + b\n\n"
        "def sub(a, b):\n    return a - b\n",
    )
    write(
        repo,
        "tests/test_app.py",
        "from src.app import add, sub\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_sub():\n    assert sub(3, 1) == 2\n",
    )
    commit(repo, "feat: add sub", "2026-02-01T12:00:00+00:00")

    # Commit 3: rename the test file with a modification (R).
    _git(repo, "mv", "tests/test_app.py", "tests/test_math.py")
    write(
        repo,
        "tests/test_math.py",
        "from src.app import add, sub\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n\n"
        "def test_sub():\n    assert sub(3, 1) == 2\n",
    )
    commit(repo, "refactor: rename test", "2026-03-01T12:00:00+00:00")

    # Commit 4: delete the test file wholesale (D), source-only change.
    (repo / "tests" / "test_math.py").unlink()
    write(repo, "src/app.py", "def add(a, b):\n    return a + b\n")
    commit(repo, "test: drop tests", "2026-04-01T12:00:00+00:00")
    return repo
