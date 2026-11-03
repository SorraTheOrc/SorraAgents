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
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

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
