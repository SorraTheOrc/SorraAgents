"""Finish-gate TMPDIR parity (SA-0MW3ENQY0001RBSW).

``implement.py`` must export the same disk-backed TMPDIR the test runner
resolves (F1, ``skill/shared/test_tmpdir.py``) into its finish-gate child
environments — the build subprocess, the in-process test-suite step and the
``git push`` child — so the pre-push hook's ``run_tests.py`` never runs on the
inode-starved tmpfs (SA-0MW2QED9M005FWOK). The export is scoped to children
only (the operator's parent shell environment is never mutated) and fail-open
(a resolution outage is logged and the push/build proceed unchanged).

Covered here (AC1–AC5):

* the push child env carries the resolved ``TMPDIR`` — an end-to-end sandbox
  push whose fake ``pre-push`` hook records the ``TMPDIR`` its process
  inherits (never a mock of ``subprocess.Popen``);
* the build subprocess env carries it (``run_build`` → ``run_cmd`` env);
* the in-process test step observes it for the duration of the
  ``guarded_run_all`` call and ``os.environ`` is restored afterwards — both
  for an absent and a pre-existing ambient ``TMPDIR`` (AC3);
* a forced resolver failure is fail-open: push and build proceed with the
  ambient env and an unchanged exit status (AC4).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

_SKILL_ROOT = REPO_ROOT / "skill"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.append(str(_SKILL_ROOT))

from shared import git_sandbox as gs

from skill.implement.scripts import implement

#: Canonical branch name of the fixture worktree (mirrors
#: ``test_implement_push_from_worktree.py``).
BRANCH = "wl-test-branch"


def _guarded_result(commands, scope="full", *, success=True):
    """A canned ``guarded_run_all`` result (the ``run_all`` shape)."""
    return {
        "success": success,
        "suites": {
            "all": {
                "returncode": 0 if success else 1,
                "success": success,
                "failures": [] if success else [{"test_name": "tests/test_x.py"}],
            }
        },
        "failures": [] if success else [{"test_name": "tests/test_x.py"}],
        "notices": [],
        "scope": scope,
        "type": "full",
        "resolved_scopes": [scope],
    }


@pytest.fixture
def resolved_tmpdir(tmp_path, monkeypatch):
    """Pin the F1 resolver to a fixed path and reset the module cache.

    Every test here controls resolution explicitly (never the host's real
    ``/var/tmp`` candidate), so the assertions are hermetic and independent
    of the mount layout or an ambient ``PI_TEST_TMPDIR`` override.
    ``monkeypatch`` also restores the resolution globals afterwards, so a
    resolution performed by one test can never leak into the next.
    """
    target = tmp_path / "disk-backed-tmp"
    target.mkdir()
    monkeypatch.setattr(
        implement,
        "_resolve_test_tmpdir",
        lambda: (target, "explicit test override"),
    )
    monkeypatch.setattr(implement, "_FINISH_TMPDIR_CACHE", None)
    monkeypatch.setattr(implement, "_FINISH_TMPDIR_RESOLVED", False)
    return target


def _pin_resolver_failure(monkeypatch):
    """Force the F1 resolver to fail (fail-open tests)."""
    def _boom():
        raise RuntimeError("simulated resolver outage")

    monkeypatch.setattr(implement, "_resolve_test_tmpdir", _boom)
    monkeypatch.setattr(implement, "_FINISH_TMPDIR_CACHE", None)
    monkeypatch.setattr(implement, "_FINISH_TMPDIR_RESOLVED", False)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A scratch ``origin``, a ``dev`` main checkout and a feature worktree."""
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    main = gs.init_repo(tmp_path / "main", default_branch="dev")
    (main / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    base = gs.commit_all(main, "base commit")
    gs.add_remote(main, "origin", str(remote))
    gs.run_git(main, ["push", "-q", "origin", "dev"], check=True)

    wt = gs.add_worktree(main, tmp_path / BRANCH, BRANCH)
    (wt / "feature.txt").write_text("feature\n", encoding="utf-8")
    feature = gs.commit_all(wt, "optional feature")

    return {
        "tmp": tmp_path,
        "remote": remote,
        "main": main,
        "wt": wt,
        "base": base,
        "feature": feature,
        "feature_short": gs.run_git(
            wt, ["rev-parse", "--short", "HEAD"], check=True
        ).stdout.strip(),
    }


def _install_tmpdir_recording_hook(sandbox: dict, out_file: Path) -> None:
    """Install a ``pre-push`` hook that records the ``TMPDIR`` it inherits."""
    git_dir = Path(
        gs.run_git(
            sandbox["main"], ["rev-parse", "--git-dir"], check=True
        ).stdout.strip()
    )
    if not git_dir.is_absolute():
        git_dir = sandbox["main"] / git_dir
    hook = git_dir / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/usr/bin/env python3\n"
        '"""Fake dev-push gate: record the TMPDIR this hook process inherits."""\n'
        "import os\n"
        f"open({str(out_file)!r}, 'w', encoding='utf-8').write(\n"
        "    os.environ.get('TMPDIR', '')\n"
        ")\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


# ---------------------------------------------------------------------------
# AC1 — the push child env carries the resolved TMPDIR
# ---------------------------------------------------------------------------


def test_push_child_env_carries_resolved_tmpdir(
    sandbox, resolved_tmpdir, tmp_path
):
    """The pre-push hook process inherits the resolved disk-backed TMPDIR.

    End-to-end: a real ``git push`` child runs the sandbox ``pre-push`` hook,
    which records the ``TMPDIR`` its process inherited from ``git push`` —
    exactly what ``run_tests.py`` would inherit in production.
    """
    recorded = tmp_path / "hook-tmpdir.txt"
    _install_tmpdir_recording_hook(sandbox, recorded)

    assert implement.git_push_to_dev(
        str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
    ) is True

    assert recorded.read_text(encoding="utf-8") == str(resolved_tmpdir), (
        "the pre-push hook (a child of git push) must inherit the resolved "
        "disk-backed TMPDIR, not the ambient one"
    )


def test_push_is_fail_open_when_resolver_raises(sandbox, tmp_path, monkeypatch):
    """A resolver failure is logged and the push proceeds unchanged (AC4)."""
    recorded = tmp_path / "hook-tmpdir.txt"
    _install_tmpdir_recording_hook(sandbox, recorded)
    _pin_resolver_failure(monkeypatch)

    assert implement.git_push_to_dev(
        str(sandbox["wt"]), BRANCH, sandbox["feature_short"]
    ) is True, "fail-open: the push must proceed despite the resolver error"

    ambient = os.environ.get("TMPDIR", "")
    assert recorded.read_text(encoding="utf-8") == ambient, (
        "fail-open: with no resolution the push child inherits the ambient "
        f"environment; hook recorded {recorded.read_text(encoding='utf-8')!r}, "
        f"ambient TMPDIR is {ambient!r}"
    )


# ---------------------------------------------------------------------------
# AC2 — the build subprocess env carries the resolved TMPDIR
# ---------------------------------------------------------------------------


def _repo_with_build_script(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts": {"build": "echo build"}}', encoding="utf-8"
    )
    return repo


def test_build_env_carries_resolved_tmpdir(tmp_path, resolved_tmpdir, monkeypatch):
    """``run_build`` passes the resolved TMPDIR into the build child env."""
    repo = _repo_with_build_script(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_cmd(cmd, cwd=None, capture=True, check=False, timeout=None,
                     env=None, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="built\n", stderr=""
        )

    monkeypatch.setattr(implement, "run_cmd", fake_run_cmd)

    result = implement.run_build(str(repo))

    assert result["success"] is True
    assert captured["cmd"] == ["npm", "run", "build"]
    assert captured["env"] == {"TMPDIR": str(resolved_tmpdir)}, (
        "the build subprocess env must carry the resolved disk-backed TMPDIR"
    )


def test_build_is_fail_open_when_resolver_raises(tmp_path, monkeypatch):
    """A resolver failure leaves the build env unchanged (fail-open, AC4)."""
    repo = _repo_with_build_script(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_cmd(cmd, cwd=None, capture=True, check=False, timeout=None,
                     env=None, **kwargs):
        captured["env"] = env
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="built\n", stderr=""
        )

    monkeypatch.setattr(implement, "run_cmd", fake_run_cmd)
    _pin_resolver_failure(monkeypatch)

    result = implement.run_build(str(repo))

    assert result["success"] is True, "fail-open: the build proceeds"
    assert captured["env"] is None, (
        "fail-open: no TMPDIR injection when resolution fails"
    )


# ---------------------------------------------------------------------------
# AC2/AC3 — the test-suite step sees the TMPDIR, scoped and restored
# ---------------------------------------------------------------------------


def test_test_step_exports_tmpdir_scoped_and_restores(
    tmp_path, resolved_tmpdir, monkeypatch
):
    """The in-process test step sees TMPDIR; ``os.environ`` is restored."""
    repo = tmp_path / "repo"
    repo.mkdir()
    seen: list[str | None] = []

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None,
                             scope="full", base_ref="origin/dev",
                             use_cache=True, **kwargs):
        seen.append(os.environ.get("TMPDIR"))
        return _guarded_result(commands, scope=scope)

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)
    assert os.environ.get("TMPDIR") != str(resolved_tmpdir), (
        "precondition: the resolved path must differ from the ambient TMPDIR"
    )

    result = implement._run_guarded_suite(
        ["pytest -q -r a --disable-warnings"],
        str(repo),
        scope="full",
        base_ref="origin/dev",
        tooling="pytest",
    )

    assert seen == [str(resolved_tmpdir)], (
        "the guarded_run_all call must observe the resolved disk-backed TMPDIR"
    )
    assert result["success"] is True
    assert os.environ.get("TMPDIR") != str(resolved_tmpdir), (
        "AC3: the export must not leak into the ambient environment after "
        "the call returns"
    )


def test_test_step_restores_a_pre_existing_ambient_tmpdir(
    tmp_path, resolved_tmpdir, monkeypatch
):
    """A pre-existing ambient TMPDIR is restored, never clobbered (AC3)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TMPDIR", "/pre-existing/ambient")

    def fake_guarded_run_all(cwd=None, timeout=600, *, commands=None,
                             scope="full", base_ref="origin/dev",
                             use_cache=True, **kwargs):
        assert os.environ.get("TMPDIR") == str(resolved_tmpdir)
        return _guarded_result(commands, scope=scope)

    monkeypatch.setattr(implement, "_guarded_run_all", fake_guarded_run_all)

    implement._run_guarded_suite(
        ["pytest -q -r a --disable-warnings"],
        str(repo),
        scope="full",
        base_ref="origin/dev",
        tooling="pytest",
    )

    assert os.environ.get("TMPDIR") == "/pre-existing/ambient", (
        "a pre-existing ambient TMPDIR must be restored after the call"
    )
