"""Abort-recovery tests for the audit runner (SA-0MTRH8C5O005URGR).

Covers the requirement that a work item is never left in ``in_progress``
when the audit runner is aborted:

  * AC1 — the pre-audit state is written to a state file before the
    ``in_progress`` claim, so a parent process (check-audit-gate.js /
    run-release) can restore the item when the child is SIGKILLed.
  * AC2 — explicit SIGTERM/SIGINT handlers restore the pre-audit status
    and remove the state file (Python's default SIGTERM disposition
    skips ``finally`` blocks; only SIGINT raises ``KeyboardInterrupt``).
  * AC3 — after any termination path the item is not left in
    ``in_progress``.
  * AC4 — existing audit tests continue to pass (covered by the suite).

These tests exercise the production helpers directly (no re-implementation
of the logic) plus an end-to-end ``cmd_issue`` ordering assertion.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.wl_helpers import stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _make_runner(updates, status="completed", stage="in_review",
                 description="", children=None, fail_children_show=False,
                 parent_id=None, on_update=None):
    """Build a mock runner recording every ``wl update`` command.

    *on_update* (optional) is called with the command list BEFORE the
    recorded update is returned — used to assert state-file ordering.
    """
    mock_runner = mock.MagicMock()

    def _side_effect(cmd):
        cmd_str = " ".join(cmd)

        if "update" in cmd_str:
            if on_update is not None:
                on_update(list(cmd))
            updates.append(list(cmd))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        if "show" in cmd_str and "--children" not in cmd_str:
            wi = {"id": "TEST-1", "status": status, "stage": stage}
            if parent_id is not None:
                wi["parentId"] = parent_id
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "workItem": wi}),
                stderr="",
            )

        if "--children" in cmd_str:
            if fail_children_show:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "description": description,
                        "status": status,
                        "stage": stage,
                    },
                    "children": children or [],
                }),
                stderr="",
            )

        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
    return mock_runner


def _run_issue(updates, verdict_report, runner=None, **runner_kwargs):
    """Run cmd_issue with a controlled report verdict and no subprocesses."""
    mock_runner = runner or _make_runner(updates, **runner_kwargs)
    with (
        mock.patch.object(
            audit_runner, "_assemble_issue_report",
            return_value=verdict_report,
        ),
        mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [], "fixes_applied": 0},
        ),
    ):
        return audit_runner.cmd_issue(
            "TEST-1", persist=False, force=True, runner=mock_runner,
        )


def _state_path(tmp_path: Path, issue_id: str = "TEST-1") -> Path:
    return tmp_path / f".audit_state_{issue_id}.json"


# ----------------------------------------------------------------------
# State file helpers
# ----------------------------------------------------------------------

class TestStateFileHelpers:
    """Unit tests for the abort-recovery state-file helpers."""

    def test_write_state_file_creates_json_with_pre_audit_state(self, tmp_path,
                                                               monkeypatch):
        """The state file records the captured pre-audit status/stage."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        path = audit_runner._write_audit_state_file(
            "TEST-1", "completed", "in_review",
        )
        assert Path(path).exists()
        data = json.loads(Path(path).read_text())
        assert data == {
            "issue_id": "TEST-1",
            "original_status": "completed",
            "original_stage": "in_review",
        }

    def test_resolve_state_path_honours_env_dir(self, tmp_path, monkeypatch):
        """AUDIT_ABSTATE_FILE redirects the state file to the given dir."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        path = audit_runner._resolve_state_path("TEST-1")
        assert path == str(tmp_path / ".audit_state_TEST-1.json")

    def test_resolve_state_path_defaults_to_tmp(self, monkeypatch):
        """Without the env override the state file lands in /tmp."""
        monkeypatch.delenv(audit_runner._AUDIT_STATE_FILE_ENV, raising=False)
        path = audit_runner._resolve_state_path("TEST-1")
        assert path == "/tmp/.audit_state_TEST-1.json"

    def test_resolve_state_path_prefers_worklog_dir(self, tmp_path, monkeypatch):
        """An explicit worklog_dir is used when the env override is unset."""
        monkeypatch.delenv(audit_runner._AUDIT_STATE_FILE_ENV, raising=False)
        path = audit_runner._resolve_state_path(
            "TEST-1", worklog_dir=str(tmp_path),
        )
        assert path == str(tmp_path / ".audit_state_TEST-1.json")

    def test_remove_state_file_is_best_effort(self, tmp_path):
        """Removing a missing state file never raises."""
        audit_runner._remove_audit_state_file(str(tmp_path / "absent.json"))
        audit_runner._remove_audit_state_file("")

    def test_remove_state_file_deletes_existing(self, tmp_path):
        """An existing state file is deleted."""
        path = tmp_path / ".audit_state_TEST-1.json"
        path.write_text("{}")
        audit_runner._remove_audit_state_file(str(path))
        assert not path.exists()


# ----------------------------------------------------------------------
# Restore helper
# ----------------------------------------------------------------------

class TestRestoreWorkItemStatus:
    """Unit tests for the lightweight signal-handler restore helper."""

    def test_restore_issues_wl_update_with_captured_state(self):
        """The restore issues a wl update with the pre-audit status/stage."""
        with mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = audit_runner._restore_work_item_status(
                "TEST-1", "completed", "in_review",
            )
        assert rc == 0
        cmd = run.call_args.args[0]
        assert cmd[0] == "wl"
        assert "update" in cmd
        assert "TEST-1" in cmd
        idx = cmd.index("--status")
        assert cmd[idx + 1] == "completed"
        idx = cmd.index("--stage")
        assert cmd[idx + 1] == "in_review"
        assert "--assignee" in cmd

    def test_restore_coerces_invalid_status_stage_combo(self, capsys):
        """An invalid captured combo is coerced to a valid one."""
        with mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = audit_runner._restore_work_item_status(
                "TEST-1", "blocked", "in_review",
            )
        assert rc == 0
        cmd = run.call_args.args[0]
        idx = cmd.index("--stage")
        assert cmd[idx + 1] == "plan_complete"

    def test_restore_legacy_hybrid_in_progress_in_review_recovers_to_completed(self, capsys):
        """WL-0MV85WE7Q00044P9: abort-recovery of the legacy audit-claim
        hybrid in-progress/in_review restores to completed/in_review (never
        in-progress/plan_complete) and always clears the assignee."""
        with mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = audit_runner._restore_work_item_status(
                "TEST-1", "in-progress", "in_review",
            )
        assert rc == 0
        cmd = run.call_args.args[0]
        idx = cmd.index("--status")
        assert cmd[idx + 1] == "completed"
        idx = cmd.index("--stage")
        assert cmd[idx + 1] == "in_review"
        idx = cmd.index("--assignee")
        assert cmd[idx + 1] == ""

    def test_restore_injects_worklog_dir_when_supplied(self, tmp_path):
        """An explicit worklog_dir is forwarded to the wl invocation."""
        with mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            rc = audit_runner._restore_work_item_status(
                "TEST-1", "completed", "in_review",
                worklog_dir=str(tmp_path),
            )
        assert rc == 0
        cmd = run.call_args.args[0]
        assert "--worklog-dir" in cmd

    def test_restore_returns_nonzero_on_subprocess_failure(self):
        """A subprocess exception is swallowed and reported as non-zero."""
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            rc = audit_runner._restore_work_item_status(
                "TEST-1", "completed", "in_review",
            )
        assert rc == 1


# ----------------------------------------------------------------------
# Signal handlers (AC2)
# ----------------------------------------------------------------------

class TestAbortSignalHandlers:
    """Unit tests for the SIGTERM/SIGINT abort handlers (AC2)."""

    def _capture_handlers(self, state_path, original_status="completed",
                          original_stage="in_review"):
        """Register handlers under a mocked ``signal.signal`` and return them."""
        registered: dict[int, object] = {}

        def _fake_signal(signum, handler):
            # Only record real handler callables — deactivation resets the
            # disposition to SIG_DFL, which is not callable.
            if callable(handler):
                registered[signum] = handler
            return signal.SIG_DFL

        with mock.patch.object(
            audit_runner.signal, "signal", side_effect=_fake_signal,
        ):
            audit_runner._register_abort_signal_handlers(
                "TEST-1", original_status, original_stage, str(state_path),
            )
        return registered

    def test_registers_sigterm_and_sigint(self, tmp_path):
        """Both SIGTERM and SIGINT handlers are registered."""
        registered = self._capture_handlers(tmp_path / "state.json")
        assert signal.SIGTERM in registered
        assert signal.SIGINT in registered

    def test_handler_restores_pre_audit_status(self, tmp_path):
        """Invoking the handler restores the captured pre-audit status."""
        state_path = tmp_path / ".audit_state_TEST-1.json"
        state_path.write_text("{}")
        registered = self._capture_handlers(
            state_path, "completed", "in_review",
        )
        handler = registered[signal.SIGTERM]

        with (
            mock.patch("subprocess.run") as run,
            mock.patch.object(audit_runner.os, "kill"),
            mock.patch.object(audit_runner.signal, "signal"),
        ):
            run.return_value = SimpleNamespace(returncode=0)
            handler(signal.SIGTERM, None)

        cmd = run.call_args.args[0]
        assert "TEST-1" in cmd
        idx = cmd.index("--status")
        assert cmd[idx + 1] == "completed"
        idx = cmd.index("--stage")
        assert cmd[idx + 1] == "in_review"

    def test_handler_removes_state_file(self, tmp_path):
        """The handler removes the state file so the parent takes no action."""
        state_path = tmp_path / ".audit_state_TEST-1.json"
        state_path.write_text("{}")
        registered = self._capture_handlers(state_path)
        handler = registered[signal.SIGTERM]

        with (
            mock.patch("subprocess.run") as run,
            mock.patch.object(audit_runner.os, "kill"),
            mock.patch.object(audit_runner.signal, "signal"),
        ):
            run.return_value = SimpleNamespace(returncode=0)
            handler(signal.SIGTERM, None)

        assert not state_path.exists()

    def test_handler_restores_default_disposition_before_resignal(self, tmp_path):
        """The handler resets SIG_DFL then re-raises to avoid recursion."""
        state_path = tmp_path / ".audit_state_TEST-1.json"
        state_path.write_text("{}")
        registered = self._capture_handlers(state_path)
        handler = registered[signal.SIGTERM]

        signal_calls: list = []

        def _fake_signal(signum, disposition):
            signal_calls.append((signum, disposition))
            return signal.SIG_DFL

        with (
            mock.patch("subprocess.run") as run,
            mock.patch.object(audit_runner.os, "kill") as kill,
            mock.patch.object(
                audit_runner.signal, "signal", side_effect=_fake_signal,
            ),
        ):
            run.return_value = SimpleNamespace(returncode=0)
            handler(signal.SIGTERM, None)

        assert (signal.SIGTERM, signal.SIG_DFL) in signal_calls
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_handler_survives_restore_failure(self, tmp_path):
        """A failing restore never escapes the handler (best-effort)."""
        state_path = tmp_path / ".audit_state_TEST-1.json"
        state_path.write_text("{}")
        registered = self._capture_handlers(state_path)
        handler = registered[signal.SIGTERM]

        with (
            mock.patch("subprocess.run", side_effect=OSError("boom")),
            mock.patch.object(audit_runner.os, "kill"),
            mock.patch.object(audit_runner.signal, "signal"),
        ):
            # Must not raise.
            handler(signal.SIGTERM, None)

        assert not state_path.exists()

    def test_registration_is_main_thread_guarded(self, tmp_path):
        """A ValueError from signal.signal (non-main thread) is swallowed."""
        with mock.patch.object(
            audit_runner.signal, "signal",
            side_effect=ValueError("signal only works in main thread"),
        ):
            # Must not raise.
            audit_runner._register_abort_signal_handlers(
                "TEST-1", "completed", "in_review",
                str(tmp_path / "state.json"),
            )

    def test_deactivate_makes_handler_inert(self, tmp_path):
        """After deactivation the handler must not restore the item.

        This is the guard against a stale handler undoing a legitimate
        terminal transition once the audit has completed.
        """
        state_path = tmp_path / ".audit_state_TEST-1.json"
        state_path.write_text("{\"issue_id\": \"TEST-1\"}")
        registered: dict[int, object] = {}

        def _fake_signal(signum, handler):
            # Only record real handler callables — deactivation resets the
            # disposition to SIG_DFL, which is not callable.
            if callable(handler):
                registered[signum] = handler
            return signal.SIG_DFL

        with mock.patch.object(
            audit_runner.signal, "signal", side_effect=_fake_signal,
        ):
            deactivate = audit_runner._register_abort_signal_handlers(
                "TEST-1", "completed", "in_review", str(state_path),
            )
            deactivate()

        handler = registered[signal.SIGTERM]
        with (
            mock.patch("subprocess.run") as run,
            mock.patch.object(audit_runner.os, "kill"),
            mock.patch.object(audit_runner.signal, "signal"),
        ):
            handler(signal.SIGTERM, None)

        # No restore was issued; the process still re-signalled.
        run.assert_not_called()

    def test_deactivate_resets_disposition_to_default(self, tmp_path):
        """Deactivation resets SIGTERM/SIGINT to SIG_DFL."""
        signal_calls: list = []

        def _fake_signal(signum, disposition):
            signal_calls.append((signum, disposition))
            return signal.SIG_DFL

        with mock.patch.object(
            audit_runner.signal, "signal", side_effect=_fake_signal,
        ):
            deactivate = audit_runner._register_abort_signal_handlers(
                "TEST-1", "completed", "in_review",
                str(tmp_path / "state.json"),
            )
            deactivate()

        assert (signal.SIGTERM, signal.SIG_DFL) in signal_calls
        assert (signal.SIGINT, signal.SIG_DFL) in signal_calls


# ----------------------------------------------------------------------
# End-to-end cmd_issue ordering (AC1/AC3)
# ----------------------------------------------------------------------

class TestCmdIssueAbortRecovery:
    """End-to-end state-file lifecycle around cmd_issue (AC1/AC3)."""

    def test_state_file_written_before_in_progress_claim(self, tmp_path,
                                                         monkeypatch):
        """AC1: the state file exists when the in_progress claim is issued."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        state_path = _state_path(tmp_path)
        seen_at_claim: list[bool] = []

        def _on_update(cmd):
            if "in_progress" in cmd:
                seen_at_claim.append(state_path.exists())

        updates: list = []
        runner = _make_runner(updates, on_update=_on_update)
        _run_issue(
            updates,
            "Ready to close: Yes\n\n## Summary\nAll met.",
            runner=runner,
        )
        assert seen_at_claim, "the in_progress claim was never issued"
        assert all(seen_at_claim), (
            "state file must exist before the in_progress claim"
        )

    def test_state_file_records_pre_audit_state(self, tmp_path, monkeypatch):
        """AC1: the state file records the pre-audit status/stage."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        state_path = _state_path(tmp_path)
        captured: list[dict] = []

        def _on_update(cmd):
            if "in_progress" in cmd and state_path.exists():
                captured.append(json.loads(state_path.read_text()))

        updates: list = []
        runner = _make_runner(
            updates, status="completed", stage="in_review", on_update=_on_update,
        )
        _run_issue(
            updates,
            "Ready to close: Yes\n\n## Summary\nAll met.",
            runner=runner,
        )
        assert captured
        assert captured[0]["original_status"] == "completed"
        assert captured[0]["original_stage"] == "in_review"

    def test_state_file_removed_on_successful_completion(self, tmp_path,
                                                         monkeypatch):
        """AC3: a normal run removes the state file (parent takes no action)."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        state_path = _state_path(tmp_path)
        updates: list = []
        _run_issue(
            updates,
            "Ready to close: Yes\n\n## Summary\nAll met.",
            status="completed", stage="in_review",
        )
        assert not state_path.exists()

    def test_state_file_removed_on_failure(self, tmp_path, monkeypatch):
        """AC3: a failing run removes the state file via the finally block."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        state_path = _state_path(tmp_path)
        updates: list = []
        _run_issue(
            updates,
            "Ready to close: Yes",
            status="completed", stage="in_review",
            fail_children_show=True,
        )
        assert not state_path.exists()

    def test_cmd_issue_deactivates_abort_handlers_on_completion(
            self, tmp_path, monkeypatch):
        """AC2/AC3: the handlers are disabled once the audit settles.

        A signal arriving after a successful audit must not restore the
        item to its pre-audit state (which would undo the completed/
        in_review advance).
        """
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        registered: dict[int, object] = {}

        def _fake_signal(signum, handler):
            # Only record real handler callables — deactivation resets the
            # disposition to SIG_DFL, which is not callable.
            if callable(handler):
                registered[signum] = handler
            return signal.SIG_DFL

        updates: list = []
        with mock.patch.object(
            audit_runner.signal, "signal", side_effect=_fake_signal,
        ):
            _run_issue(
                updates,
                "Ready to close: Yes\n\n## Summary\nAll met.",
                status="completed", stage="in_review",
            )

        # The handler captured during the run is now inert: invoking it
        # issues no further wl update.
        updates_before = len(updates)
        handler = registered[signal.SIGTERM]
        with (
            mock.patch("subprocess.run") as run,
            mock.patch.object(audit_runner.os, "kill"),
            mock.patch.object(audit_runner.signal, "signal"),
        ):
            handler(signal.SIGTERM, None)
        run.assert_not_called()
        assert len(updates) == updates_before

    def test_no_state_file_on_freshness_skip(self, tmp_path, monkeypatch):
        """A freshness skip returns before the claim — no state file is left."""
        monkeypatch.setenv(audit_runner._AUDIT_STATE_FILE_ENV, str(tmp_path))
        state_path = _state_path(tmp_path)
        updates: list = []
        runner = _make_runner(updates)
        with (
            mock.patch.object(
                audit_runner, "_check_audit_freshness",
                return_value="Ready to close: Yes",
            ),
            mock.patch.object(
                audit_runner, "_fetch_audited_at", return_value=None,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=runner,
            )
        assert rc == 0
        assert not state_path.exists()
