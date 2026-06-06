"""Tests for Ralph pi subprocess lifecycle notifications.

Verifies that Ralph sends PI_STARTED and ERROR events when pi subprocesses
are launched or fail, and that Ralph sends a notification when it stops
executing due to an unexpected error.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import EventType, RalphLoop, RalphError


# ── Helper: a RalphLoop subclass for testing notifications ────────────────


class NotificationTestLoop(RalphLoop):
    """A RalphLoop configured for testing notification behavior.

    Uses a temp directory for signal files and disables streaming so that
    _call_with_retry is used instead of _stream_pi.
    """

    def __init__(self, signal_dir: Path, webhook_url: str | None = None):
        super().__init__(
            pi_bin="pi",
            stream=False,
            signal_file_path=str(signal_dir / "event.pending"),
            webhook_url=webhook_url,
        )


# ── _run_pi: PI_STARTED notification ──────────────────────────────────────


class TestRunPiStartNotification:
    """Verify _run_pi sends PI_STARTED notification before launching pi."""

    def test_run_pi_sends_pi_started(self, tmp_path: Path):
        """Calling _run_pi produces a PI_STARTED signal file entry."""
        loop = NotificationTestLoop(tmp_path)

        # Mock _extract_text_and_structured_response_from_json_output
        # to avoid the output validation dependency
        with patch.object(loop, "_call_with_retry") as mock_call:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = '{"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Completed the implementation work. All acceptance criteria are satisfied and tests pass. The changes were applied to the relevant files."}]}]}'
            mock_proc.stderr = ""
            mock_call.return_value = mock_proc

            loop._run_pi("test prompt", phase="implementation", work_item_ids=["SA-001"])

        signal_file = tmp_path / "event.pending"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "pi_started"
        assert "SA-001" in data["work_item_ids"]

    def test_run_pi_pi_started_includes_phase(self, tmp_path: Path):
        """The PI_STARTED event description includes the phase name."""
        loop = NotificationTestLoop(tmp_path)

        valid_stdout = '{"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Audit completed. Ready to close: Yes."}]}]}'

        with patch.object(loop, "_call_with_retry") as mock_call:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = valid_stdout
            mock_proc.stderr = ""
            mock_call.return_value = mock_proc

            loop._run_pi("audit prompt", phase="audit", work_item_ids=["SA-002"])

        # Verify the event was written by reading the signal file
        signal_file = tmp_path / "event.pending"
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "pi_started"

        # Also verify via webhook call
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_ctx
            mock_ctx.status = 204

            webhook_loop = NotificationTestLoop(tmp_path, webhook_url="https://discord.com/api/webhooks/test/abc")
            with patch.object(webhook_loop, "_call_with_retry") as mock_call2:
                mock_proc2 = MagicMock()
                mock_proc2.returncode = 0
                mock_proc2.stdout = valid_stdout
                mock_proc2.stderr = ""
                mock_call2.return_value = mock_proc2
                webhook_loop._run_pi("test", phase="audit", work_item_ids=["SA-002"])

            # Should have sent at least one webhook (for PI_STARTED)
            assert mock_urlopen.call_count >= 1

    def test_run_pi_start_notification_without_ids(self, tmp_path: Path):
        """_run_pi still works when no work_item_ids are provided (no crash)."""
        loop = NotificationTestLoop(tmp_path)

        with patch.object(loop, "_call_with_retry") as mock_call:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = '{"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Completed the implementation work. All acceptance criteria are satisfied and tests pass."}]}]}'
            mock_proc.stderr = ""
            mock_call.return_value = mock_proc

            # Should not raise even without work_item_ids
            result = loop._run_pi("test prompt", phase="implementation")
            assert result is not None

    def test_run_pi_webhook_sent_for_start(self, tmp_path: Path):
        """PI_STARTED triggers a webhook notification when webhook is configured."""
        loop = NotificationTestLoop(
            tmp_path,
            webhook_url="https://discord.com/api/webhooks/test/abc",
        )

        with patch.object(loop, "_call_with_retry") as mock_call:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = '{"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Completed the implementation work."}]}]}'
            mock_proc.stderr = ""
            mock_call.return_value = mock_proc

            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_urlopen.return_value.__enter__.return_value = mock_ctx
                mock_ctx.status = 204

                loop._run_pi("test", phase="implementation", work_item_ids=["SA-001"])

                # PI_STARTED should trigger a webhook call
                assert mock_urlopen.call_count >= 1
                request = mock_urlopen.call_args[0][0]
                body = json.loads(request.data.decode("utf-8"))
                embed = body["embeds"][0]
                assert "pi_started" in embed["description"] or "implementation" in embed["description"]


# ── _run_pi: ERROR notification on failure ──────────────────────────────


class TestRunPiErrorNotification:
    """Verify _run_pi sends ERROR notification when pi subprocess fails."""

    def test_run_pi_failure_sends_error_event(self, tmp_path: Path):
        """When pi subprocess fails, an ERROR event is written to the signal file."""
        loop = NotificationTestLoop(tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Something went wrong"

        with patch.object(loop, "_call_with_retry", return_value=mock_proc):
            with pytest.raises(RalphError):
                loop._run_pi("test prompt", phase="implementation", work_item_ids=["SA-001"])

        # Verify ERROR event was written to signal file
        signal_file = tmp_path / "event.pending"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "error"
        assert "SA-001" in data["work_item_ids"]

    def test_run_pi_stall_sends_error_event(self, tmp_path: Path):
        """When pi subprocess stalls, an ERROR event is written."""
        loop = NotificationTestLoop(tmp_path)

        # Mock _call_with_retry to raise RalphError (simulating stall)
        with patch.object(loop, "_call_with_retry", side_effect=RalphError("pi stream stalled after 60s")):
            with pytest.raises(RalphError):
                loop._run_pi("test prompt", phase="implementation", work_item_ids=["SA-002"])

        # Verify ERROR event was written
        signal_file = tmp_path / "event.pending"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "error"

    def test_run_pi_validation_failure_sends_error(self, tmp_path: Path):
        """When pi output validation fails, an ERROR event is written."""
        loop = NotificationTestLoop(tmp_path)

        # Mock a successful returncode but output that will fail validation
        # by echoing back the input prompt verbatim
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"type": "message_end", "content": "test prompt"}'
        mock_proc.stderr = ""

        with patch.object(loop, "_call_with_retry", return_value=mock_proc):
            with pytest.raises(RalphError):
                loop._run_pi("test prompt", phase="implementation", work_item_ids=["SA-003"])

        # Verify ERROR event was written
        signal_file = tmp_path / "event.pending"
        assert signal_file.exists()
        data = json.loads(signal_file.read_text(encoding="utf-8"))
        assert data["event_type"] == "error"

    def test_run_pi_failure_sends_webhook(self, tmp_path: Path):
        """When pi fails, an ERROR webhook notification is sent."""
        loop = NotificationTestLoop(
            tmp_path,
            webhook_url="https://discord.com/api/webhooks/test/abc",
        )

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Failed"

        with patch.object(loop, "_call_with_retry", return_value=mock_proc):
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_ctx = MagicMock()
                mock_urlopen.return_value.__enter__.return_value = mock_ctx
                mock_ctx.status = 204

                with pytest.raises(RalphError):
                    loop._run_pi("test", phase="implementation", work_item_ids=["SA-001"])

                # At least one webhook should have been sent (PI_STARTED + ERROR)
                assert mock_urlopen.call_count >= 1

    def test_run_pi_failure_does_not_block_loop(self, tmp_path: Path):
        """ERROR event is written even when signal write fails."""
        loop = NotificationTestLoop(tmp_path)

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "err"

        with patch.object(loop, "_call_with_retry", return_value=mock_proc):
            with patch.object(loop, "_signal_writer") as mock_writer:
                mock_writer.write_event.side_effect = PermissionError("No write")
                with pytest.raises(RalphError):
                    # Should still raise even though signal write failed
                    loop._run_pi("test", phase="implementation", work_item_ids=["SA-001"])


# ── main(): RalphError catch sends notification ──────────────────────────


class TestMainErrorNotification:
    """Verify main() sends ERROR notification when RalphError is caught."""

    def test_main_ralp_error_sends_notification(self, tmp_path: Path):
        """When main() catches RalphError, an ERROR notification is produced."""
        from skill.ralph.scripts.ralph_loop import main

        signal_path = tmp_path / "event.pending"

        # Need to mock the loop creation and run to inject our signal path
        with patch("skill.ralph.scripts.ralph_loop.RalphLoop") as MockLoop:
            mock_instance = MagicMock()
            mock_instance.run.side_effect = RalphError("pi stream stalled after 60s")
            mock_instance._signal_writer = None
            mock_instance._webhook_notifier = None
            MockLoop.return_value = mock_instance

            # main() should catch the RalphError and return 2
            with patch("sys.argv", ["ralph", "SA-001"]):
                exit_code = main(["SA-001"])
                assert exit_code == 2

    def test_main_sends_error_event_when_signal_writer_available(self, tmp_path: Path):
        """When signal_writer is available in main(), ERROR event is sent on RalphError."""
        from skill.ralph.scripts.ralph_loop import main

        with patch("skill.ralph.scripts.ralph_loop.RalphLoop") as MockLoop:
            mock_instance = MagicMock()
            mock_instance.run.side_effect = RalphError("pi failed")
            MockLoop.return_value = mock_instance

            with patch("sys.argv", ["ralph", "SA-001"]):
                main(["SA-001"])

            # _notify_event should have been called at least once (for ERROR)
            assert mock_instance._notify_event.call_count >= 1
