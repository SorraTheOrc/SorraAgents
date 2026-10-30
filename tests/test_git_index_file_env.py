"""Regression pins for ``GIT_INDEX_FILE`` surviving the test-skill env scrub.

SA-0MVQHV9Y3001RNNW (child of SA-0MVL2NH1G0094FA2 — finish gate F5).

``implement.py finish`` stages the *would-be commit* in a temporary index and
exports ``GIT_INDEX_FILE`` so every child ``git`` command spawned by the suite
— including ``git ls-files`` guards inside tests — sees the staged file set
rather than the loose working tree's. That contract only holds while
``GIT_INDEX_FILE`` stays out of
:data:`shared.git_sandbox.REPOSITORY_OVERRIDE_ENV_VARS`: the scrub is the
single choke point every production subprocess-spawning path routes through
(:func:`test_runner.subprocess_env_with_scrubbed_overrides`,
``run_tests._suite_command_env``, ``test_cache._default_runner``), so a
well-meaning future addition to that tuple would silently defeat the gate —
it would report a green verdict for a tree that excludes the commit's new
files.

These tests pin three things:

1. both scrub entry points preserve a set ``GIT_INDEX_FILE`` (name *and*
   value), while still removing genuine repository overrides;
2. a real child ``git ls-files`` spawned under the scrubbed suite environment
   honours the temporary index (the observable behaviour the gate relies on);
3. ``GIT_INDEX_FILE`` is not — and must not become — a scrubbed override.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from shared import git_sandbox as gs
from test_runner import subprocess_env_with_scrubbed_overrides

TEMP_INDEX_NAME = "would-be-commit.index"


def _minimal_suite_base(**extra: str) -> dict[str, str]:
    """A small, hermetic base environment for spawning ``git``.

    Only ``PATH`` and ``HOME`` are carried over: enough for ``git`` to run,
    with nothing ambient that could redirect the fixture at a real checkout
    (the scrub removes overrides regardless).
    """
    base = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    base.update(extra)
    return base


def _repo_with_pending_change_set(tmp_path: Path) -> Path:
    """A hermetic repo whose working tree diverges from HEAD in both directions.

    ``remove.py`` is tracked at HEAD but deleted in the working tree;
    ``new.py`` exists only in the working tree. The loose ``git ls-files``
    therefore reports ``remove.py`` and not ``new.py``, while a temporary
    index built from ``HEAD`` plus the change set reports the reverse — which
    makes index honouring observable in a single assertion pair.
    """
    repo = gs.init_repo(tmp_path / "index-repo", sandbox_root=tmp_path)
    (repo / "keep.py").write_text("keep = 1\n", encoding="utf-8")
    (repo / "remove.py").write_text("remove = 1\n", encoding="utf-8")
    gs.commit_all(repo, "seed tracked files")

    (repo / "new.py").write_text("new = 1\n", encoding="utf-8")
    (repo / "remove.py").unlink()
    return repo


def _build_temp_index(repo: Path, index_path: Path) -> None:
    """Stage the change set into *index_path*, leaving the real index alone."""
    env = {"GIT_INDEX_FILE": str(index_path)}
    gs.run_git(repo, ["read-tree", "HEAD"], check=True, env=env)
    gs.run_git(repo, ["add", "-A"], check=True, env=env)


def _git_ls_files(repo: Path, env: dict[str, str]) -> set[str]:
    """Run ``git ls-files`` in *repo* under *env*, returning the tracked set."""
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return set(proc.stdout.split())


# ---------------------------------------------------------------------------
# AC1 — GIT_INDEX_FILE survives both scrub entry points
# ---------------------------------------------------------------------------


def test_scrub_repository_overrides_preserves_git_index_file():
    """The shared scrub keeps ``GIT_INDEX_FILE`` and its value verbatim."""
    env = {
        "GIT_INDEX_FILE": "/tmp/would-be-commit.index",
        "GIT_DIR": "/tmp/leaked/.git",
        "PATH": "/usr/bin",
    }

    cleaned = gs.scrub_repository_overrides(env)

    assert cleaned["GIT_INDEX_FILE"] == "/tmp/would-be-commit.index"
    assert "GIT_DIR" not in cleaned
    assert cleaned["PATH"] == "/usr/bin"


def test_subprocess_env_with_scrubbed_overrides_preserves_git_index_file():
    """The suite subprocess env keeps ``GIT_INDEX_FILE`` for child git commands."""
    base = _minimal_suite_base(
        GIT_INDEX_FILE="/tmp/would-be-commit.index",
        GIT_DIR="/tmp/leaked/.git",
    )

    env = subprocess_env_with_scrubbed_overrides(base=base)

    assert env["GIT_INDEX_FILE"] == "/tmp/would-be-commit.index"
    assert "GIT_DIR" not in env
    # The PATH normalisation the runner also owns stays intact.
    assert env["PATH"].split(os.pathsep)[0] == os.path.expanduser("~/.local/bin")


# ---------------------------------------------------------------------------
# AC2 — a spawned `git ls-files` honours the temporary index
# ---------------------------------------------------------------------------


def test_child_git_ls_files_sees_the_temp_index_file_set(tmp_path):
    """Under the scrubbed suite env, ``git ls-files`` reflects the temp index.

    This is exactly the semantics the finish gate needs: the suite's tracked-
    file guards must see the file set the commit will produce, not the loose
    working tree's. Both invocations run as real child processes, so a scrub
    that dropped ``GIT_INDEX_FILE`` would make the two sets identical and the
    test fail.
    """
    repo = _repo_with_pending_change_set(tmp_path)
    index_path = tmp_path / TEMP_INDEX_NAME
    _build_temp_index(repo, index_path)

    loose = _git_ls_files(repo, subprocess_env_with_scrubbed_overrides(_minimal_suite_base()))
    staged = _git_ls_files(
        repo,
        subprocess_env_with_scrubbed_overrides(
            _minimal_suite_base(GIT_INDEX_FILE=str(index_path))
        ),
    )

    assert loose == {"keep.py", "remove.py"}, loose
    assert staged == {"keep.py", "new.py"}, staged


# ---------------------------------------------------------------------------
# AC3 — guard: GIT_INDEX_FILE must never join the scrub list
# ---------------------------------------------------------------------------


def test_git_index_file_is_not_a_repository_override():
    """``GIT_INDEX_FILE`` must stay out of ``REPOSITORY_OVERRIDE_ENV_VARS``.

    Adding it would make the finish gate run its suite against the loose
    working tree while reporting a verdict for the would-be-commit tree
    (SA-0MVL2NH1G0094FA2 F5) — the false-green class this pin exists to keep
    impossible.
    """
    assert "GIT_INDEX_FILE" not in gs.REPOSITORY_OVERRIDE_ENV_VARS, (
        "GIT_INDEX_FILE must not be scrubbed: the finish gate exports it so "
        "child `git ls-files` commands inside suite tests see the temporary "
        "would-be-commit index. Remove it from REPOSITORY_OVERRIDE_ENV_VARS, "
        "or update the gate's cache-determinism contract and this pin "
        "(tests/test_git_index_file_env.py) together."
    )
