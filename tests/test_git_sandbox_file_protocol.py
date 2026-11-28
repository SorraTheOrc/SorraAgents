"""Fixture git commands allow local file transport for submodules.

Work item: **SA-0MWZY32SU00771VB**.

Git 2.38+ defaults ``protocol.file.allow=user``. A fixture repository is
created under ``tmp_path`` and a *second* fixture repository is added to it as
a submodule by local path, so the sandbox's ``git submodule add`` is refused
with::

    fatal: transport 'file' not allowed

``shared.git_sandbox.run_git`` supplies the per-command opt-in
(``-c protocol.file.allow=always``) — hermetic, with no ambient or global git
configuration read or written. These tests pin both the command shape and the
behaviour.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared import git_sandbox as gs


def test_run_git_opts_into_file_protocol(monkeypatch, tmp_path):
    """Every fixture git command carries the local-file-transport opt-in."""
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gs.subprocess, "run", _fake_run)
    gs.run_git(tmp_path, ["status"])

    cmd = captured["cmd"]
    pairs = set(itertools.pairwise(cmd))
    for expected in (
        ("-c", f"user.name={gs.FIXTURE_AUTHOR_NAME}"),
        ("-c", f"user.email={gs.FIXTURE_AUTHOR_EMAIL}"),
        ("-c", "protocol.file.allow=always"),
    ):
        assert expected in pairs


def test_submodule_add_from_local_path_succeeds(tmp_path):
    """The behavioural regression: a local-path submodule add now succeeds."""
    source = gs.init_repo(tmp_path / "engine", sandbox_root=tmp_path)
    (source / "marker.txt").write_text("engine\n", encoding="utf-8")
    gs.commit_all(source, "engine base")

    target = gs.init_repo(tmp_path / "consumer", sandbox_root=tmp_path)
    (target / "readme.txt").write_text("consumer\n", encoding="utf-8")
    gs.commit_all(target, "consumer base")

    gs.run_git(
        target,
        ["submodule", "add", "-q", str(source), "core"],
        check=True,
    )

    assert (target / "core" / "marker.txt").is_file()
    status = gs.run_git(target, ["status", "--porcelain"], check=True)
    assert "core" in status.stdout


def test_submodule_add_without_opt_in_would_fail(tmp_path):
    """Guard the premise: plain git refuses the same clone in this environment.

    If a future git default ever allows file transport implicitly, this test
    becomes informative rather than failing — it asserts only that the
    sandbox's own command *is* the one that opts in.
    """
    source = gs.init_repo(tmp_path / "engine", sandbox_root=tmp_path)
    (source / "marker.txt").write_text("engine\n", encoding="utf-8")
    gs.commit_all(source, "engine base")

    target = gs.init_repo(tmp_path / "consumer", sandbox_root=tmp_path)
    (target / "readme.txt").write_text("consumer\n", encoding="utf-8")
    gs.commit_all(target, "consumer base")

    plain = subprocess.run(
        ["git", "submodule", "add", "-q", str(source), "core"],
        cwd=str(target),
        env=gs.fixture_git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if plain.returncode == 0:
        pytest.skip("git allows file transport by default in this environment")
    assert "transport 'file' not allowed" in plain.stderr
