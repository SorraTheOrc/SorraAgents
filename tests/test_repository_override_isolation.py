"""Regression tests: a poisoned repository-override env cannot reach real git.

Tracer-bullet coverage for the 2026-09-26 leaked-``GIT_DIR`` incident
(SA-0MUIA3OE40001QJX). The *production* test-execution paths —
``test_cache._default_runner``, ``run_tests._run_cmd`` / ``_cached_runner`` and
``audit_runner._run_tests_via_test_skill`` — previously inherited the full
process environment (including a leaked ``GIT_DIR``) into every test
subprocess. Git honours ``GIT_DIR`` over ``cwd``, so a fixture that ran
``git init`` / ``git commit`` / ``git push`` from an otherwise-isolated
``tmp_path`` operated on the leaked target repository instead.

Each test drives a production path with ``GIT_DIR`` pointed at a throwaway
*victim* repository and asserts the victim is byte-for-byte unmoved. The tests
were introduced as an ``xfail(strict=True)`` tracer bullet in F1 (reproducing
the leak against pre-fix code) and F3 removed the marker once the shared scrub
was wired into every path — they are now hard regression guards.

Design constraints (SA-0MUG0WFP8008WN63):

- stdlib + pytest only; no network;
- the *live* checkout is never the target — the victim is a ``tmp_path`` repo
  snapshotted with ``shared.git_sandbox`` plumbing that is itself hermetic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared import git_sandbox as gs
from test_cache import _default_runner


def _victim(tmp_path: Path) -> Path:
    """A throwaway repo with ``dev`` and a bare ``origin`` tracking ``dev``."""
    victim = gs.init_repo(tmp_path / "victim", default_branch="dev")
    (victim / "tracked.txt").write_text("base\n", encoding="utf-8")
    gs.commit_all(victim, "base")
    remote = gs.init_bare_remote(tmp_path / "origin.git")
    gs.add_remote(victim, "origin", str(remote))
    gs.run_git(victim, ["push", "-q", "origin", "dev"], check=True)
    return victim


def _poison(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Point GIT_DIR at the victim (the incident's leak mechanism)."""
    monkeypatch.setenv("GIT_DIR", str(victim / ".git"))


def _victim_snapshot(victim: Path) -> dict:
    return gs.snapshot_repo_state(victim)


def _assert_victim_unmoved(before: dict, victim: Path) -> None:
    diff = gs.diff_snapshots(before, _victim_snapshot(victim))
    assert not diff["changed"], (
        "a poisoned repository-override env reached the victim repo:\n"
        + gs.describe_diff(diff)
    )


def test_default_runner_scrubs_repository_overrides(tmp_path, monkeypatch):
    """``test_cache._default_runner`` must not spill a poisoned GIT_DIR."""
    victim = _victim(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    before = _victim_snapshot(victim)

    _poison(monkeypatch, victim)
    _default_runner("git commit --allow-empty -m leaked", str(sandbox), 30)

    _assert_victim_unmoved(before, victim)


def test_run_tests_run_cmd_scrubs_repository_overrides(tmp_path, monkeypatch):
    """``run_tests._run_cmd`` must pass a scrubbed environment to subprocesses."""
    import test.scripts.run_tests as rt

    victim = _victim(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    before = _victim_snapshot(victim)

    _poison(monkeypatch, victim)
    rt._run_cmd(
        ["git", "commit", "--allow-empty", "-m", "leaked"], sandbox, timeout=30
    )

    _assert_victim_unmoved(before, victim)


def test_run_tests_cached_runner_scrubs_repository_overrides(tmp_path, monkeypatch):
    """``run_tests._cached_runner`` must scrub before spawning the suite."""
    import test.scripts.run_tests as rt

    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(tmp_path / "semaphores"))
    victim = _victim(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    before = _victim_snapshot(victim)

    _poison(monkeypatch, victim)
    rt._cached_runner("git commit --allow-empty -m leaked", str(sandbox), 30)

    _assert_victim_unmoved(before, victim)


def test_audit_runner_suite_env_has_no_repository_overrides(tmp_path, monkeypatch):
    """The audit suite invocation must not propagate override vars downstream."""
    import test_cache
    from audit.scripts import audit_runner

    victim = _victim(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    captured_envs: list[dict[str, str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003 - mirrors subprocess.run
        if kwargs.get("env") is not None:
            captured_envs.append(dict(kwargs["env"]))
        return SimpleNamespace(stdout="1 passed in 0.01s\n", stderr="", returncode=0)

    monkeypatch.setattr(test_cache.subprocess, "run", fake_run)
    monkeypatch.setattr(audit_runner, "full_suite_commands", lambda root: ["pytest -q"])
    monkeypatch.setattr(audit_runner, "suite_timeout_per_command", lambda root: 60)

    _poison(monkeypatch, victim)
    audit_runner._run_tests_via_test_skill(str(sandbox))

    assert captured_envs, "no suite subprocess env was captured"
    assert all("GIT_DIR" not in env for env in captured_envs), (
        "audit suite subprocess inherited a poisoned GIT_DIR"
    )


def test_scrub_diagnostic_is_one_shot_and_silent_when_clean(tmp_path, monkeypatch, capsys):
    """F3 AC4: the scrub diagnostic fires once when vars are present, else never."""
    from test_runner import _reset_scrub_diagnostic, log_repository_override_scrub

    _reset_scrub_diagnostic()
    monkeypatch.setenv("GIT_DIR", str(tmp_path))
    assert log_repository_override_scrub() == ("GIT_DIR",)
    log_repository_override_scrub()  # second call must not re-emit
    assert capsys.readouterr().err.count("scrubbed repository-override") == 1

    _reset_scrub_diagnostic()
    monkeypatch.delenv("GIT_DIR", raising=False)
    assert log_repository_override_scrub() == ()
    assert "scrubbed repository-override" not in capsys.readouterr().err


def test_audit_pi_launch_scrubs_repository_overrides(tmp_path, monkeypatch):
    """F3 AC3: the audit pi-launch subprocess must not inherit override vars."""
    import json as _json

    from audit.scripts import audit_runner

    inner = _json.dumps({"verdict": "met", "evidence": "ok"})
    stream = _json.dumps({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_end", "content": inner},
    })

    class _FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):  # noqa: ANN001, ANN201
            return stream, ""

        def kill(self):  # noqa: ANN201
            pass

    captured: dict = {}

    def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN003, ANN201
        captured["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(audit_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("GIT_DIR", "/tmp/leaked")
    # Isolate the audit semaphore so the launch does not contend with host
    # audits, and keep any bounded wait short.
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(tmp_path / "semaphores"))
    monkeypatch.setenv("AUDIT_LOCK_TIMEOUT", "1")
    monkeypatch.setenv("AUDIT_QUEUE_TIMEOUT", "1")

    result = audit_runner._call_pi("prompt", model="m", pi_bin="pi")

    assert result.get("verdict") == "met"
    assert captured["env"] is not None, "pi launch did not pass an explicit env"
    assert "GIT_DIR" not in captured["env"]


def test_poisoned_env_cannot_move_dev_or_origin_dev(tmp_path, monkeypatch):
    """No fixture may move ``refs/heads/dev`` / ``refs/remotes/origin/dev``."""
    victim = _victim(tmp_path)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    before = _victim_snapshot(victim)
    assert "refs/heads/dev" in before["refs"]
    assert "refs/remotes/origin/dev" in before["refs"]

    _poison(monkeypatch, victim)
    _default_runner("git commit --allow-empty -m leaked", str(sandbox), 30)
    _default_runner("git push -q origin dev", str(sandbox), 30)

    after = _victim_snapshot(victim)
    _assert_victim_unmoved(before, victim)
    assert after["refs"]["refs/heads/dev"] == before["refs"]["refs/heads/dev"]
    assert (
        after["refs"]["refs/remotes/origin/dev"]
        == before["refs"]["refs/remotes/origin/dev"]
    )
