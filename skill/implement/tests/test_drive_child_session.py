"""Tests for the implement skill's driven child-session spawn command.

``implement.py drive`` spawns a fresh Pi session per child via
``_default_session_spawner``. That command MUST construct extensions-disabled
child sessions so a globally installed ``turn_end`` extension cannot trigger
Pi core's ``_dispatchTurnEndBoundary`` error (``Extension error (<boundary>):
turn_end could not resolve the persisted assistant entry ID``) and flood the
child's captured stderr (SA-0MUY9PYBD009Y7J5).

These tests exercise ``_default_session_spawner`` directly, patching the two
external boundaries (``_resolve_pi_bin`` for binary discovery and
``subprocess.run`` for the process spawn) so no real ``pi`` session is
started. They assert observable command construction via the captured
``subprocess.run`` arguments.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts.implement import _default_session_spawner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCompleted:
    """Minimal ``subprocess.CompletedProcess`` stand-in."""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _spawn(
    *,
    child_id: str = "SA-CHILD",
    verbose: bool = False,
    pi_bin: str | None = "/usr/local/bin/pi",
    proc: _FakeCompleted | None = None,
    raises: Exception | None = None,
):
    """Invoke ``_default_session_spawner`` with external boundaries patched.

    Returns a ``(result, captured)`` tuple where *captured* exposes the
    ``subprocess.run`` command and keyword arguments for assertion.
    """
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return proc or _FakeCompleted()

    with (
        patch(
            "skill.implement.scripts.implement._resolve_pi_bin",
            return_value=pi_bin,
        ),
        patch(
            "skill.implement.scripts.implement.subprocess.run",
            side_effect=_fake_run,
        ),
    ):
        result = _default_session_spawner(
            child_id,
            worktree_path="/tmp/wt",
            timeout=3600,
            env={"PATH": "/usr/local/bin"},
            verbose=verbose,
        )

    return result, captured


def _expected_cmd(child_id: str, *, verbose: bool = False) -> list[str]:
    """The canonical extensions-disabled child-session command."""
    cmd = [
        "/usr/local/bin/pi",
        "-p",
        f"/skill:implement {child_id}",
        "--approve",
        "--no-extensions",
    ]
    if verbose:
        cmd.append("--verbose")
    return cmd


# ---------------------------------------------------------------------------
# 1. Extension isolation is present in the spawned command (AC1)
# ---------------------------------------------------------------------------


class TestExtensionsDisabled:
    """The child-session command must disable extension discovery."""

    def test_command_includes_no_extensions_with_core_flags(self):
        """``-p``, the ``/skill:implement <id>`` prompt, ``--approve`` and
        ``--no-extensions`` are all present, in canonical order."""
        result, captured = _spawn(child_id="SA-CHILD")
        assert result["success"] is True
        cmd = captured["cmd"]
        assert "--no-extensions" in cmd
        assert "-p" in cmd
        assert "/skill:implement SA-CHILD" in cmd
        assert "--approve" in cmd
        assert cmd == _expected_cmd("SA-CHILD")

    def test_no_extensions_immediately_follows_approve(self):
        """The flag is placed alongside the other isolation flags (not after
        ``--verbose``) so it cannot be lost when verbose output is enabled."""
        _result, captured = _spawn(child_id="SA-CHILD", verbose=True)
        cmd = captured["cmd"]
        assert cmd.index("--no-extensions") == cmd.index("--approve") + 1

    # Regression guard: this is the "mutation" test. If a future edit removes
    # ``--no-extensions`` from ``_default_session_spawner`` the exact-equality
    # assertion below fails, making the isolation regression visible.
    def test_removing_no_extensions_would_break_the_command_contract(self):
        """A command built without ``--no-extensions`` must not match the
        contract the spawner produces — proving the flag is load-bearing."""
        contract = _expected_cmd("SA-CHILD")
        mutated = [arg for arg in contract if arg != "--no-extensions"]

        _result, captured = _spawn(child_id="SA-CHILD")

        assert captured["cmd"] == contract
        assert captured["cmd"] != mutated
        assert "--no-extensions" in captured["cmd"]


# ---------------------------------------------------------------------------
# 2. Existing spawn behaviour is preserved (AC1, AC4)
# ---------------------------------------------------------------------------


class TestSpawnBehaviourPreserved:
    """The flag must not disturb cwd/env/timeout/verbose handling."""

    def test_verbose_appends_after_no_extensions(self):
        """``--verbose`` is still honoured and appended last."""
        _result, captured = _spawn(child_id="SA-CHILD", verbose=True)
        assert captured["cmd"] == _expected_cmd("SA-CHILD", verbose=True)
        assert captured["cmd"][-1] == "--verbose"

    def test_spawn_uses_worktree_cwd_env_and_timeout(self):
        """The process still runs in the child's worktree with the child env
        and the requested timeout (capture_output/text/check unchanged)."""
        _result, captured = _spawn(child_id="SA-CHILD")
        kwargs = captured["kwargs"]
        assert kwargs["cwd"] == "/tmp/wt"
        assert kwargs["env"] == {"PATH": "/usr/local/bin"}
        assert kwargs["timeout"] == 3600
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False

    def test_missing_pi_binary_reports_actionable_reason(self):
        """When no ``pi`` binary is found the spawner returns a failure that
        points at ``IMPLEMENT_DRIVE_PI_BIN`` (unchanged behaviour)."""
        result, captured = _spawn(pi_bin=None)
        assert result["success"] is False
        assert "IMPLEMENT_DRIVE_PI_BIN" in result["reason"]
        assert "cmd" not in captured

    def test_nonzero_exit_surfaces_stderr_tail(self):
        """A failed session still surfaces the real stderr tail, not noise."""
        result, _captured = _spawn(
            child_id="SA-CHILD",
            proc=_FakeCompleted(
                stderr="boom: real failure",
                returncode=7,
            ),
        )
        assert result["success"] is False
        assert result["returncode"] == 7
        assert "boom: real failure" in result["reason"]


# ---------------------------------------------------------------------------
# 3. The flag leaves the prompt intact (AC4: /skill: expansion unaffected)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("child_id", ["SA-CHILD", "MS-0MUXWGT7G009THBO", "OSL-0MUKDXAD80052O1D"])
def test_prompt_preserved_for_various_ids(child_id):
    """The ``/skill:implement <id>`` prompt is passed verbatim — skills load
    via ``--no-skills``, not extensions, so ``--no-extensions`` must not
    affect skill expansion."""
    _result, captured = _spawn(child_id=child_id)
    assert f"/skill:implement {child_id}" in captured["cmd"]
