"""Tests for streaming output in ralph_loop._stream_pi, specifically that
text blocks separated by suppressed events (thinking, etc.) get a newline
separator between them."""

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

from skill.ralph.scripts.ralph_loop import RalphLoop


def _fake_process(lines: list[str], returncode: int = 0) -> MagicMock:
    """Create a mock subprocess.Popen that yields the given lines from stdout."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.poll.return_value = returncode
    proc.wait.return_value = returncode

    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)
            self._idx = 0

        def readline(self, size=-1):
            if self._idx < len(self._lines):
                line = self._lines[self._idx]
                self._idx += 1
                return line
            return ""

        def read(self, size=-1):
            return ""

        def close(self):
            pass

    proc.stdout = FakeStream(lines)
    proc.stderr = FakeStream([])
    return proc


def _json_line(event: dict) -> str:
    """Return a line as readline() would yield it: JSON + trailing newline."""
    return json.dumps(event) + "\n"


def _event(event_type: str, assistant_type: str | None = None, **extra) -> dict:
    d = {"type": event_type}
    if assistant_type is not None:
        d["assistantMessageEvent"] = {"type": assistant_type, **extra}
    else:
        d.update(extra)
    return d


def test_stream_pi_adds_newline_between_thought_blocks():
    """When text_delta events are separated by thinking (suppressed) events,
    a newline should be printed between them."""
    lines = [
        _json_line(_event("message_update", "text_delta", delta="First thought.")),
        _json_line(_event("message_update", "thinking_start")),
        _json_line(_event("message_update", "thinking_end")),
        _json_line(_event("message_update", "text_delta", delta="Second thought.")),
        _json_line(_event("message_update", "text_delta", delta=" Continued.")),
    ]
    proc = _fake_process(lines)
    loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

    with (
        patch("subprocess.Popen", return_value=proc) as mock_popen,
        redirect_stdout(io.StringIO()) as buf,
    ):
        result = loop._stream_pi(["pi", "-p", "--mode", "json", "prompt"], "prompt")

    output = buf.getvalue()
    # Should have a newline between the two thought blocks
    assert "First thought.\nSecond thought." in output, (
        f"Expected newline between thought blocks, got: {repr(output)}"
    )


def test_stream_pi_no_leading_newline():
    """The first text_delta should NOT have a leading newline."""
    lines = [
        _json_line(_event("message_update", "text_delta", delta="First thought.")),
    ]
    proc = _fake_process(lines)
    loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

    with (
        patch("subprocess.Popen", return_value=proc),
        redirect_stdout(io.StringIO()) as buf,
    ):
        loop._stream_pi(["pi", "-p", "--mode", "json", "prompt"], "prompt")

    output = buf.getvalue()
    assert output == "First thought.", f"Expected no leading newline, got: {repr(output)}"


def test_stream_pi_newline_after_complete_text():
    """When a text_end event fires followed by a new text_delta (e.g. after
    tool use), a newline should separate the blocks."""
    lines = [
        _json_line(_event("message_update", "text_delta", delta="Block one.")),
        _json_line(_event("message_update", "text_end", content="Block one.")),
        _json_line(_event("message_update", "text_delta", delta="Block two.")),
        _json_line(_event("message_update", "text_delta", delta=" More.")),
    ]
    proc = _fake_process(lines)
    loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

    with (
        patch("subprocess.Popen", return_value=proc),
        redirect_stdout(io.StringIO()) as buf,
    ):
        loop._stream_pi(["pi", "-p", "--mode", "json", "prompt"], "prompt")

    output = buf.getvalue()
    assert "Block one.\nBlock two." in output, (
        f"Expected newline between complete text blocks, got: {repr(output)}"
    )


def test_stream_pi_continuous_deltas_no_extra_newlines():
    """Continuous text_delta events within the same block should not get
    extra newlines between them."""
    lines = [
        _json_line(_event("message_update", "text_delta", delta="Hello ")),
        _json_line(_event("message_update", "text_delta", delta="world")),
    ]
    proc = _fake_process(lines)
    loop = RalphLoop(pi_bin="pi", stream=True, verbose=False)

    with (
        patch("subprocess.Popen", return_value=proc),
        redirect_stdout(io.StringIO()) as buf,
    ):
        loop._stream_pi(["pi", "-p", "--mode", "json", "prompt"], "prompt")

    output = buf.getvalue()
    assert output == "Hello world", f"Expected no extra newlines, got: {repr(output)}"
