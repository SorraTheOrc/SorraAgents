"""Tests for the Pi-side Ralph signal consumer.

Verifies signal file polling, event deduplication, ``ralph status``
invocation integration, configuration, and error handling according to the
specification in ``docs/ralph-signal.md``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts import signal_consumer


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_signal_file(path: Path, event_type: str, timestamp: str) -> None:
    """Helper: write a signal file with the given event_type and timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "event_type": event_type,
            "timestamp": timestamp,
            "work_item_ids": ["SA-001"],
        }, indent=2) + "\n",
        encoding="utf-8",
    )


def _make_runtime_context(runtime_dir: Path, signal_path: str) -> None:
    """Helper: write a runtime context with the given signal_file_path."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "current.json").write_text(
        json.dumps({
            "target_id": "SA-001",
            "pid": 12345,
            "signal_file_path": signal_path,
        }, indent=2) + "\n",
        encoding="utf-8",
    )


# ── resolve_runtime_dir ─────────────────────────────────────────────────────


class TestResolveRuntimeDir:
    """Verify runtime directory path resolution."""

    def test_default_when_no_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Without env var, defaults to .worklog/ralph relative to CWD."""
        monkeypatch.delenv("RALPH_RUNTIME_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        result = signal_consumer.resolve_runtime_dir()
        assert result == tmp_path / ".worklog" / "ralph"

    def test_from_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """RALPH_RUNTIME_DIR env var takes precedence."""
        monkeypatch.setenv("RALPH_RUNTIME_DIR", "/custom/ralph/dir")
        monkeypatch.chdir(tmp_path)
        result = signal_consumer.resolve_runtime_dir()
        assert result == Path("/custom/ralph/dir")

    def test_from_cwd_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Explicit cwd argument is used as base."""
        monkeypatch.delenv("RALPH_RUNTIME_DIR", raising=False)
        result = signal_consumer.resolve_runtime_dir(cwd=tmp_path / "override")
        assert result == tmp_path / "override" / ".worklog" / "ralph"


# ── resolve_signal_file_path ────────────────────────────────────────────────


class TestResolveSignalFilePath:
    """Verify signal file path resolution from runtime context."""

    def test_from_runtime_context(self, tmp_path: Path):
        """signal_file_path is read from current.json."""
        runtime_dir = tmp_path / "runtime"
        signal_path = tmp_path / ".ralph" / "event.pending"
        _make_runtime_context(runtime_dir, str(signal_path))
        result = signal_consumer.resolve_signal_file_path(runtime_dir)
        assert result == signal_path

    def test_defaults_to_ralph_event_pending(self, tmp_path: Path):
        """When context has no signal_file_path, falls back to default."""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "current.json").write_text(
            json.dumps({"target_id": "SA-001", "pid": 123}) + "\n",
            encoding="utf-8",
        )
        result = signal_consumer.resolve_signal_file_path(runtime_dir)
        assert result == Path(".ralph") / "event.pending"

    def test_returns_none_when_no_context(self, tmp_path: Path):
        """Returns None when runtime context file does not exist."""
        runtime_dir = tmp_path / "runtime"
        result = signal_consumer.resolve_signal_file_path(runtime_dir)
        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path: Path):
        """Returns None when context file contains invalid JSON."""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "current.json").write_text("not json", encoding="utf-8")
        result = signal_consumer.resolve_signal_file_path(runtime_dir)
        assert result is None


# ── Deduplication ───────────────────────────────────────────────────────────


class TestDedupStore:
    """Verify dedup store path resolution."""

    def test_default_path(self, tmp_path: Path):
        """Default dedup store is in the runtime directory."""
        store = signal_consumer._dedup_store_path(tmp_path)
        assert store == tmp_path / ".last_signal_consumed.json"

    def test_from_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """RALPH_DEDUP_STORE env var overrides default."""
        monkeypatch.setenv("RALPH_DEDUP_STORE", "/custom/dedup.json")
        store = signal_consumer._dedup_store_path(tmp_path)
        assert store == Path("/custom/dedup.json")


class TestDeduplication:
    """Verify event deduplication logic."""

    def test_load_last_consumed_no_file(self, tmp_path: Path):
        """Returns empty dict when store doesn't exist."""
        result = signal_consumer.load_last_consumed(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_last_consumed_valid(self, tmp_path: Path):
        """Returns parsed dict when store exists and is valid."""
        store_path = tmp_path / "dedup.json"
        store_path.write_text(
            json.dumps({"event_type": "started", "timestamp": "2026-06-05T10:00:00Z"}) + "\n",
            encoding="utf-8",
        )
        result = signal_consumer.load_last_consumed(store_path)
        assert result == {"event_type": "started", "timestamp": "2026-06-05T10:00:00Z"}

    def test_load_last_consumed_invalid_json(self, tmp_path: Path):
        """Returns empty dict when store contains invalid JSON."""
        store_path = tmp_path / "dedup.json"
        store_path.write_text("garbage", encoding="utf-8")
        result = signal_consumer.load_last_consumed(store_path)
        assert result == {}

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        """save_last_consumed produces a file that load_last_consumed reads."""
        store_path = tmp_path / "dedup.json"
        signal_consumer.save_last_consumed(store_path, "completed", "2026-06-06T12:00:00Z")
        result = signal_consumer.load_last_consumed(store_path)
        assert result["event_type"] == "completed"
        assert result["timestamp"] == "2026-06-06T12:00:00Z"

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """save_last_consumed creates parent directories if needed."""
        nested = tmp_path / "a" / "b" / "c" / "dedup.json"
        signal_consumer.save_last_consumed(nested, "started", "2026-01-01T00:00:00Z")
        assert nested.exists()

    def test_is_new_event_first_event(self):
        """First event is always new when no last_consumed exists."""
        assert signal_consumer.is_new_event({}, "started", "2026-06-05T10:00:00Z") is True

    def test_is_new_event_same_event_is_duplicate(self):
        """Matching event_type + timestamp is a duplicate."""
        last = {"event_type": "completed", "timestamp": "2026-06-06T12:00:00Z"}
        assert signal_consumer.is_new_event(last, "completed", "2026-06-06T12:00:00Z") is False

    def test_is_new_event_different_event_type(self):
        """Different event_type means it's a new event."""
        last = {"event_type": "completed", "timestamp": "2026-06-06T12:00:00Z"}
        assert signal_consumer.is_new_event(last, "started", "2026-06-06T12:00:00Z") is True

    def test_is_new_event_different_timestamp(self):
        """Different timestamp means it's a new event even with same type."""
        last = {"event_type": "completed", "timestamp": "2026-06-06T12:00:00Z"}
        assert signal_consumer.is_new_event(last, "completed", "2026-06-06T13:00:00Z") is True


# ── Signal file reading ─────────────────────────────────────────────────────


class TestReadSignalFile:
    """Verify signal file reading and parsing."""

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        """Returns None when signal file doesn't exist."""
        result = signal_consumer.read_signal_file(tmp_path / "nonexistent.json")
        assert result is None

    def test_returns_parsed_json(self, tmp_path: Path):
        """Returns parsed JSON dict when file contains valid JSON."""
        sig_path = tmp_path / "event.pending"
        _make_signal_file(sig_path, "completed", "2026-06-06T12:00:00Z")
        result = signal_consumer.read_signal_file(sig_path)
        assert result is not None
        assert result["event_type"] == "completed"
        assert result["timestamp"] == "2026-06-06T12:00:00Z"

    def test_returns_none_on_invalid_json(self, tmp_path: Path):
        """Returns None when signal file contains invalid JSON."""
        sig_path = tmp_path / "event.pending"
        sig_path.write_text("not valid json{{{", encoding="utf-8")
        result = signal_consumer.read_signal_file(sig_path)
        assert result is None

    def test_returns_none_on_empty_file(self, tmp_path: Path):
        """Returns None when signal file is empty."""
        sig_path = tmp_path / "event.pending"
        sig_path.write_text("", encoding="utf-8")
        result = signal_consumer.read_signal_file(sig_path)
        assert result is None

    def test_returns_none_on_non_object_json(self, tmp_path: Path):
        """Returns None when signal file contains a non-object JSON value."""
        sig_path = tmp_path / "event.pending"
        sig_path.write_text("[1, 2, 3]", encoding="utf-8")
        result = signal_consumer.read_signal_file(sig_path)
        assert result is None


# ── Signal clearing ─────────────────────────────────────────────────────────


class TestClearSignalFile:
    """Verify signal file clearing behavior."""

    def test_delete_mode(self, tmp_path: Path):
        """clear_signal_file deletes the signal file."""
        sig_path = tmp_path / "event.pending"
        _make_signal_file(sig_path, "started", "2026-06-05T10:00:00Z")
        signal_consumer.clear_signal_file(sig_path)
        assert not sig_path.exists()

    def test_fallback_overwrite_when_delete_fails(self, tmp_path: Path):
        """Falls back to overwriting with {} when delete fails."""
        sig_path = tmp_path / "event.pending"
        _make_signal_file(sig_path, "started", "2026-06-05T10:00:00Z")
        # Mock unlink to force fallback
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            signal_consumer.clear_signal_file(sig_path)
            assert sig_path.exists()
            content = sig_path.read_text(encoding="utf-8").strip()
            assert content == "{}"


# ── invoke_ralph_status ─────────────────────────────────────────────────────


class TestInvokeRalphStatus:
    """Verify ralph status invocation."""

    def test_command_notfound(self):
        """Returns error tuple when ralph command doesn't exist."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ralph not found")
            rc, stdout, stderr = signal_consumer.invoke_ralph_status()
            assert rc == 1
            assert stdout == ""
            assert "not found" in stderr.lower()

    def test_command_runs_successfully(self):
        """Returns (0, stdout, stderr) on successful execution."""
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "# Ralph Status\n\n**State**: `running`"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc
            rc, stdout, stderr = signal_consumer.invoke_ralph_status()
            assert rc == 0
            assert stdout == mock_proc.stdout
            assert stderr == mock_proc.stderr

    def test_command_returns_error_on_nonzero_rc(self):
        """Returns (rc, stdout, stderr) even when command fails."""
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stdout = ""
            mock_proc.stderr = "ralph: no context found"
            mock_run.return_value = mock_proc
            rc, stdout, stderr = signal_consumer.invoke_ralph_status()
            assert rc == 1
            assert "no context found" in stderr


# ── consume_once ────────────────────────────────────────────────────────────


class TestConsumeOnceNoSignal:
    """Verify consume_once when no signal is present."""

    def test_returns_none_when_no_runtime_context(self, tmp_path: Path):
        """Returns a dict with consumed=False when runtime context doesn't exist."""
        runtime_dir = tmp_path / "runtime"
        result = signal_consumer.consume_once(runtime_dir=runtime_dir)
        assert result is not None
        assert result["consumed"] is False

    def test_returns_none_when_no_signal_file(self, tmp_path: Path):
        """Returns a dict with consumed=False when signal file doesn't exist."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        _make_runtime_context(runtime_dir, str(signal_dir / "event.pending"))
        result = signal_consumer.consume_once(runtime_dir=runtime_dir)
        assert result is not None
        assert result["consumed"] is False


class TestConsumeOnceWithSignal:
    """Verify consume_once when a new signal is present."""

    def test_consume_single_signal(self, tmp_path: Path):
        """A new signal triggers ralph_status invocation and is consumed."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "completed", "2026-06-06T12:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "# Ralph Status\n**State**: stopped", "")
            result = signal_consumer.consume_once(runtime_dir=runtime_dir)

        assert result is not None
        assert result["consumed"] is True
        assert result["event_type"] == "completed"
        assert result["ralph_output"] is not None
        assert not signal_path.exists()

    def test_deduplication_prevents_re_consumption(self, tmp_path: Path):
        """Re-running consume_once on the same signal skips it as duplicate."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        dedup_path = tmp_path / "dedup.json"
        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "completed", "2026-06-06T12:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "output", "")
            result1 = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
        assert result1 is not None
        assert result1["consumed"] is True

        _make_signal_file(signal_path, "completed", "2026-06-06T12:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "output", "")
            result2 = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
        assert result2 is not None
        assert result2.get("skipped") == "duplicate"

    def test_new_event_after_same_type_different_timestamp(self, tmp_path: Path):
        """A new signal with same type but different timestamp is consumed."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        dedup_path = tmp_path / "dedup.json"
        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "error", "2026-06-06T12:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "status output", "")
            result = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
        assert result["consumed"] is True

        _make_signal_file(signal_path, "error", "2026-06-06T13:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status2:
            mock_status2.return_value = (0, "status output 2", "")
            result2 = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
        assert result2["consumed"] is True
        assert mock_status2.call_count == 1


class TestConsumeOnceOnError:
    """Verify consume_once error handling."""

    def test_ralph_status_failure_does_not_prevent_consumption(self, tmp_path: Path):
        """Even when ralph_status fails, the signal is consumed and cleared."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "error", "2026-06-06T12:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (1, "", "ralph: error")
            result = signal_consumer.consume_once(runtime_dir=runtime_dir)

        assert result is not None
        assert result["consumed"] is True
        assert result.get("ralph_error") is True
        assert not signal_path.exists()

    def test_missing_required_fields_causes_error(self, tmp_path: Path):
        """Signal file without event_type or timestamp is cleared and returns error."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        signal_dir.mkdir(parents=True, exist_ok=True)
        _make_runtime_context(runtime_dir, str(signal_path))
        signal_path.write_text(json.dumps({"work_item_ids": []}) + "\n", encoding="utf-8")

        result = signal_consumer.consume_once(runtime_dir=runtime_dir)
        assert result is not None
        assert result["consumed"] is False
        assert "required fields" in (result.get("error") or "")
        assert not signal_path.exists()


class TestConsumeOnceOverrides:
    """Verify consume_once supports explicit path overrides."""

    def test_explicit_signal_file_path(self, tmp_path: Path):
        """consume_once accepts explicit signal_file_path."""
        runtime_dir = tmp_path / "runtime"
        signal_path = tmp_path / "custom" / "signal.json"
        dedup_path = tmp_path / "dedup.json"
        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "started", "2026-06-05T10:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "output", "")
            result = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                signal_file_path=signal_path,
                dedup_store_path=dedup_path,
            )
        assert result is not None
        assert result["consumed"] is True


# ── build_parser ────────────────────────────────────────────────────────────


class TestBuildParser:
    """Verify the CLI argument parser."""

    def test_parser_has_consume_once_flag(self):
        """Parser has --consume-once flag."""
        parser = signal_consumer.build_parser()
        args = parser.parse_args(["--consume-once"])
        assert args.consume_once is True

    def test_parser_has_verbose_flag(self):
        """Parser has --verbose flag."""
        parser = signal_consumer.build_parser()
        args = parser.parse_args(["-v"])
        assert args.verbose is True

    def test_parser_has_poll_interval_option(self):
        """Parser has --poll-interval option."""
        parser = signal_consumer.build_parser()
        args = parser.parse_args(["--poll-interval", "60"])
        assert args.poll_interval == 60


# ── Integration-style tests ─────────────────────────────────────────────────


class TestIntegration:
    """Integration tests that exercise the full consume pipeline."""

    def test_full_poll_cycle(self, tmp_path: Path):
        """End-to-end: signal file exists, consumed, cleared, dedup works."""
        runtime_dir = tmp_path / "runtime"
        signal_dir = tmp_path / ".ralph"
        signal_path = signal_dir / "event.pending"
        dedup_path = tmp_path / "dedup.json"

        _make_runtime_context(runtime_dir, str(signal_path))
        _make_signal_file(signal_path, "phase_change", "2026-06-07T10:00:00Z")

        with patch.object(signal_consumer, "invoke_ralph_status") as mock_status:
            mock_status.return_value = (0, "# Ralph Status\n**State**: `running`", "")

            result = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
            assert result["consumed"] is True
            assert mock_status.call_count == 1

            _make_signal_file(signal_path, "phase_change", "2026-06-07T10:00:00Z")
            result = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
            assert result.get("skipped") == "duplicate"
            assert mock_status.call_count == 1

            _make_signal_file(signal_path, "completed", "2026-06-07T10:05:00Z")
            result = signal_consumer.consume_once(
                runtime_dir=runtime_dir,
                dedup_store_path=dedup_path,
            )
            assert result["consumed"] is True
            assert mock_status.call_count == 2
