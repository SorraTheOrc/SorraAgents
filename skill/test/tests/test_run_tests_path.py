"""``node_modules/.bin`` PATH augmentation for suite commands
(SA-0MWWKLY8M009PD78).

A changed-scope run builds a direct ``node_modules/.bin/vitest run <files>``
command but historically spawned it with a child ``PATH`` that did **not**
include ``<repo>/node_modules/.bin``. Tests that shell out to a local binary
(e.g. ``tsx``) then failed with ``sh: tsx: not found``, even though the same
tests passed under the full suite because ``npm test`` puts
``node_modules/.bin`` on ``PATH``.

The fix is env-only: :func:`_suite_command_env` prepends the absolute
``<repo>/node_modules/.bin`` (existence-gated, deduped) ahead of the existing
``~/.local/bin`` normalisation. These assertions are behavioural — the child
``PATH`` is read back from a **real** process spawned through
:func:`_run_cmd`, the single suite-command execution point — so a regression
that drops the augmentation fails here instead of silently restoring the
``sh: tsx: not found`` failure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path — mirror test_run_tests_tmpdir.py / test_vitest_worker_cap.py
# so `import run_tests` works regardless of collection order.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

_RUNNER_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))

from run_tests import (
    _run_cmd,
    _suite_command_env,
    full_suite_commands,
)
from test_runner import subprocess_env_with_scrubbed_overrides


def _child_path_probe(cwd: Path) -> str:
    """Read the child ``PATH`` through the real suite execution path."""
    script = "import json, os; print(json.dumps({'PATH': os.environ.get('PATH', '')}))"
    proc = _run_cmd([sys.executable, "-c", script], cwd=cwd, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])["PATH"]


def _make_repo_with_node_bin(tmp_path: Path) -> tuple[Path, Path]:
    """Create a throwaway repo root holding ``node_modules/.bin``."""
    repo = tmp_path / "repo"
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    return repo, bin_dir


# ---------------------------------------------------------------------------
# Branch A — the directory exists: it leads PATH, exactly once
# ---------------------------------------------------------------------------


def test_node_modules_bin_leads_child_path_when_present(tmp_path, monkeypatch):
    """A local ``node_modules/.bin`` is prepended to the child PATH.

    The child is spawned through :func:`_run_cmd` (the runner's real env
    construction point), so this proves the wiring end-to-end: a
    ``node_modules/.bin/vitest run <files>`` command can shell-spawn local
    bins (``tsx`` etc.) exactly as ``npm test`` does.
    """
    # Hermetic: the ambient PATH must not already carry the fixture dir.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    repo, bin_dir = _make_repo_with_node_bin(tmp_path)

    child_path = _child_path_probe(repo)
    parts = child_path.split(os.pathsep)

    assert parts[0] == str(bin_dir.resolve())
    assert parts.count(str(bin_dir.resolve())) == 1


def test_duplicate_node_modules_bin_is_deduped(tmp_path, monkeypatch):
    """An already-present entry is moved to the front, never duplicated."""
    repo, bin_dir = _make_repo_with_node_bin(tmp_path)
    absolute = str(bin_dir.resolve())
    # Simulate a launcher that already put the dir on PATH (not first).
    monkeypatch.setenv("PATH", f"/usr/bin:{absolute}:/bin")

    parts = _child_path_probe(repo).split(os.pathsep)

    assert parts[0] == absolute
    assert parts.count(absolute) == 1


# ---------------------------------------------------------------------------
# Branch B — the directory is absent: a graceful no-op
# ---------------------------------------------------------------------------


def test_absent_node_modules_bin_leaves_path_unchanged(tmp_path, monkeypatch):
    """With no ``node_modules/.bin`` the child PATH is the shared helper's.

    This is the pytest-only / no-installed-deps case (the SorraAgents
    checkout itself): the augmentation is a no-op, never an error.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    repo = tmp_path / "plain-repo"
    repo.mkdir()

    expected = subprocess_env_with_scrubbed_overrides()["PATH"]
    child_path = _child_path_probe(repo)

    assert child_path == expected
    assert str((repo / "node_modules" / ".bin").resolve()) not in child_path.split(
        os.pathsep
    )


# ---------------------------------------------------------------------------
# AC5 — env-only: command strings and cache keys are byte-identical
# ---------------------------------------------------------------------------


def test_command_strings_are_env_only(tmp_path):
    """The augmentation never rewrites a command string (no cache churn).

    ``full_suite_commands`` is the per-repo command/cache-key source; it is
    byte-identical whether or not an augmentation-triggering repo root is in
    play, and no command carries a PATH fragment.
    """
    before = full_suite_commands(REPO_ROOT)

    # Building the env for a repo WITH node_modules/.bin must not touch the
    # command set.
    repo, _bin_dir = _make_repo_with_node_bin(tmp_path)
    _suite_command_env(repo)

    after = full_suite_commands(REPO_ROOT)

    assert after == before
    assert not any(os.pathsep in cmd or "node_modules" in cmd for cmd in after)
