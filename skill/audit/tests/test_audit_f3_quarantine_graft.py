"""Regression tests: F3 quarantine never triages, and its cache is grafted.

Two invariants of the mid-run fast-forward mitigation
(WL-0MVSPZ1FH000RWET):

* a full-suite run whose checkout moved mid-execution is a mixed-tree verdict
  — it is quarantined, reported red, and **never** triaged into a critical
  ``test-failure`` item; the suppression is recorded explicitly;
* the entries an isolated F3 run writes are grafted into the main checkout's
  cache before the execution worktree (and its cache) is removed, so a later
  read-only audit serves the verdict instead of re-executing the suite.

Both are driven through the production ``_run_tests_via_test_skill`` with only
the suite command resolution, the cache runner and the graft helper mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _patch_suite(monkeypatch, results):
    """Resolve one suite command and return *results* from ``run_cached``."""
    monkeypatch.setattr(
        audit_runner, "full_suite_commands", lambda root: ["pytest -q"]
    )
    monkeypatch.setattr(
        audit_runner, "suite_timeout_per_command", lambda root: 60
    )
    seen = []

    def fake_run_cached(command, **kwargs):
        seen.append(command)
        return results(command)

    monkeypatch.setattr(audit_runner, "run_cached", fake_run_cached)
    return seen


def test_moved_run_is_quarantined_not_triaged(monkeypatch, tmp_path):
    """A ``state_changed_during_run`` verdict stays red and is never triaged."""
    movement = {
        "notice_code": "state_changed_during_run",
        "checkout": str(tmp_path),
        "before_commit": "a" * 40,
        "after_commit": "b" * 40,
        "message": "checkout moved mid-run",
    }
    _patch_suite(
        monkeypatch,
        lambda command: {
            "stdout": "1 failed",
            "stderr": "",
            "exit_code": 75,
            "command": command,
            "git_state": "",
            "cached": False,
            "state_changed_during_run": movement,
        },
    )

    triage_calls = []

    def fail_if_triaged(payload):
        triage_calls.append(payload)
        return {"created": True}

    import triage.scripts.check_or_create as check_or_create_module

    monkeypatch.setattr(
        check_or_create_module, "check_or_create", fail_if_triaged
    )

    result = audit_runner._run_tests_via_test_skill(str(tmp_path))

    assert result["success"] is False
    assert result["failures"] == [], "a mixed-tree verdict must not be parsed"
    assert result["triaged"] == []
    assert triage_calls == [], "a moving checkout must never mint a test-failure"
    assert len(result["suppressed_phantoms"]) == 1
    assert (
        result["suppressed_phantoms"][0]["notice_code"]
        == "state_changed_during_run"
    )
    assert result["state_changed_during_run"][0]["suite_command"] == "pytest -q"


def test_isolated_run_entries_are_grafted_into_the_main_checkout(
    monkeypatch, tmp_path
):
    """``mirror_to`` grafts the execution worktree's cache into the caller's."""
    _patch_suite(
        monkeypatch,
        lambda command: {
            "stdout": "3 passed",
            "stderr": "",
            "exit_code": 0,
            "command": command,
            "git_state": "state",
            "cached": False,
            "state_changed_during_run": None,
        },
    )

    graft_calls = []

    def fake_mirror(source, destination):
        graft_calls.append((str(source), str(destination)))
        return ["entry"]

    monkeypatch.setattr(audit_runner, "mirror_cache_entries", fake_mirror)

    execution_worktree = tmp_path / "exec-worktree"
    main_checkout = tmp_path / "main"
    execution_worktree.mkdir()
    main_checkout.mkdir()

    result = audit_runner._run_tests_via_test_skill(
        str(execution_worktree), mirror_to=str(main_checkout)
    )

    assert result["success"] is True
    assert graft_calls == [
        (str(execution_worktree.resolve()), str(main_checkout.resolve()))
    ]


def test_no_graft_without_mirror_to(monkeypatch, tmp_path):
    """The default (no ``mirror_to``) leaves cache grafting untouched."""
    _patch_suite(
        monkeypatch,
        lambda command: {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "command": command,
            "git_state": "",
            "cached": False,
            "state_changed_during_run": None,
        },
    )
    graft_calls = []
    monkeypatch.setattr(
        audit_runner,
        "mirror_cache_entries",
        lambda source, destination: graft_calls.append((source, destination)) or [],
    )

    audit_runner._run_tests_via_test_skill(str(tmp_path))

    assert graft_calls == []
