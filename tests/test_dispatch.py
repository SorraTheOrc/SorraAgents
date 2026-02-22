"""Tests for ampa.engine.dispatch — Dispatcher protocol, OpenCodeRunDispatcher, DryRunDispatcher."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from ampa.engine.dispatch import (
    DispatchRecord,
    DispatchResult,
    Dispatcher,
    DryRunDispatcher,
    OpenCodeRunDispatcher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXED_TIME = datetime(2026, 2, 22, 5, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return FIXED_TIME


# ---------------------------------------------------------------------------
# DispatchResult tests
# ---------------------------------------------------------------------------


class TestDispatchResult:
    """Tests for DispatchResult data class."""

    def test_successful_result(self):
        r = DispatchResult(
            success=True,
            command='opencode run "/intake WL-1"',
            work_item_id="WL-1",
            timestamp=FIXED_TIME,
            pid=12345,
        )
        assert r.success is True
        assert r.pid == 12345
        assert r.error is None
        assert r.work_item_id == "WL-1"

    def test_failed_result(self):
        r = DispatchResult(
            success=False,
            command='opencode run "/intake WL-2"',
            work_item_id="WL-2",
            timestamp=FIXED_TIME,
            error="FileNotFoundError: opencode not found",
        )
        assert r.success is False
        assert r.pid is None
        assert r.error == "FileNotFoundError: opencode not found"

    def test_summary_success(self):
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-3",
            timestamp=FIXED_TIME,
            pid=999,
        )
        s = r.summary
        assert "WL-3" in s
        assert "pid=999" in s
        assert "Dispatched" in s

    def test_summary_failure(self):
        r = DispatchResult(
            success=False,
            command="cmd",
            work_item_id="WL-4",
            timestamp=FIXED_TIME,
            error="boom",
        )
        s = r.summary
        assert "WL-4" in s
        assert "boom" in s
        assert "failed" in s

    def test_frozen(self):
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-5",
            timestamp=FIXED_TIME,
        )
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify both dispatchers satisfy the Dispatcher protocol."""

    def test_opencode_run_dispatcher_is_dispatcher(self):
        d = OpenCodeRunDispatcher()
        assert isinstance(d, Dispatcher)

    def test_dry_run_dispatcher_is_dispatcher(self):
        d = DryRunDispatcher()
        assert isinstance(d, Dispatcher)


# ---------------------------------------------------------------------------
# OpenCodeRunDispatcher tests
# ---------------------------------------------------------------------------


class TestOpenCodeRunDispatcherSuccess:
    """Tests for successful subprocess spawning."""

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_successful_spawn(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_popen.return_value = mock_proc

        d = OpenCodeRunDispatcher(cwd="/tmp/project", clock=_fixed_clock)
        result = d.dispatch(
            command='opencode run "/intake WL-1 do not ask questions"',
            work_item_id="WL-1",
        )

        assert result.success is True
        assert result.pid == 42
        assert result.error is None
        assert result.command == 'opencode run "/intake WL-1 do not ask questions"'
        assert result.work_item_id == "WL-1"
        assert result.timestamp == FIXED_TIME

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_popen_called_with_correct_args(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=1)
        env = {"PATH": "/usr/bin"}

        d = OpenCodeRunDispatcher(cwd="/my/cwd", env=env, clock=_fixed_clock)
        d.dispatch(command="some command", work_item_id="WL-2")

        mock_popen.assert_called_once_with(
            "some command",
            shell=True,
            cwd="/my/cwd",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_default_cwd_is_none(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=1)

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd", work_item_id="WL-3")

        _, kwargs = mock_popen.call_args
        assert kwargs["cwd"] is None

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_default_env_is_none(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=1)

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd", work_item_id="WL-4")

        _, kwargs = mock_popen.call_args
        assert kwargs["env"] is None

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_detached_session(self, mock_popen):
        """Verify start_new_session=True for process group detachment."""
        mock_popen.return_value = MagicMock(pid=1)

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd", work_item_id="WL-5")

        _, kwargs = mock_popen.call_args
        assert kwargs["start_new_session"] is True

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_devnull_streams(self, mock_popen):
        """Verify stdout/stderr/stdin redirected to DEVNULL."""
        mock_popen.return_value = MagicMock(pid=1)

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd", work_item_id="WL-6")

        _, kwargs = mock_popen.call_args
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL


class TestOpenCodeRunDispatcherFailures:
    """Tests for spawn failure handling."""

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_file_not_found(self, mock_popen):
        mock_popen.side_effect = FileNotFoundError("opencode: command not found")

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="opencode run x", work_item_id="WL-7")

        assert result.success is False
        assert result.pid is None
        assert "FileNotFoundError" in result.error
        assert "command not found" in result.error

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_permission_error(self, mock_popen):
        mock_popen.side_effect = PermissionError("Permission denied")

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="opencode run x", work_item_id="WL-8")

        assert result.success is False
        assert "PermissionError" in result.error
        assert "Permission denied" in result.error

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_os_error(self, mock_popen):
        mock_popen.side_effect = OSError("Too many open files")

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="opencode run x", work_item_id="WL-9")

        assert result.success is False
        assert "OSError" in result.error
        assert "Too many open files" in result.error

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_failure_preserves_command_and_id(self, mock_popen):
        mock_popen.side_effect = FileNotFoundError("not found")

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="the command", work_item_id="WL-10")

        assert result.command == "the command"
        assert result.work_item_id == "WL-10"
        assert result.timestamp == FIXED_TIME

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_no_blocking_on_failure(self, mock_popen):
        """Dispatch returns immediately even on failure."""
        mock_popen.side_effect = OSError("bad")

        d = OpenCodeRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="cmd", work_item_id="WL-11")

        # The test itself proves non-blocking (no hang), but also verify result
        assert result.success is False


# ---------------------------------------------------------------------------
# DryRunDispatcher tests
# ---------------------------------------------------------------------------


class TestDryRunDispatcherBasic:
    """Tests for DryRunDispatcher recording and mock results."""

    def test_records_dispatch_call(self):
        d = DryRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd1", work_item_id="WL-20")

        assert len(d.calls) == 1
        rec = d.calls[0]
        assert rec.command == "cmd1"
        assert rec.work_item_id == "WL-20"
        assert rec.timestamp == FIXED_TIME

    def test_returns_successful_result(self):
        d = DryRunDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="cmd2", work_item_id="WL-21")

        assert result.success is True
        assert result.pid is not None
        assert result.pid >= 10000
        assert result.error is None

    def test_increments_pid(self):
        d = DryRunDispatcher(clock=_fixed_clock)
        r1 = d.dispatch(command="c1", work_item_id="WL-22")
        r2 = d.dispatch(command="c2", work_item_id="WL-23")

        assert r2.pid == r1.pid + 1

    def test_multiple_calls_recorded(self):
        d = DryRunDispatcher(clock=_fixed_clock)
        d.dispatch(command="a", work_item_id="WL-30")
        d.dispatch(command="b", work_item_id="WL-31")
        d.dispatch(command="c", work_item_id="WL-32")

        assert len(d.calls) == 3
        assert [c.work_item_id for c in d.calls] == ["WL-30", "WL-31", "WL-32"]

    def test_empty_calls_initially(self):
        d = DryRunDispatcher()
        assert d.calls == []


class TestDryRunDispatcherFailOn:
    """Tests for simulated failure mode."""

    def test_fail_on_specific_id(self):
        d = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-BAD"})
        result = d.dispatch(command="cmd", work_item_id="WL-BAD")

        assert result.success is False
        assert "Simulated spawn failure" in result.error
        assert "WL-BAD" in result.error

    def test_fail_on_still_records(self):
        d = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-BAD"})
        d.dispatch(command="cmd", work_item_id="WL-BAD")

        assert len(d.calls) == 1
        assert d.calls[0].work_item_id == "WL-BAD"

    def test_fail_on_does_not_affect_other_ids(self):
        d = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-BAD"})

        r1 = d.dispatch(command="cmd", work_item_id="WL-GOOD")
        r2 = d.dispatch(command="cmd", work_item_id="WL-BAD")

        assert r1.success is True
        assert r2.success is False

    def test_fail_on_no_pid(self):
        d = DryRunDispatcher(clock=_fixed_clock, fail_on={"WL-FAIL"})
        result = d.dispatch(command="cmd", work_item_id="WL-FAIL")

        assert result.pid is None


# ---------------------------------------------------------------------------
# DispatchRecord tests
# ---------------------------------------------------------------------------


class TestDispatchRecord:
    """Tests for DispatchRecord data class."""

    def test_fields(self):
        rec = DispatchRecord(
            command="cmd",
            work_item_id="WL-50",
            timestamp=FIXED_TIME,
        )
        assert rec.command == "cmd"
        assert rec.work_item_id == "WL-50"
        assert rec.timestamp == FIXED_TIME


# ---------------------------------------------------------------------------
# Integration-style tests (realistic command strings)
# ---------------------------------------------------------------------------


class TestRealisticCommands:
    """Tests with realistic opencode run command strings."""

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_intake_command(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=100)
        d = OpenCodeRunDispatcher(cwd="/project", clock=_fixed_clock)

        result = d.dispatch(
            command='opencode run "/intake SA-0MLX8E2790I37XJT do not ask questions"',
            work_item_id="SA-0MLX8E2790I37XJT",
        )

        assert result.success is True
        assert "SA-0MLX8E2790I37XJT" in result.command

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_plan_command(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=101)
        d = OpenCodeRunDispatcher(clock=_fixed_clock)

        result = d.dispatch(
            command='opencode run "/plan SA-0MLX8EN3E0QHMN4I"',
            work_item_id="SA-0MLX8EN3E0QHMN4I",
        )

        assert result.success is True

    @patch("ampa.engine.dispatch.subprocess.Popen")
    def test_implement_command(self, mock_popen):
        mock_popen.return_value = MagicMock(pid=102)
        d = OpenCodeRunDispatcher(clock=_fixed_clock)

        result = d.dispatch(
            command='opencode run "work on SA-0MLX8F4EP1FMCO8L using the implement skill"',
            work_item_id="SA-0MLX8F4EP1FMCO8L",
        )

        assert result.success is True

    def test_dry_run_with_realistic_commands(self):
        d = DryRunDispatcher(clock=_fixed_clock)

        d.dispatch(
            command='opencode run "/intake WL-1 do not ask questions"',
            work_item_id="WL-1",
        )
        d.dispatch(
            command='opencode run "/plan WL-2"',
            work_item_id="WL-2",
        )
        d.dispatch(
            command='opencode run "work on WL-3 using the implement skill"',
            work_item_id="WL-3",
        )

        assert len(d.calls) == 3
        assert d.calls[0].command == 'opencode run "/intake WL-1 do not ask questions"'
        assert d.calls[1].command == 'opencode run "/plan WL-2"'
        assert (
            d.calls[2].command
            == 'opencode run "work on WL-3 using the implement skill"'
        )
