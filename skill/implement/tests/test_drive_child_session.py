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

The pane-driven transport (SA-0MVPHYIIE002OBC8) is covered at the end of the
module: the Herdr CLI boundary is faked with
``skill/shared/tests/fake_herdr_cli.py``, so the split → start → prompt argv,
the env isolation and the opt-in selection matrix are all asserted without a
live Herdr session.

The final section pins the per-child lock's *lifetime* (parent epic
SA-0MVXWMVTH007MNJR AC5): the driver must hold the child's lock for the whole
spawned session, exposed to that session as ``IMPLEMENT_CHILD_LOCK_TOKEN``,
and release it once the session exits. The assertions run synchronously
inside an injectable fake spawner that ``phase_drive`` awaits — no sleep, no
clock.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared.work_item_lock import ENV_LOCK_TOKEN, WorkItemLock, lock_file_path

from skill.implement.scripts import implement as impl_mod
from skill.implement.scripts.implement import _default_session_spawner
from skill.shared.tests import fake_herdr_cli as fake

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


# ---------------------------------------------------------------------------
# 4. Pane-driven transport (SA-0MVPHYIIE002OBC8)
# ---------------------------------------------------------------------------
#
# ``--pane-sessions`` drives each child as an interactive Pi TUI agent in a
# sibling Herdr pane of the calling tab. Detection + opt-in decide the
# transport (fail-open to the headless ``pi -p`` spawner); the pane spawner
# reuses ``shared.herdr_session.HerdrSessionClient``, so these tests fake the
# herdr CLI boundary and assert the *exact* invocations it builds.

PANE_CHILD = "SA-0MVPHYIIE002OBC8"
PANE_WORKTREE = "/wt/pane-child"
FAKE_HERDR_BIN = "/fake/bin/herdr"
PANE_AGENT_NAME = impl_mod.agent_name_for_child(PANE_CHILD)


def _pane_env(**overrides) -> dict:
    """The child environment ``_child_session_env`` would build in Herdr."""
    env = {
        "PATH": "/usr/local/bin",
        "HERDR_ENV": "1",
        "HERDR_BIN_PATH": FAKE_HERDR_BIN,
    }
    env.update(overrides)
    return env


def _pane_script(*, status: str = "done") -> list[dict]:
    """The three successful herdr responses: split, start, prompt."""
    return [
        {"stdout": fake.pane_info_response("w1:p9")},
        {"stdout": fake.agent_started_response(PANE_AGENT_NAME, "w1:p9")},
        {
            "stdout": fake.agent_prompted_response(
                PANE_AGENT_NAME, "w1:p9", status=status,
            )
        },
    ]


def _pane_client(script: list[dict], *, env: dict | None = None):
    """A ``HerdrSessionClient`` wired to a recording fake herdr CLI."""
    runner = fake.FakeHerdrRunner(script)
    client = impl_mod.HerdrSessionClient(
        herdr_bin=FAKE_HERDR_BIN, env=env or _pane_env(), runner=runner,
    )
    return client, runner


@pytest.fixture(autouse=True)
def _neutralise_ambient_pane_transport(monkeypatch):
    """Keep this module hermetic when the suite runs inside a driven session.

    A driven session exports ``IMPLEMENT_DRIVE_ACTIVE=1`` (recursion guard) and
    inherits ``HERDR_ENV=1`` with a real herdr binary on PATH, so an ambient
    opt-in variable would silently switch the ``phase_drive`` tests to the pane
    transport — or make them refuse to drive at all. Remove all four and let
    each pane test opt in explicitly.
    """
    for name in (
        impl_mod.PANE_SESSIONS_ENV,
        impl_mod.DRIVE_RECURSION_ENV,
        "HERDR_ENV",
        "HERDR_BIN_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


class TestPaneSessionSpawner:
    """``_pane_session_spawner`` builds split → start → prompt, fail-open."""

    def test_splits_the_callers_tab_pane_and_prompts_the_child(self):
        env = impl_mod._child_session_env(
            _pane_env(), worktree_path=PANE_WORKTREE,
        )
        client, runner = _pane_client(_pane_script(), env=env)

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 3600, env, client=client,
        )

        calls = runner.arguments()
        assert calls == [
            [
                "pane", "split", "--current", "--direction", "right",
                "--cwd", PANE_WORKTREE,
                "--env", "IMPLEMENT_DRIVE_ACTIVE=1",
                "--env", f"IMPLEMENT_WORKTREE_PATH={PANE_WORKTREE}",
                "--no-focus",
            ],
            [
                "agent", "start", PANE_AGENT_NAME, "--kind", "pi",
                "--pane", "w1:p9", "--timeout", "300000",
                "--", "--approve", "--no-extensions",
            ],
            [
                "agent", "prompt", PANE_AGENT_NAME,
                f"/skill:implement {PANE_CHILD}", "--wait",
                "--until", "done", "--until", "blocked",
                "--timeout", "3600000",
            ],
        ]
        # No tab/workspace is ever created: children are siblings in the
        # caller's tab (AC1).
        for argv in calls:
            assert "tab" not in argv
            assert "workspace" not in argv
        assert result["success"] is True
        assert result["pane_id"] == "w1:p9"
        assert result["transport"] == "pane"
        assert result["returncode"] == 0
        assert result["agent_status"] == "done"

    def test_result_shape_is_a_superset_of_the_headless_spawner(self):
        client, _runner = _pane_client(_pane_script())

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )

        for key in ("success", "returncode", "reason"):
            assert key in result
        assert result["pane_id"]
        assert result["transport"] == "pane"

    def test_default_focus_is_no_focus_and_focus_switches_the_flag(self):
        client, runner = _pane_client(_pane_script())
        impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )
        assert "--no-focus" in runner.arguments()[0]
        assert "--focus" not in runner.arguments()[0]

        focus_client, focus_runner = _pane_client(_pane_script())
        impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(),
            client=focus_client, focus=True,
        )
        split_argv = focus_runner.arguments()[0]
        assert "--focus" in split_argv
        assert "--no-focus" not in split_argv

    def test_split_failure_is_fail_open_and_names_the_herdr_reason(self):
        client, runner = _pane_client([
            {"stdout": fake.error_response("pane_limit_reached", "no room"), "exit_code": 1},
        ])

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )

        assert result["success"] is False
        assert result["pane_id"] is None
        assert "pane_limit_reached" in result["reason"]
        # The chain stops after the failed split: no agent is ever started.
        assert len(runner.arguments()) == 1

    def test_start_failure_is_reported_with_the_created_pane(self):
        client, runner = _pane_client([
            {"stdout": fake.pane_info_response("w1:p9")},
            {
                "stdout": fake.error_response(
                    "pane_not_found", "pane vanished",
                ),
                "exit_code": 1,
            },
        ])

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )

        assert result["success"] is False
        assert "pane_not_found" in result["reason"]
        # The pane exists: the caller can retain/report it (AC4 next slice).
        assert result["pane_id"] == "w1:p9"
        assert len(runner.arguments()) == 2

    def test_agent_not_ready_is_retried_within_the_readiness_budget(self):
        now = [0.0]

        def _clock() -> float:
            return now[0]

        def _sleep(seconds: float) -> None:
            now[0] += seconds

        runner = fake.FakeHerdrRunner([
            {"stdout": fake.pane_info_response("w1:p9")},
            {
                "stdout": fake.error_response(
                    "agent_not_ready", "pane is not at a shell prompt",
                ),
                "exit_code": 1,
            },
            {"stdout": fake.agent_started_response(PANE_AGENT_NAME, "w1:p9")},
            {
                "stdout": fake.agent_prompted_response(
                    PANE_AGENT_NAME, "w1:p9", status="done",
                )
            },
        ])
        client = impl_mod.HerdrSessionClient(
            herdr_bin=FAKE_HERDR_BIN,
            env=_pane_env(),
            runner=runner,
            clock=_clock,
            sleep=_sleep,
        )

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )

        assert result["success"] is True
        # split, start (not ready), start (ready), prompt
        assert [argv[:2] for argv in runner.arguments()] == [
            ["pane", "split"], ["agent", "start"],
            ["agent", "start"], ["agent", "prompt"],
        ]

    def test_prompt_timeout_is_reported_as_a_timeout(self):
        client, _runner = _pane_client([
            {"stdout": fake.pane_info_response("w1:p9")},
            {"stdout": fake.agent_started_response(PANE_AGENT_NAME, "w1:p9")},
            {"stdout": fake.error_response("timeout", "wait expired"), "exit_code": 1},
        ])

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 600, _pane_env(), client=client,
        )

        assert result["success"] is False
        # The inherited reconciliation classifies on this phrase.
        assert "timed out" in result["reason"]
        assert result["pane_id"] == "w1:p9"

    def test_blocked_agent_settles_without_being_reported_as_a_crash(self):
        client, _runner = _pane_client(_pane_script(status="blocked"))

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, _pane_env(), client=client,
        )

        # A blocked turn is a settled state, not a transport failure: the
        # spawner reports it so the lifecycle slice can retain the pane.
        assert result["success"] is True
        assert result["agent_status"] == "blocked"
        assert result["pane_id"] == "w1:p9"

    # SA-0MW7PAKSF008H8OH — a real (multi-minute) child turn must outlive the
    # client's 60 s default subprocess timeout. The default client is built
    # with a subprocess timeout scaled *above* the child budget, so a turn that
    # runs its full budget is stopped by the herdr CLI's own ``--timeout``
    # verdict, never killed by the client. An injected client is left alone —
    # tests own its timeout.
    def test_default_client_subprocess_timeout_scales_with_child_budget(self, monkeypatch):
        """AC2: the client's subprocess timeout exceeds the child budget."""
        runner = fake.FakeHerdrRunner(_pane_script())
        monkeypatch.setattr(subprocess, "run", runner)
        child_budget = 600

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, child_budget, _pane_env(),
        )

        assert result["success"] is True
        assert len(runner.kwargs) == 3
        # Every herdr subprocess call (split, start, prompt) runs under the
        # scaled timeout — the prompt wait (the long turn) above all.
        assert all(
            kwargs["timeout"] > child_budget for kwargs in runner.kwargs
        )

    def test_long_turn_records_the_cli_timeout_verdict_never_a_client_kill(self, monkeypatch):
        """AC1: a >60 s turn surfaces the herdr CLI's ``--timeout`` verdict,
        not the client's ``herdr call timed out`` kill message."""
        runner = fake.FakeHerdrRunner([
            {"stdout": fake.pane_info_response("w1:p9")},
            {"stdout": fake.agent_started_response(PANE_AGENT_NAME, "w1:p9")},
            {
                "stdout": fake.error_response("timeout", "wait expired"),
                "exit_code": 1,
            },
        ])
        monkeypatch.setattr(subprocess, "run", runner)
        child_budget = 120

        result = impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, child_budget, _pane_env(),
        )

        assert result["success"] is False
        assert "timed out after 120s" in result["reason"]
        assert "herdr call timed out" not in result["reason"]
        # The client did not kill the subprocess: the budget-scale timeout was
        # in place for the wait that produced the CLI verdict.
        assert runner.kwargs[2]["timeout"] > child_budget


class TestPaneEnvironmentIsolation:
    """The child pane gets the markers but never the driving identity (AC3)."""

    def test_pane_env_pairs_are_an_allow_list(self):
        env = impl_mod._child_session_env(
            {
                "PATH": "/usr/local/bin",
                "PI_SESSION_ID": "parent-id",
                "PI_SESSION_FILE": "/sessions/parent.jsonl",
                "DEEPSEEK_API_KEY": "secret",
            },
            worktree_path=PANE_WORKTREE,
        )

        pairs = impl_mod._pane_child_env(env)

        assert pairs == {
            "IMPLEMENT_DRIVE_ACTIVE": "1",
            "IMPLEMENT_WORKTREE_PATH": PANE_WORKTREE,
        }
        assert "PI_SESSION_ID" not in pairs
        assert "PI_SESSION_FILE" not in pairs
        assert "DEEPSEEK_API_KEY" not in pairs

    def test_split_env_args_and_herdr_process_env_carry_no_identity(self):
        child_env = impl_mod._child_session_env(
            {
                "PATH": "/usr/local/bin",
                "PI_SESSION_ID": "parent-id",
                "PI_SESSION_FILE": "/sessions/parent.jsonl",
            },
            worktree_path=PANE_WORKTREE,
        )
        client, runner = _pane_client(_pane_script(), env=child_env)

        impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, child_env, client=client,
        )

        split_argv = runner.arguments()[0]
        env_pairs = [
            split_argv[index + 1]
            for index, token in enumerate(split_argv)
            if token == "--env"
        ]
        assert env_pairs == [
            "IMPLEMENT_DRIVE_ACTIVE=1",
            f"IMPLEMENT_WORKTREE_PATH={PANE_WORKTREE}",
        ]
        # ``agent start`` has no unset option, so the ``env -u PI_SESSION_ID
        # -u PI_SESSION_FILE`` equivalent is applied to the herdr CLI itself.
        for kwargs in runner.kwargs:
            assert kwargs["env"]["IMPLEMENT_DRIVE_ACTIVE"] == "1"
            assert "PI_SESSION_ID" not in kwargs["env"]
            assert "PI_SESSION_FILE" not in kwargs["env"]

    def test_default_client_is_built_from_the_scrubbed_child_env(self, monkeypatch):
        captured: dict = {}
        client, _runner = _pane_client(_pane_script())
        child_env = impl_mod._child_session_env(
            {"PI_SESSION_ID": "parent", "PI_SESSION_FILE": "/p"},
            worktree_path=PANE_WORKTREE,
        )

        def _factory(**kwargs):
            captured.update(kwargs)
            return client

        monkeypatch.setattr(impl_mod, "HerdrSessionClient", _factory)

        impl_mod._pane_session_spawner(
            PANE_CHILD, PANE_WORKTREE, 60, dict(child_env),
        )

        assert captured["env"]["IMPLEMENT_DRIVE_ACTIVE"] == "1"
        assert "PI_SESSION_ID" not in captured["env"]
        assert "PI_SESSION_FILE" not in captured["env"]


class TestSessionSpawnerSelection:
    """Detection + opt-in pick the transport; everything else falls back."""

    @pytest.mark.parametrize(
        ("pane_sessions", "env", "herdr_bin", "expect_pane"),
        [
            # Herdr present + explicit opt-in → pane transport.
            (True, {"HERDR_ENV": "1"}, FAKE_HERDR_BIN, True),
            # Opted in but no resolvable binary → headless.
            (True, {"HERDR_ENV": "1"}, None, False),
            # Opted in but not inside Herdr → headless.
            (True, {}, FAKE_HERDR_BIN, False),
            # Env opt-in counts the same as the flag.
            (None, {"HERDR_ENV": "1", impl_mod.PANE_SESSIONS_ENV: "1"},
             FAKE_HERDR_BIN, True),
            # Herdr present but nobody opted in → headless.
            (None, {"HERDR_ENV": "1"}, FAKE_HERDR_BIN, False),
            # Explicit opt-out wins over an ambient env opt-in.
            (False, {"HERDR_ENV": "1", impl_mod.PANE_SESSIONS_ENV: "1"},
             FAKE_HERDR_BIN, False),
        ],
    )
    def test_transport_selection_matrix(
        self, monkeypatch, pane_sessions, env, herdr_bin, expect_pane,
    ):
        def _headless(*_args, **_kwargs):
            return {"success": True, "returncode": 0, "reason": ""}

        monkeypatch.setattr(
            impl_mod, "_resolve_herdr_bin_path", lambda _env: herdr_bin,
        )

        spawner = impl_mod._select_session_spawner(
            pane_sessions, env=env, default=_headless,
        )

        if expect_pane:
            assert isinstance(spawner, functools.partial)
            assert spawner.func is impl_mod._pane_session_spawner
        else:
            assert spawner is _headless

    def test_focus_is_threaded_into_the_selected_pane_spawner(self, monkeypatch):
        monkeypatch.setattr(
            impl_mod, "_resolve_herdr_bin_path", lambda _env: FAKE_HERDR_BIN,
        )

        spawner = impl_mod._select_session_spawner(
            True, focus=True, env={"HERDR_ENV": "1"},
        )

        assert spawner.keywords["focus"] is True

    def test_default_transport_outside_herdr_is_the_headless_spawner(
        self, monkeypatch,
    ):
        monkeypatch.setattr(
            impl_mod, "_resolve_herdr_bin_path", lambda _env: None,
        )

        spawner = impl_mod._select_session_spawner(
            True, env={"HERDR_ENV": "1"},
        )

        assert spawner is impl_mod._default_session_spawner


class TestPhaseDrivePaneTransport:
    """``phase_drive`` wires the selected transport end to end (AC1/AC5)."""

    def test_drives_a_child_through_the_pane_transport(self, monkeypatch):
        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_BIN_PATH", FAKE_HERDR_BIN)
        client, runner = _pane_client(_pane_script())
        parent_reports = [
            {"success": True, "next_child": PANE_CHILD,
             "worktree_path": PANE_WORKTREE},
            {"success": True, "parent_advanced": True, "message": "advanced",
             "children_processed": []},
        ]

        def _parent_runner(action, work_item_id, no_refactor=False, verbose=False):
            return parent_reports.pop(0)

        with (
            patch.object(impl_mod, "wl_show", return_value={"id": "SA-PARENT"}),
            patch.object(impl_mod, "wl_show_children", return_value=[
                {"id": PANE_CHILD, "status": "open", "stage": "intake_complete"},
            ]),
            patch.object(impl_mod, "_discover_worktree", return_value=""),
            patch.object(impl_mod, "wl_add_comment", return_value=True),
        ):
            report = impl_mod.phase_drive(
                "SA-PARENT",
                parent_runner=_parent_runner,
                pane_sessions=True,
                pane_client=client,
            )

        assert report["parent_advanced"] is True
        assert report["success"] is True
        assert report["sessions_spawned"] == 1
        # The production selector chose the pane spawner: herdr really ran.
        assert [argv[:2] for argv in runner.arguments()] == [
            ["pane", "split"], ["agent", "start"], ["agent", "prompt"],
        ]
        entry = report["children_driven"][0]
        assert entry["id"] == PANE_CHILD
        assert entry["result"] == "finished"

    def test_drive_without_opt_in_keeps_the_headless_spawner(self):
        spawned: list[str] = []

        def _headless(child_id, worktree_path, timeout, env, verbose=False):
            spawned.append(child_id)
            return {"success": True, "returncode": 0, "reason": ""}

        parent_reports = [
            {"success": True, "next_child": PANE_CHILD,
             "worktree_path": PANE_WORKTREE},
            {"success": True, "parent_advanced": True, "message": "advanced",
             "children_processed": []},
        ]

        with (
            patch.object(impl_mod, "wl_show", return_value={"id": "SA-PARENT"}),
            patch.object(impl_mod, "wl_show_children", return_value=[
                {"id": PANE_CHILD, "status": "open", "stage": "intake_complete"},
            ]),
            patch.object(impl_mod, "_discover_worktree", return_value=""),
            patch.object(impl_mod, "wl_add_comment", return_value=True),
        ):
            report = impl_mod.phase_drive(
                "SA-PARENT",
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                spawn_session=_headless,
            )

        assert spawned == [PANE_CHILD]
        assert report["parent_advanced"] is True

    def test_drive_closes_the_pane_once_the_child_is_terminal(self, monkeypatch):
        """Terminality (from the next `parent` re-read) is the close signal."""
        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_BIN_PATH", FAKE_HERDR_BIN)
        script = [*_pane_script(), {"stdout": fake.ok_response()}]
        client, runner = _pane_client(script)
        parent_reports = [
            {"success": True, "next_child": PANE_CHILD,
             "worktree_path": PANE_WORKTREE},
            {"success": True, "parent_advanced": True, "message": "advanced",
             "children_processed": [{
                 "id": PANE_CHILD, "status": "completed", "action": "finished",
             }]},
        ]

        with (
            patch.object(impl_mod, "wl_show", return_value={"id": "SA-PARENT"}),
            patch.object(impl_mod, "wl_show_children", return_value=[
                {"id": PANE_CHILD, "status": "open", "stage": "intake_complete"},
            ]),
            patch.object(impl_mod, "_discover_worktree", return_value=""),
            patch.object(impl_mod, "wl_add_comment", return_value=True),
        ):
            report = impl_mod.phase_drive(
                "SA-PARENT",
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                pane_sessions=True,
                pane_client=client,
            )

        assert report["success"] is True
        # The pane is closed exactly once, after the terminal confirmation.
        assert runner.arguments()[-1] == ["pane", "close", "w1:p9"]
        entry = report["children_driven"][0]
        assert entry["transport"] == "pane"
        assert entry["pane_id"] == "w1:p9"
        assert entry["pane_closed"] is True
        assert "retained_panes" not in report

    def test_blocked_pane_is_retained_and_never_closed(
        self, monkeypatch,
    ):
        """A `blocked` session stops the chain; its pane is left open."""
        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_BIN_PATH", FAKE_HERDR_BIN)
        monkeypatch.setattr(
            impl_mod, "_update_pane_title",
            lambda status, **kw: {"updated": False, "status": status},
        )
        client, runner = _pane_client(_pane_script(status="blocked"))
        parent_reports = [
            {"success": True, "next_child": PANE_CHILD,
             "worktree_path": PANE_WORKTREE},
        ]

        with (
            patch.object(impl_mod, "wl_show", return_value={"id": "SA-PARENT"}),
            patch.object(impl_mod, "wl_show_children", return_value=[
                {"id": PANE_CHILD, "status": "open", "stage": "intake_complete"},
            ]),
            patch.object(impl_mod, "_discover_worktree", return_value=""),
            patch.object(impl_mod, "wl_add_comment", return_value=True),
        ):
            report = impl_mod.phase_drive(
                "SA-PARENT",
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                pane_sessions=True,
                pane_client=client,
            )

        assert report["success"] is False
        assert report["blocked_child"] == PANE_CHILD
        assert report["blocked_pane"] == "w1:p9"
        # No `pane close` was ever issued: split, start, prompt only.
        assert [argv[:2] for argv in runner.arguments()] == [
            ["pane", "split"], ["agent", "start"], ["agent", "prompt"],
        ]
        assert report["children_driven"][0]["result"] == "blocked"
        retained = report["retained_panes"]
        assert [p["pane_id"] for p in retained] == ["w1:p9"]
        assert retained[0]["remedy"] == "herdr pane read w1:p9"


# ---------------------------------------------------------------------------
# 5. The per-child lock lives for the driven session's whole lifetime
# ---------------------------------------------------------------------------
#
# Parent epic SA-0MVXWMVTH007MNJR AC5. While a driven child session is alive,
# ``<worklog>/locks/child-<id>.lock`` must be held by a live kernel ``flock``
# and name the **live driver's** pid, and the spawned session must receive the
# matching ownership token. Only then does the herdr hydrator's
# "busy = pane OR live claim holder" rule keep the child ``in-progress``
# instead of demoting it mid-implementation.
#
# The assertions run *inside* the injectable fake spawner, which
# ``phase_drive`` calls synchronously and waits on, so the check is made while
# the lock is provably supposed to be held — no sleep, no clock, no race.
#
# TDD ordering: this slice landed first and the fixing change set is
# SA-0MW7LMUUQ00701KJ (``phase_drive`` acquires the child lock before spawning
# and releases it after the session exits). That change set removed the
# ``xfail(strict=True)`` markers these tests originally carried; a strict
# xpass was the reminder, and the tests are now unconditional.
#
# The harness precondition — that ``phase_drive`` really calls the injected
# spawner and awaits it — is proven independently, before and after the fix,
# by ``TestPhaseDrivePaneTransport.test_drive_without_opt_in_keeps_the_
# headless_spawner`` above, so a failure here can only be about the lock.

LOCK_PARENT = "SA-LOCKPARENT"
LOCK_CHILD = "SA-LOCKCHILD"
LOCK_CHILD_TWO = "SA-LOCKCHILD2"
LOCK_WORKTREE = "/tmp/wt-lock-child"
LOCK_WORKTREE_TWO = "/tmp/wt-lock-child-two"


@pytest.fixture(autouse=True)
def _hermetic_child_lock_state(monkeypatch):
    """Never observe an ambient driver token or a leaked held lock.

    A driven child session exports ``IMPLEMENT_CHILD_LOCK_TOKEN``; inheriting
    it would make an "independent contender" probe a token-based re-entrant
    no-op instead of being refused. The module global is reset and any lock the
    code under test leaves behind is released at teardown.
    """
    monkeypatch.delenv(ENV_LOCK_TOKEN, raising=False)
    monkeypatch.setattr(impl_mod, "_child_lock_global", None)
    yield
    lock = impl_mod._child_lock_global
    if lock is not None:
        lock.release()
    impl_mod._child_lock_global = None  # monkeypatch restores the original


@contextlib.contextmanager
def _without_ambient_lock_token():
    """Unset the ambient ownership token so a probe is a *different* owner."""
    token = os.environ.pop(ENV_LOCK_TOKEN, None)
    try:
        yield
    finally:
        if token is not None:
            os.environ[ENV_LOCK_TOKEN] = token


def _lock_store() -> Path:
    """The per-test throwaway worklog store the child lock resolves to.

    ``skill/implement/tests/conftest.py`` points ``_child_lock_dir`` at a
    per-test directory via ``IMPLEMENT_CHILD_LOCK_DIR``, so the driver under
    test and the assertions below read and write the same lock file.
    """
    store = os.environ.get("IMPLEMENT_CHILD_LOCK_DIR")
    assert store, (
        "the package conftest fixture must point IMPLEMENT_CHILD_LOCK_DIR at "
        "a throwaway store"
    )
    return Path(store)


def _child_lock_metadata(child_id: str, store: Path) -> dict:
    """Read *child_id*'s lock metadata, failing when no lock file exists."""
    path = lock_file_path(child_id, store)
    assert path.is_file(), (
        f"no per-child lock file at {path} while the driver was spawning "
        f"{child_id}'s session: the driver must hold the child lock for the "
        "session's whole lifetime (SA-0MVXWMVTH007MNJR AC1)"
    )
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict), metadata
    return metadata


def _assert_pid_alive(pid, child_id: str) -> None:
    """Assert *pid* names a live process (the hydrator trusts only these)."""
    assert isinstance(pid, int) and pid > 0, (
        f"the lock file for {child_id} records a non-pid {pid!r}"
    )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise AssertionError(
            f"the lock file for {child_id} records pid {pid}, which is not a "
            "live process: the recorded owner is stale, so the herdr hydrator "
            "would demote the child mid-session (SA-0MVXWMVTH007MNJR AC1)"
        ) from None
    except PermissionError:  # pragma: no cover - pid owned by another user
        return  # the process exists; only our permission is missing


def _assert_lock_held_for_session(
    child_id: str, store: Path, env: dict[str, str],
) -> dict:
    """Assert, synchronously inside the spawner, that the driver holds the lock.

    Covers the three facts the hydrator and the driven session depend on:

    1. the kernel ``flock`` is live — an independent contender is refused;
    2. the lock file's recorded ``pid`` is the live driver process;
    3. the spawned session's environment carries the lock's ownership token
       (so its own ``start``/``finish`` re-enter the lock instead of blocking).

    Returns:
        The lock metadata observed while the session was running.
    """
    metadata = _child_lock_metadata(child_id, store)

    with _without_ambient_lock_token():
        probe = WorkItemLock(
            child_id,
            agent="intruder",
            session_id="intruder-probe",
            worklog_dir=store,
        )
        try:
            result = probe.acquire()
        finally:
            probe.release()
    assert result.acquired is False, (
        f"the driver did not hold {child_id}'s child lock while the session "
        "it spawned was running: an independent contender acquired it "
        "(SA-0MVXWMVTH007MNJR AC1)"
    )
    assert (result.held_by or {}).get("pid") == metadata.get("pid"), (
        "the contender was refused by a different owner than the one the lock "
        f"file records: held_by={result.held_by!r}, file={metadata!r}"
    )

    recorded_pid = metadata.get("pid")
    _assert_pid_alive(recorded_pid, child_id)
    assert recorded_pid == os.getpid(), (
        "the lock file must record the live driver process's pid "
        f"(expected {os.getpid()}, recorded {recorded_pid!r}) so the hydrator "
        "sees a live claim holder (SA-0MVXWMVTH007MNJR AC1)"
    )

    token = env.get(ENV_LOCK_TOKEN)
    assert token, (
        f"the spawned session for {child_id} did not receive "
        f"{ENV_LOCK_TOKEN}: its own start/finish would be refused as "
        "`skipped-held` instead of re-entering the lock "
        "(SA-0MVXWMVTH007MNJR AC2)"
    )
    assert token == metadata.get("token"), (
        "the token handed to the spawned session does not match the lock "
        f"file's token (env={token!r}, file={metadata.get('token')!r}) "
        "(SA-0MVXWMVTH007MNJR AC2)"
    )
    return metadata


def _assert_lock_free(child_id: str, store: Path) -> None:
    """Assert *child_id*'s lock is free (released when the session exited)."""
    with _without_ambient_lock_token():
        probe = WorkItemLock(
            child_id, agent="next", session_id="next-session", worklog_dir=store,
        )
        try:
            result = probe.acquire()
            assert result.acquired is True, (
                f"{child_id}'s child lock is still held after the driven "
                "session returned; the driver must release it once the "
                f"session exits (held by {result.held_by!r}) "
                "(SA-0MVXWMVTH007MNJR AC3)"
            )
        finally:
            probe.release()


@contextlib.contextmanager
def _driven_phase(children: list[dict]):
    """Patch ``phase_drive``'s worklog and worktree boundaries (no store I/O)."""
    with (
        patch.object(impl_mod, "wl_show", return_value={"id": LOCK_PARENT}),
        patch.object(impl_mod, "wl_show_children", return_value=children),
        patch.object(impl_mod, "_discover_worktree", return_value=""),
        patch.object(impl_mod, "wl_add_comment", return_value=True),
    ):
        yield


def _child_record(child_id: str) -> dict:
    """A startable child as ``wl_show_children`` reports it."""
    return {"id": child_id, "status": "open", "stage": "intake_complete"}


def _spawned() -> dict:
    """A successful spawner result (headless transport shape)."""
    return {"success": True, "returncode": 0, "reason": ""}


class TestDriveHoldsChildLockForSessionLifetime:
    """``phase_drive`` owns the child lock while the spawned session runs."""

    def test_lock_is_held_inside_the_spawner_and_released_afterwards(self):
        """Held for the session's lifetime (flock live, pid live, token passed)."""
        store = _lock_store()
        observed: list[str] = []

        def _fake_spawner(
            child_id, worktree_path, timeout, env, verbose=False,
        ):
            # All three facts are asserted here, synchronously: ``phase_drive``
            # is waiting on this call, so the session is "alive".
            _assert_lock_held_for_session(child_id, store, env)
            observed.append(child_id)
            return _spawned()

        parent_reports = [
            {
                "success": True,
                "next_child": LOCK_CHILD,
                "worktree_path": LOCK_WORKTREE,
            },
            {
                "success": True,
                "parent_advanced": True,
                "message": "advanced",
                "children_processed": [],
            },
        ]

        with _driven_phase([_child_record(LOCK_CHILD)]):
            report = impl_mod.phase_drive(
                LOCK_PARENT,
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                spawn_session=_fake_spawner,
            )

        assert observed == [LOCK_CHILD], (
            "the fake spawner never ran, so the lock lifetime was never "
            f"observed: {observed}"
        )
        assert report["sessions_spawned"] == 1, report
        assert report["success"] is True, report["message"]
        assert report["parent_advanced"] is True, report

        # AC3: released once the spawner (the session) returned.
        _assert_lock_free(LOCK_CHILD, store)

    def test_each_child_gets_a_fresh_lock_token(self):
        """A second child never inherits the first child's token (no reuse)."""
        store = _lock_store()
        observed: dict[str, dict] = {}

        def _fake_spawner(
            child_id, worktree_path, timeout, env, verbose=False,
        ):
            observed[child_id] = _assert_lock_held_for_session(
                child_id, store, env,
            )
            return _spawned()

        parent_reports = [
            {
                "success": True,
                "next_child": LOCK_CHILD,
                "worktree_path": LOCK_WORKTREE,
            },
            {
                "success": True,
                "next_child": LOCK_CHILD_TWO,
                "worktree_path": LOCK_WORKTREE_TWO,
            },
            {
                "success": True,
                "parent_advanced": True,
                "message": "advanced",
                "children_processed": [],
            },
        ]

        with _driven_phase([_child_record(LOCK_CHILD), _child_record(LOCK_CHILD_TWO)]):
            report = impl_mod.phase_drive(
                LOCK_PARENT,
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                spawn_session=_fake_spawner,
            )

        assert set(observed) == {LOCK_CHILD, LOCK_CHILD_TWO}, observed
        assert report["sessions_spawned"] == 2, report

        first = observed[LOCK_CHILD]
        second = observed[LOCK_CHILD_TWO]
        # Each lock file records its own ownership token, and the env token
        # handed to each session matched that child's lock (asserted above), so
        # the two tokens being distinct proves there is no stale-token reuse.
        assert first["token"] != second["token"], (
            "the second child inherited the first child's lock token; a stale "
            "token would let `_probe_child_lock` misclassify a different "
            "child's lock as self-owned (SA-0MVXWMVTH007MNJR risk: stale "
            "token reuse across children)"
        )
        _assert_lock_free(LOCK_CHILD, store)
        _assert_lock_free(LOCK_CHILD_TWO, store)

    def test_release_is_bounded_to_the_session(self):
        """AC3: success, non-zero exit and timeout all leave no stale flock.

        The lock is held *inside* each spawner call (the session is alive) and
        free immediately after ``phase_drive`` returns, whatever the session's
        outcome — so the next ``parent``/``start`` acquires it cleanly.
        """
        store = _lock_store()
        outcomes = [
            {"success": True, "returncode": 0, "reason": ""},
            {"success": False, "returncode": 1, "reason": "session failed"},
            {"success": False, "returncode": -9, "reason": "session timed out"},
        ]
        for outcome in outcomes:
            observed: list[str] = []

            def _fake_spawner(
                child_id, worktree_path, timeout, env, verbose=False,
                _outcome=outcome, _observed=observed,
            ):
                _assert_lock_held_for_session(child_id, store, env)
                _observed.append(child_id)
                return dict(_outcome)

            parent_reports = [
                {
                    "success": True,
                    "next_child": LOCK_CHILD,
                    "worktree_path": LOCK_WORKTREE,
                },
                # Only consumed when the session succeeded: a failure stops
                # the chain, so its lock release is observed on return.
                {
                    "success": True,
                    "parent_advanced": True,
                    "message": "advanced",
                    "children_processed": [],
                },
            ]
            # The failure paths reconcile the child against the store and
            # inspect its worktree; those boundaries are covered elsewhere, so
            # they are stubbed to isolate the lock's lifetime (AC3).
            with (
                _driven_phase([_child_record(LOCK_CHILD)]),
                patch.object(
                    impl_mod,
                    "_reconcile_spawn_failure",
                    return_value={
                        "timed_out": outcome["returncode"] == -9,
                        "timeout_seconds": 1,
                        "terminal": False,
                        "status": "in_progress",
                        "stage": "in_progress",
                        "reason": outcome["reason"],
                        "evidence": "",
                    },
                ),
                patch.object(
                    impl_mod,
                    "_release_orphaned_child_claim",
                    return_value={"released": False, "reason": "stubbed"},
                ),
                patch.object(
                    impl_mod,
                    "_detect_stranded_child",
                    return_value={
                        "stranded": False,
                        "commit": None,
                        "evidence": "",
                        "reason": "",
                    },
                ),
            ):
                report = impl_mod.phase_drive(
                    LOCK_PARENT,
                    parent_runner=(
                        lambda *a, _reports=parent_reports, **k:
                        _reports.pop(0)
                    ),
                    spawn_session=_fake_spawner,
                )

            assert observed == [LOCK_CHILD], (outcome, observed)
            assert report["sessions_spawned"] == 1, report
            _assert_lock_free(LOCK_CHILD, store)

    def test_lock_is_released_when_the_spawner_raises(self):
        """AC3: the release is in a ``finally``, so a raise cannot leak it."""
        store = _lock_store()

        def _fake_spawner(child_id, worktree_path, timeout, env, verbose=False):
            _assert_lock_held_for_session(child_id, store, env)
            raise RuntimeError("spawner blew up")

        parent_reports = [
            {
                "success": True,
                "next_child": LOCK_CHILD,
                "worktree_path": LOCK_WORKTREE,
            },
        ]

        with (
            _driven_phase([_child_record(LOCK_CHILD)]),
            pytest.raises(RuntimeError, match="spawner blew up"),
        ):
            impl_mod.phase_drive(
                LOCK_PARENT,
                parent_runner=lambda *a, **k: parent_reports.pop(0),
                spawn_session=_fake_spawner,
            )

        _assert_lock_free(LOCK_CHILD, store)

    def test_a_different_live_owner_is_refused_with_skipped_held(self):
        """AC4: a contended child is reported held and no session is spawned."""
        store = _lock_store()
        other = WorkItemLock(
            LOCK_CHILD,
            agent="other-session",
            session_id="sess-other",
            worklog_dir=store,
        )
        contended = other.acquire()
        assert contended.acquired is True, contended

        spawned: list[str] = []

        def _fake_spawner(child_id, worktree_path, timeout, env, verbose=False):
            spawned.append(child_id)
            return _spawned()

        parent_reports = [
            {
                "success": True,
                "next_child": LOCK_CHILD,
                "worktree_path": LOCK_WORKTREE,
                "children_processed": [],
            },
        ]

        try:
            with _driven_phase([_child_record(LOCK_CHILD)]):
                report = impl_mod.phase_drive(
                    LOCK_PARENT,
                    parent_runner=lambda *a, **k: parent_reports.pop(0),
                    spawn_session=_fake_spawner,
                )
        finally:
            other.release()

        assert spawned == [], (
            "a session was spawned for a child whose lock a different live "
            "owner holds (SA-0MW7LMUUQ00701KJ AC4)"
        )
        assert report["sessions_spawned"] == 0, report
        assert report["action"] == "skipped-held", report
        assert report["skipped_held"] is True, report
        assert report["success"] is False, report
        assert report["held_by"]["agent"] == "other-session", report
        assert report["held_children"] == [LOCK_CHILD], report
        assert str(store) in report["lock_path"], report

    def test_signal_cleanup_does_not_misreport_the_parent_as_start(self):
        """A held *child* lock during drive is not a parent ``start`` marker.

        ``drive`` sets the process-wide work item to the parent while it holds
        the child's lock across the spawned session. A signal in that window
        must release the lock but must not post the "start phase interrupted
        before the worktree was created" comment on the **parent** item — the
        child's worktree exists and the parent was never being started
        (SA-0MW7LMUUQ00701KJ).
        """
        store = _lock_store()
        lock = WorkItemLock(
            LOCK_CHILD,
            agent="implement",
            session_id="sess-drive",
            worklog_dir=store,
        )
        acquired = lock.acquire()
        assert acquired.acquired is True, acquired
        impl_mod._child_lock_global = lock
        impl_mod._work_item_id_global = LOCK_PARENT
        impl_mod._worktree_path_global = ""
        impl_mod._repo_root_global = ""

        comments: list[tuple[str, str]] = []
        with (
            patch.object(
                impl_mod.StatusLifecycle,
                "show",
                return_value={"workItem": {"status": "in-progress"}},
            ),
            patch.object(impl_mod.StatusLifecycle, "update_status"),
            patch.object(
                impl_mod,
                "wl_add_comment",
                side_effect=lambda wid, c: comments.append((wid, c)),
            ),
        ):
            impl_mod._handle_signal_cleanup(signal.SIGTERM)

        assert comments == [], (
            "a signal while the driver held the child's lock was reported as "
            f"an interrupted `start` on the parent: {comments}"
        )
        assert impl_mod._child_lock_global is None
        _assert_lock_free(LOCK_CHILD, store)
