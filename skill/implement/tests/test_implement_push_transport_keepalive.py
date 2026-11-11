"""SSH keepalives and the distinct transport-lost failure (SA-0MVWNHLQQ002TFZR).

The pre-push full-suite gate runs **after** ``git push`` has opened the SSH
connection and produces no SSH traffic for its whole duration (measured 11 min
warm, 35 min cold). A remote that closes an idle connection (GitHub does)
leaves git writing the ref update to a dead socket: the gate's green verdict
is discarded, the push never lands, and the operator reads "full suite green".

Two contracts are pinned here:

* **AC1/AC2 — keepalives:** ``git_push_to_dev`` injects
  ``ServerAliveInterval``/``ServerAliveCountMax``/``TCPKeepAlive`` via
  ``GIT_SSH_COMMAND`` into the push child environment, so the fix travels with
  the code (a fresh clone/worktree) instead of depending on one operator's
  ``~/.ssh/config``. The operator's own command is preserved.
* **AC3/AC4 — transport lost is distinct from suite red:** when the retries
  are exhausted on a transport close, the push raises
  ``PushTransportLostError`` and records ``reason: connection_closed`` — the
  existing *infrastructure* classification, not ``test_failure``.

Hermetic: only ``git push`` is faked; the backoff sleep seam is recorded.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

TRANSPORT_CLOSE = (
    b"Connection to github.com closed by remote host.\n"
    b"fatal: Could not read from remote repository.\n"
)


class _FakeProc:
    """A minimal stand-in for a ``git push`` child process."""

    pid = 919191

    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self, timeout=None):
        return (b"", self._stderr)

    def wait(self):
        return self.returncode


_REAL_POPEN = subprocess.Popen


def _scripted_popen(script, calls):
    """Fake only ``git push``; everything else runs for real."""
    iterator = iter(script)

    def factory(argv, **kwargs):
        if list(argv[:2]) != ["git", "push"]:
            return _REAL_POPEN(argv, **kwargs)
        calls.append({"argv": list(argv), "env": kwargs.get("env")})
        returncode, stderr = next(iterator)
        return _FakeProc(returncode, stderr)

    return factory


@pytest.fixture
def push_harness(monkeypatch, tmp_path):
    calls: list[dict] = []

    def install(script):
        calls.clear()
        monkeypatch.setattr(
            implement.subprocess, "Popen", _scripted_popen(script, calls)
        )
        monkeypatch.setattr(
            implement, "_push_retry_sleep", lambda seconds: None
        )
        monkeypatch.setattr(
            implement, "_kill_process_group", lambda pid: None
        )

    return {"cwd": str(tmp_path), "calls": calls, "install": install}


# ── AC1/AC2 — keepalive command construction ───────────────────────────────


class TestKeepaliveCommand:
    def test_unset_command_becomes_ssh_with_keepalives(self) -> None:
        command = implement._ssh_keepalive_command(None)
        assert command.startswith("ssh ")
        for option in (
            "ServerAliveInterval=30",
            "ServerAliveCountMax=20",
            "TCPKeepAlive=yes",
        ):
            assert option in command, command

    def test_operator_ssh_args_are_preserved(self) -> None:
        command = implement._ssh_keepalive_command("ssh -i /keys/deploy -v")
        tokens = command.split()
        assert tokens[0] == "ssh"
        assert "-i" in tokens and "/keys/deploy" in tokens
        assert "-v" in tokens
        assert "ServerAliveInterval=30" in tokens
        # The keepalive options trail the ssh token but precede the operator's
        # own arguments, so both are honoured.
        assert tokens.index("ServerAliveInterval=30") < tokens.index("-i")

    def test_absolute_ssh_path_is_recognised(self) -> None:
        command = implement._ssh_keepalive_command("/usr/bin/ssh -F cfg")
        assert command.startswith("/usr/bin/ssh ")
        assert "ServerAliveInterval=30" in command
        assert "-F cfg" in command

    def test_existing_keepalive_config_is_never_second_guessed(self) -> None:
        existing = "ssh -o ServerAliveInterval=5 -o ServerAliveCountMax=3"
        assert implement._ssh_keepalive_command(existing) == existing

    def test_unknown_wrapper_is_preserved_and_warned(self, caplog) -> None:
        existing = "/usr/local/bin/my-ssh-wrapper --flag"
        with caplog.at_level(logging.WARNING, logger=implement.LOG.name):
            command = implement._ssh_keepalive_command(existing)
        assert command == existing
        assert any(
            "Cannot inject SSH keepalives" in record.getMessage()
            for record in caplog.records
            if record.name == implement.LOG.name
        )


# ── AC1/AC2 — the keepalive reaches the push child env ─────────────────────


class TestPushCarriesKeepalive:
    def test_push_child_env_gets_keepalive_by_default(
        self, push_harness, monkeypatch
    ) -> None:
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        push_harness["install"]([(0, b"")])

        implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")

        env = push_harness["calls"][0]["env"]
        assert "ServerAliveInterval=30" in env["GIT_SSH_COMMAND"]

    def test_operator_git_ssh_command_is_merged_not_replaced(
        self, push_harness, monkeypatch
    ) -> None:
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/op")
        push_harness["install"]([(0, b"")])

        implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")

        env = push_harness["calls"][0]["env"]
        assert "-i /keys/op" in env["GIT_SSH_COMMAND"]
        assert "ServerAliveInterval=30" in env["GIT_SSH_COMMAND"]

    def test_keepalive_is_confined_to_the_push_child(
        self, push_harness, monkeypatch
    ) -> None:
        """The parent process env is never mutated (no ambient leak)."""
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        push_harness["install"]([(0, b"")])

        implement.git_push_to_dev(push_harness["cwd"], "wl-test", "abc123")

        assert "GIT_SSH_COMMAND" not in os.environ


# ── AC3/AC4 — transport lost is distinct from a red suite ───────────────────


class TestTransportLostClassification:
    def test_exhausted_transport_raises_the_distinct_error(
        self, push_harness
    ) -> None:
        push_harness["install"](
            [(128, TRANSPORT_CLOSE)] * implement.PUSH_RETRY_MAX_ATTEMPTS
        )

        with pytest.raises(implement.PushTransportLostError) as excinfo:
            implement.git_push_to_dev(
                push_harness["cwd"], "wl-test", "abc123"
            )

        message = str(excinfo.value)
        assert "transport lost" in message.lower()
        assert "not a test failure" in message.lower()
        assert excinfo.value.attempts == implement.PUSH_RETRY_MAX_ATTEMPTS

    def test_evidence_records_connection_closed_not_a_gate_reason(
        self, push_harness
    ) -> None:
        push_harness["install"](
            [(128, TRANSPORT_CLOSE)] * implement.PUSH_RETRY_MAX_ATTEMPTS
        )
        evidence: dict = {}

        with pytest.raises(implement.PushTransportLostError):
            implement.git_push_to_dev(
                push_harness["cwd"],
                "wl-test",
                "abc123",
                evidence_out=evidence,
            )

        assert evidence["success"] is False
        assert evidence["reason"] == "connection_closed"
        assert evidence["transport_lost"] is True
        assert evidence["gate_reason"] is None, (
            "transport loss must not masquerade as a gate reason"
        )

    def test_connection_closed_is_infrastructure_for_the_drive_classifier(
        self,
    ) -> None:
        """Drive retries a lost transport on its infra budget, never a gate."""
        result = implement.classify_finish_failure(
            {
                "success": False,
                "steps": {
                    "push": {
                        "success": False,
                        "reason": "connection_closed",
                    }
                },
            }
        )
        assert result["kind"] == "infrastructure", result
        assert result["step"] == "push"
        assert result["reason"] == "connection_closed"

    def test_a_red_suite_is_still_not_infrastructure(self) -> None:
        """The distinction is real: ``test_failure`` is never infrastructure.

        A red dev-push suite is not on the infra allow-list, so classifies
        ``unknown`` (fail-safe: drive never retries a genuine failure as
        infrastructure), whereas ``connection_closed`` classifies
        ``infrastructure`` (retry). The two reasons are therefore handled
        differently everywhere they are consumed.
        """
        result = implement.classify_finish_failure(
            {
                "success": False,
                "steps": {
                    "push": {"success": False, "reason": "test_failure"}
                },
            }
        )
        assert result["kind"] == "unknown", result
        assert result["kind"] in implement.FINISH_FAILURE_KINDS
        assert result["reason"] == "test_failure"
