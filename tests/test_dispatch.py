"""Tests for ampa.engine.dispatch — Dispatcher protocol, OpenCodeRunDispatcher, ContainerDispatcher, DryRunDispatcher."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from ampa.engine.dispatch import (
    ContainerDispatcher,
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

    def test_container_id_default_none(self):
        """container_id defaults to None when not provided."""
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-CID-1",
            timestamp=FIXED_TIME,
            pid=100,
        )
        assert r.container_id is None

    def test_container_id_set(self):
        """container_id is stored when explicitly provided."""
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-CID-2",
            timestamp=FIXED_TIME,
            pid=200,
            container_id="abc123def456",
        )
        assert r.container_id == "abc123def456"

    def test_summary_with_container_id(self):
        """summary includes container_id when present."""
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-CID-3",
            timestamp=FIXED_TIME,
            pid=300,
            container_id="ctr-xyz",
        )
        s = r.summary
        assert "WL-CID-3" in s
        assert "pid=300" in s
        assert "container=ctr-xyz" in s
        assert "Dispatched" in s

    def test_summary_without_container_id(self):
        """summary omits container when container_id is None."""
        r = DispatchResult(
            success=True,
            command="cmd",
            work_item_id="WL-CID-4",
            timestamp=FIXED_TIME,
            pid=400,
        )
        s = r.summary
        assert "container" not in s
        assert "pid=400" in s

    def test_summary_failure_ignores_container_id(self):
        """Failed dispatch summary does not mention container_id."""
        r = DispatchResult(
            success=False,
            command="cmd",
            work_item_id="WL-CID-5",
            timestamp=FIXED_TIME,
            error="boom",
            container_id="should-not-appear",
        )
        s = r.summary
        assert "boom" in s
        assert "failed" in s
        assert "should-not-appear" not in s


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


# ---------------------------------------------------------------------------
# ContainerDispatcher tests
# ---------------------------------------------------------------------------

# Patch base path for all pool helpers so tests never touch real pool state.
_POOL_MOD = "ampa.engine.dispatch"


class TestContainerDispatcherProtocol:
    """Verify ContainerDispatcher satisfies the Dispatcher protocol."""

    def test_is_dispatcher(self):
        d = ContainerDispatcher(project_root="/tmp/proj")
        assert isinstance(d, Dispatcher)


class TestContainerDispatcherSuccess:
    """Tests for the successful container dispatch path."""

    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-0")
    def test_successful_dispatch(self, mock_claim, mock_popen):
        """dispatch() acquires container, spawns distrobox, returns success."""
        mock_proc = MagicMock()
        mock_proc.pid = 7777
        mock_popen.return_value = mock_proc

        d = ContainerDispatcher(
            project_root="/tmp/proj",
            branch="feature/test",
            clock=_fixed_clock,
        )
        result = d.dispatch(
            command='opencode run "/intake WL-1"',
            work_item_id="WL-1",
        )

        assert result.success is True
        assert result.pid == 7777
        assert result.container_id == "ampa-pool-0"
        assert result.error is None
        assert result.timestamp == FIXED_TIME
        assert "distrobox enter ampa-pool-0" in result.command
        assert 'opencode run "/intake WL-1"' in result.command

    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-2")
    def test_popen_called_with_distrobox_command(self, mock_claim, mock_popen):
        """Popen is called with the distrobox-wrapped command."""
        mock_popen.return_value = MagicMock(pid=1)

        d = ContainerDispatcher(
            project_root="/my/project",
            branch="main",
            clock=_fixed_clock,
        )
        d.dispatch(command='opencode run "/plan WL-2"', work_item_id="WL-2")

        args, kwargs = mock_popen.call_args
        assert args[0] == 'distrobox enter ampa-pool-2 -- opencode run "/plan WL-2"'
        assert kwargs["shell"] is True
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["cwd"] == "/my/project"

    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-1")
    def test_container_env_vars_set(self, mock_claim, mock_popen):
        """Container env vars are injected into the subprocess environment."""
        mock_popen.return_value = MagicMock(pid=1)

        d = ContainerDispatcher(
            project_root="/proj",
            branch="feat/x",
            env={"EXISTING": "value"},
            clock=_fixed_clock,
        )
        d.dispatch(command="cmd", work_item_id="WL-3")

        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env["AMPA_CONTAINER_NAME"] == "ampa-pool-1"
        assert env["AMPA_WORK_ITEM_ID"] == "WL-3"
        assert env["AMPA_BRANCH"] == "feat/x"
        assert env["AMPA_PROJECT_ROOT"] == "/proj"
        # Also preserves the caller-supplied env
        assert env["EXISTING"] == "value"

    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-0")
    def test_claim_called_with_work_item_and_branch(self, mock_claim, mock_popen):
        """_claim_pool_container is called with the correct arguments."""
        mock_popen.return_value = MagicMock(pid=1)

        d = ContainerDispatcher(
            project_root="/p",
            branch="wl-123-fix",
            clock=_fixed_clock,
        )
        d.dispatch(command="cmd", work_item_id="WL-123")

        mock_claim.assert_called_once_with("WL-123", "wl-123-fix")


class TestContainerDispatcherPoolEmpty:
    """Tests for the pool-empty failure path."""

    @patch(f"{_POOL_MOD}._claim_pool_container", return_value=None)
    def test_no_containers_available(self, mock_claim):
        """dispatch() returns failed result when pool is empty."""
        d = ContainerDispatcher(
            project_root="/proj",
            clock=_fixed_clock,
        )
        result = d.dispatch(command="cmd", work_item_id="WL-EMPTY")

        assert result.success is False
        assert result.pid is None
        assert result.container_id is None
        assert "No pool containers available" in result.error
        assert result.work_item_id == "WL-EMPTY"
        assert result.timestamp == FIXED_TIME

    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value=None)
    def test_popen_not_called_when_pool_empty(self, mock_claim, mock_popen):
        """Popen is never called when no container is available."""
        d = ContainerDispatcher(clock=_fixed_clock)
        d.dispatch(command="cmd", work_item_id="WL-EMPTY")

        mock_popen.assert_not_called()


class TestContainerDispatcherSpawnFailure:
    """Tests for spawn failure with pool release."""

    @patch(f"{_POOL_MOD}._release_pool_container")
    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-0")
    def test_file_not_found_releases_container(
        self, mock_claim, mock_popen, mock_release
    ):
        """FileNotFoundError releases the container and returns failure."""
        mock_popen.side_effect = FileNotFoundError("distrobox: not found")

        d = ContainerDispatcher(
            project_root="/proj",
            clock=_fixed_clock,
        )
        result = d.dispatch(command="cmd", work_item_id="WL-FAIL1")

        assert result.success is False
        assert "FileNotFoundError" in result.error
        assert result.container_id == "ampa-pool-0"
        mock_release.assert_called_once_with("ampa-pool-0")

    @patch(f"{_POOL_MOD}._release_pool_container")
    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-1")
    def test_permission_error_releases_container(
        self, mock_claim, mock_popen, mock_release
    ):
        """PermissionError releases the container and returns failure."""
        mock_popen.side_effect = PermissionError("denied")

        d = ContainerDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="cmd", work_item_id="WL-FAIL2")

        assert result.success is False
        assert "PermissionError" in result.error
        assert result.container_id == "ampa-pool-1"
        mock_release.assert_called_once_with("ampa-pool-1")

    @patch(f"{_POOL_MOD}._release_pool_container")
    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-2")
    def test_os_error_releases_container(self, mock_claim, mock_popen, mock_release):
        """OSError releases the container and returns failure."""
        mock_popen.side_effect = OSError("Too many open files")

        d = ContainerDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="cmd", work_item_id="WL-FAIL3")

        assert result.success is False
        assert "OSError" in result.error
        assert result.container_id == "ampa-pool-2"
        mock_release.assert_called_once_with("ampa-pool-2")

    @patch(f"{_POOL_MOD}._release_pool_container")
    @patch(f"{_POOL_MOD}.subprocess.Popen")
    @patch(f"{_POOL_MOD}._claim_pool_container", return_value="ampa-pool-0")
    def test_failure_preserves_command_and_id(
        self, mock_claim, mock_popen, mock_release
    ):
        """Failed result preserves the distrobox command and work item ID."""
        mock_popen.side_effect = OSError("bad")

        d = ContainerDispatcher(clock=_fixed_clock)
        result = d.dispatch(command="opencode run x", work_item_id="WL-META")

        assert "distrobox enter ampa-pool-0 -- opencode run x" in result.command
        assert result.work_item_id == "WL-META"
        assert result.timestamp == FIXED_TIME


class TestContainerDispatcherTimeout:
    """Tests for AMPA_CONTAINER_DISPATCH_TIMEOUT configuration."""

    def test_default_timeout(self):
        """Default timeout is 30 seconds."""
        d = ContainerDispatcher(clock=_fixed_clock)
        assert d._timeout == 30

    def test_constructor_timeout(self):
        """Constructor timeout overrides default."""
        d = ContainerDispatcher(timeout=60, clock=_fixed_clock)
        assert d._timeout == 60

    @patch.dict("os.environ", {"AMPA_CONTAINER_DISPATCH_TIMEOUT": "120"})
    def test_env_var_timeout(self):
        """AMPA_CONTAINER_DISPATCH_TIMEOUT env var overrides constructor."""
        d = ContainerDispatcher(timeout=60, clock=_fixed_clock)
        assert d._timeout == 120

    @patch.dict("os.environ", {"AMPA_CONTAINER_DISPATCH_TIMEOUT": "not-a-number"})
    def test_invalid_env_var_falls_back(self):
        """Invalid env var falls back to constructor / default."""
        d = ContainerDispatcher(timeout=45, clock=_fixed_clock)
        assert d._timeout == 45


# ---------------------------------------------------------------------------
# Pool helper unit tests (module-level functions)
# ---------------------------------------------------------------------------


class TestPoolHelpers:
    """Tests for the pool state read/write/claim/release helpers."""

    def test_read_pool_state_missing_file(self, tmp_path):
        """Returns empty dict when pool-state.json doesn't exist."""
        from ampa.engine.dispatch import _read_pool_state

        with patch(
            f"{_POOL_MOD}._pool_state_path", return_value=tmp_path / "nope.json"
        ):
            assert _read_pool_state() == {}

    def test_read_pool_state_valid(self, tmp_path):
        """Returns parsed JSON when file exists."""
        from ampa.engine.dispatch import _read_pool_state

        state_file = tmp_path / "pool-state.json"
        state_file.write_text(json.dumps({"ampa-pool-0": {"workItemId": "WL-1"}}))

        with patch(f"{_POOL_MOD}._pool_state_path", return_value=state_file):
            state = _read_pool_state()
            assert state["ampa-pool-0"]["workItemId"] == "WL-1"

    def test_read_pool_state_invalid_json(self, tmp_path):
        """Returns empty dict on invalid JSON."""
        from ampa.engine.dispatch import _read_pool_state

        state_file = tmp_path / "pool-state.json"
        state_file.write_text("{bad json")

        with patch(f"{_POOL_MOD}._pool_state_path", return_value=state_file):
            assert _read_pool_state() == {}

    def test_save_pool_state(self, tmp_path):
        """Writes pool state to disk."""
        from ampa.engine.dispatch import _save_pool_state

        state_file = tmp_path / "subdir" / "pool-state.json"

        with patch(f"{_POOL_MOD}._pool_state_path", return_value=state_file):
            _save_pool_state({"ampa-pool-0": {"workItemId": "WL-X"}})

        assert state_file.exists()
        loaded = json.loads(state_file.read_text())
        assert loaded["ampa-pool-0"]["workItemId"] == "WL-X"

    @patch(
        f"{_POOL_MOD}._existing_pool_containers",
        return_value={"ampa-pool-0", "ampa-pool-1"},
    )
    @patch(
        f"{_POOL_MOD}._read_pool_state",
        return_value={"ampa-pool-0": {"workItemId": "WL-BUSY"}},
    )
    def test_list_available_pool(self, mock_state, mock_existing):
        """Returns containers that exist but are not claimed."""
        from ampa.engine.dispatch import _list_available_pool

        available = _list_available_pool()
        assert "ampa-pool-1" in available
        assert "ampa-pool-0" not in available

    @patch(f"{_POOL_MOD}._save_pool_state")
    @patch(f"{_POOL_MOD}._read_pool_state", return_value={})
    @patch(f"{_POOL_MOD}._list_available_pool", return_value=["ampa-pool-0"])
    def test_claim_pool_container_success(self, mock_avail, mock_read, mock_save):
        """Claims the first available container and writes state."""
        from ampa.engine.dispatch import _claim_pool_container

        name = _claim_pool_container("WL-42", "feat/x")
        assert name == "ampa-pool-0"
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        assert saved_state["ampa-pool-0"]["workItemId"] == "WL-42"
        assert saved_state["ampa-pool-0"]["branch"] == "feat/x"

    @patch(f"{_POOL_MOD}._list_available_pool", return_value=[])
    def test_claim_pool_container_empty(self, mock_avail):
        """Returns None when no containers are available."""
        from ampa.engine.dispatch import _claim_pool_container

        assert _claim_pool_container("WL-99", "main") is None

    @patch(f"{_POOL_MOD}._save_pool_state")
    @patch(
        f"{_POOL_MOD}._read_pool_state",
        return_value={"ampa-pool-0": {"workItemId": "WL-1"}},
    )
    def test_release_pool_container(self, mock_read, mock_save):
        """Removes the container claim from pool state."""
        from ampa.engine.dispatch import _release_pool_container

        _release_pool_container("ampa-pool-0")
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        assert "ampa-pool-0" not in saved_state
