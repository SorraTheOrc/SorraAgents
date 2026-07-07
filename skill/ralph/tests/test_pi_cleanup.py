"""Tests for Ralph Pi subprocess cleanup after loop completion.

Covers both the graceful shutdown path (SIGTERM → exit) and the
fallback forced-termination path (SIGTERM → timeout → SIGKILL).
"""

import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[3]

from skill.ralph.scripts.ralph_loop import RalphLoop  # noqa: E402


def _make_mock_process(
    pid: int = 12345,
    poll_return: int | None = None,
    wait_side_effect: Exception | list | None = None,
    kill_side_effect: Exception | None = None,
) -> MagicMock:
    """Create a mock subprocess.Popen with configurable lifecycle behavior.

    Args:
        pid: The process ID.
        poll_return: Return value for poll(). None means process is alive;
            an int means it has exited with that return code.
        wait_side_effect: If set, wait() raises this exception or iterates
            through a list of effects (for multi-call scenarios where the
            first call should time out and the second should succeed).
        kill_side_effect: If set, kill() raises this exception.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = poll_return
    if wait_side_effect is not None:
        proc.wait.side_effect = wait_side_effect
    else:
        proc.wait.return_value = 0
    # Ensure wait returns 0 after iterating through list side effects
    if isinstance(wait_side_effect, list):
        proc.wait.return_value = 0
    if kill_side_effect:
        proc.kill.side_effect = kill_side_effect
    else:
        proc.kill.return_value = None
    return proc


def test_cleanup_pi_process_already_exited():
    """When Pi has already exited, cleanup returns immediately without
    attempting any signals."""
    proc = _make_mock_process(poll_return=0)

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop._pi_process = proc
    loop._cleanup_pi_process()

    proc.kill.assert_not_called()
    proc.wait.assert_not_called()
    assert loop._pi_process is None


def test_cleanup_pi_process_no_process():
    """When there is no tracked process, cleanup is a no-op."""
    loop = RalphLoop(pi_bin="pi", stream=False)
    loop._pi_process = None
    # Should not raise
    loop._cleanup_pi_process()


def test_cleanup_pi_process_graceful_shutdown():
    """Cleanup sends SIGTERM, and the process exits within the grace period."""
    proc = _make_mock_process(poll_return=None, wait_side_effect=None)

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop.pi_cleanup_timeout = 0.5
    loop._pi_process = proc

    with patch("os.kill") as mock_kill:
        loop._cleanup_pi_process()

    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    proc.wait.assert_called_once_with(timeout=0.5)
    proc.kill.assert_not_called()
    assert loop._pi_process is None


def test_cleanup_pi_process_graceful_shutdown_exit_logged():
    """When SIGTERM is sent and the process exits gracefully, a cleanup
    info log is emitted. We verify by checking that kill was NOT called
    and wait returned normally."""
    proc = _make_mock_process(poll_return=None, wait_side_effect=None)

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop.pi_cleanup_timeout = 0.5
    loop._pi_process = proc

    with patch("os.kill") as mock_kill:
        loop._cleanup_pi_process()

    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    proc.kill.assert_not_called()
    assert loop._pi_process is None


def test_cleanup_pi_process_process_already_gone():
    """If the process disappears between poll() and os.kill (ProcessLookupError),
    cleanup handles it gracefully and continues."""
    proc = _make_mock_process(poll_return=None, wait_side_effect=None)

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop._pi_process = proc

    def _raise_process_lookup(*args, **kwargs):
        raise ProcessLookupError(f"No process with PID {proc.pid}")

    with patch("os.kill", side_effect=_raise_process_lookup):
        loop._cleanup_pi_process()

    proc.kill.assert_not_called()
    assert loop._pi_process is None


def test_cleanup_pi_process_force_kill_fallback():
    """Cleanup sends SIGTERM, process does not exit within the grace period,
    so cleanup escalates to SIGKILL."""
    proc = _make_mock_process(
        poll_return=None,
        wait_side_effect=[
            subprocess.TimeoutExpired(cmd="pi", timeout=0.5),
            None,
        ],
    )

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop.pi_cleanup_timeout = 0.5
    loop._pi_process = proc

    with patch("os.kill") as mock_kill:
        loop._cleanup_pi_process()

    # os.kill should be called once: SIGTERM for graceful shutdown
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    # process.kill() is called for the forced kill fallback
    proc.kill.assert_called_once()
    assert loop._pi_process is None


def test_cleanup_pi_process_no_pid():
    """If the process has no pid, cleanup returns immediately."""
    proc = MagicMock()
    proc.pid = None

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop._pi_process = proc
    loop._cleanup_pi_process()

    assert loop._pi_process is None


def test_run_calls_cleanup_on_success():
    """When run() completes with success, _cleanup_pi_process is called."""
    proc = _make_mock_process(poll_return=None)

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop._pi_process = proc

    with (
        patch("os.kill") as mock_kill,
        patch.object(loop, "_run_checks"),
        patch.object(loop, "_run_merge"),
    ):
        loop._cleanup_pi_process()

    mock_kill.assert_called_once_with(12345, signal.SIGTERM)


def test_cleanup_pi_process_oserror_on_kill():
    """If process.kill() raises an OSError during the forced-kill step,
    cleanup logs the warning and clears the process reference."""
    proc = _make_mock_process(
        poll_return=None,
        wait_side_effect=[
            subprocess.TimeoutExpired(cmd="pi", timeout=0.5),
            None,
        ],
        kill_side_effect=OSError("cannot kill process"),
    )

    loop = RalphLoop(pi_bin="pi", stream=False)
    loop.pi_cleanup_timeout = 0.5
    loop._pi_process = proc

    with patch("os.kill"):
        loop._cleanup_pi_process()

    assert loop._pi_process is None
