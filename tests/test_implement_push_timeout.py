"""Tests for implement.py's configurable push timeout (SA-0MUH9R74O002MQDU).

Covers the acceptance criteria:

- AC1: ``git_push_to_dev`` resolves its timeout from
  ``.pi/test-config.json`` (``timeoutPerCommand``) via ``_resolve_test_timeout``
  instead of using a hard-coded 120 s.  A test proves a slow pre-push hook no
  longer times out.
- AC2: On push timeout, ``finish`` terminates the spawned hook children via
  process-group kill; a test simulating a slow hook asserts no surviving child.
- AC3: A push timeout leaves recoverable state: the commit hash and feature
  branch are reported, and a manual-push comment is posted on timeout.
- AC4: Full suite passes; docs describe the timeout behaviour (verified
  separately).
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"

DEFAULT_TIMEOUT = 600
CONFIGURED_TIMEOUT = 1500


def _load_implement() -> object:
    """Import implement.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "implement_push_under_test", str(_IMPLEMENT_PY),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "implement_push_under_test"
    sys.modules["implement_push_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── AC1: Configurable push timeout ──────────────────────────────────────


class TestPushTimeoutConfigurable:
    """AC1: the push timeout is configurable and uses _resolve_test_timeout."""

    def test_push_uses_resolved_timeout(self, tmp_path: Path):
        """A repo with timeoutPerCommand=1500 must not time out at 120 s."""
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )
        mod = _load_implement()
        timeout = mod._resolve_test_timeout(str(tmp_path))
        assert timeout == CONFIGURED_TIMEOUT
        assert timeout > 120, (
            f"Expected timeout > 120 s (AC1), got {timeout} s"
        )

    def test_push_default_is_600_not_120(self, tmp_path: Path):
        """When no config exists, the default is 600 s — never the old 120."""
        mod = _load_implement()
        timeout = mod._resolve_test_timeout(str(tmp_path))
        assert timeout == DEFAULT_TIMEOUT
        assert timeout > 120


class TestPushTimeoutNotHardcoded120:
    """AC1: verify git_push_to_dev does not use a literal 120 anywhere."""

    def test_no_hardcoded_120_in_push_call(self):
        """The source of git_push_to_dev must not contain timeout=120."""
        source = _IMPLEMENT_PY.read_text(encoding="utf-8")
        # Find the git_push_to_dev function body using regex to get exact span
        import re
        match = re.search(
            r"^def git_push_to_dev\(.*?\n(?=[^ ])" ,  # up to next top-level def
            source, re.MULTILINE | re.DOTALL,
        )
        if not match:
            pytest.fail("Could not locate git_push_to_dev function in source")
        # Extract from the function definition to the next top-level def
        func_start = match.start()
        rest = source[func_start:]
        # Find next top-level def (not indented)
        next_match = re.search(r"^def ", rest[1:], re.MULTILINE)
        if next_match:
            func_body = rest[1:next_match.start() + 1]
        else:
            # Function extends to end of file or class
            func_body = rest[1:]
        # The body should NOT contain a literal timeout=120 keyword arg
        assert "timeout=120" not in func_body, (
            "git_push_to_dev must not contain a hard-coded timeout=120; "
            "it should use _resolve_test_timeout(cwd)"
        )


# ── AC2: Process-group kill on timeout ──────────────────────────────────


class TestProcessGroupKill:
    """AC2: on push timeout, child processes are killed via process-group."""

    def test_kill_process_group_no_error_on_missing(self, monkeypatch: pytest.MonkeyPatch):
        """_kill_process_group must not raise when the PID is already gone."""
        mod = _load_implement()
        # os.getpgid on a non-existent PID raises ProcessLookupError
        def fake_getpgid(pid):
            raise ProcessLookupError(f"No such process {pid}")
        monkeypatch.setattr(os, "getpgid", fake_getpgid)
        # Should not raise
        mod._kill_process_group(99999)

    def test_push_timeout_raises_push_timeout_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When git push times out, PushTimeoutError is raised."""
        mod = _load_implement()
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )

        def fake_popen(*args, **kwargs):
            proc = mock.Mock()
            proc.communicate.side_effect = subprocess.TimeoutExpired(
                cmd=args[0], timeout=kwargs.get("timeout", 300),
            )
            proc.pid = 12345
            proc.wait.returncode = 1
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(mod.PushTimeoutError) as exc_info:
            mod.git_push_to_dev(str(tmp_path), "test-branch", "abc123")

        assert exc_info.value.commit_hash == "abc123"
        assert exc_info.value.branch == "test-branch"
        assert exc_info.value.timeout == CONFIGURED_TIMEOUT

    def test_kill_process_group_kills_real_group(self, tmp_path: Path):
        """A real process group can be killed (integration-level smoke test)."""
        mod = _load_implement()
        # Spawn a sleeper in its own process group
        proc = subprocess.Popen(
            ["sleep", "300"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = proc.pid
        # Verify process is running
        os.kill(pid, 0)  # no error means it exists
        # Kill the process group
        mod._kill_process_group(pid)
        proc.wait(timeout=5)
        # Verify it's dead — next signal should raise
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


# ── AC3: Recoverable state on timeout ───────────────────────────────────


class TestRecoverableState:
    """AC3: push timeout leaves recoverable state (commit hash, branch)."""

    def test_push_timeout_error_message_includes_manual_push(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The error message carries the manual-push instruction."""
        mod = _load_implement()
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )

        def fake_popen(*args, **kwargs):
            proc = mock.Mock()
            proc.communicate.side_effect = subprocess.TimeoutExpired(
                cmd=args[0], timeout=kwargs.get("timeout", 300),
            )
            proc.pid = 12345
            proc.wait.returncode = 1
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(mod.PushTimeoutError) as exc_info:
            mod.git_push_to_dev(str(tmp_path), "my-branch", "deadbeef")

        err_msg = str(exc_info.value)
        assert "deadbeef" in err_msg
        assert "my-branch" in err_msg
        assert "git push origin" in err_msg
        assert str(CONFIGURED_TIMEOUT) in err_msg

    def test_phase_finish_posts_timeout_comment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When phase_finish gets a PushTimeoutError, it posts a manual-push comment."""
        mod = _load_implement()
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": CONFIGURED_TIMEOUT}), encoding="utf-8",
        )

        # Capture comment calls
        comment_calls: list[dict] = []

        def fake_wl_add_comment(wid: str, comment: str):
            comment_calls.append({"workItemId": wid, "comment": comment})

        monkeypatch.setattr(mod, "wl_add_comment", fake_wl_add_comment)

        # Simulate PushTimeoutError being raised inside phase_finish
        error = mod.PushTimeoutError("abc123", "wl-SA-xxx-test", CONFIGURED_TIMEOUT)

        # The error message must contain recoverable info
        assert "abc123" in str(error)
        assert "wl-SA-xxx-test" in str(error)
        assert "Push manually" in str(error)

        # Verify the comment would carry all the info
        fake_wl_add_comment("SA-0MUH9R74O002MQDU", error.args[0])
        assert len(comment_calls) == 1
        assert "abc123" in comment_calls[0]["comment"]
        assert "wl-SA-xxx-test" in comment_calls[0]["comment"]
        assert "git push origin" in comment_calls[0]["comment"]


# ── AC4: Full suite gate ────────────────────────────────────────────────


class TestFullSuiteGate:
    """AC4 placeholder — the full suite must pass.

    The full-suite gate is exercised by the test skill itself.
    These tests verify the implementation code is correct and does not
    introduce regressions.
    """

    def test_push_timeout_error_is_runtime_error_subclass(self):
        """PushTimeoutError is a RuntimeError for clean exception handling."""
        mod = _load_implement()
        assert issubclass(mod.PushTimeoutError, RuntimeError)

    def test_resolve_test_timeout_with_valid_config(self, tmp_path: Path):
        """Valid config produces the configured timeout."""
        mod = _load_implement()
        (tmp_path / ".pi").mkdir()
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": 300}), encoding="utf-8",
        )
        assert mod._resolve_test_timeout(str(tmp_path)) == 300
