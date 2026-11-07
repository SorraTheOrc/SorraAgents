"""Tests for the session-level herdr boundary (SA-0MVPHXNTJ006TNFB).

``shared.herdr_session`` is the seam the pane-driven implement workflow builds
on: split a sibling pane, start an interactive agent in it, prompt it, wait for
its lifecycle state, then close or rename the pane. No live herdr is involved —
every test injects either the in-process recording runner or the executable
shim from :mod:`tests.fake_herdr_cli`, so the assertions are about the exact
herdr invocations the helper builds and about its fail-open behaviour.

Coverage map:

* :class:`TestAgentNames` — herdr's ``[a-z][a-z0-9_-]{0,31}`` rule, deterministic
  truncation and collision resolution.
* :class:`TestSplitPane` — ``pane split --current`` argv, parsed ``pane_id``, and
  the guarantee that no tab/workspace is created.
* :class:`TestStartAgent` — ``agent start`` argv, ``agent_not_ready`` retry
  within budget versus a surfaced reason.
* :class:`TestPromptAndWait` — ``agent prompt --wait`` / ``agent wait`` argv and
  the ``done`` / ``blocked`` / ``timeout`` / ``unknown`` transitions.
* :class:`TestCloseAndRename` — pane lifecycle argv.
* :class:`TestFailOpen` — every failure mode is a reported reason, never a raise.
* :class:`TestFakeCliShim` — end-to-end through a real subprocess: exact
  arguments *and order* of a split → start → prompt → wait → close sequence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILLS_ROOT = REPO_ROOT / "skill"
for _path in (str(_SKILLS_ROOT), str(Path(__file__).resolve().parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fake_herdr_cli as fake
from shared import herdr_session
from shared.herdr_session import HerdrSessionClient

HERDR_BIN = "/fake/bin/herdr"


def make_client(runner, **kwargs) -> HerdrSessionClient:
    """A client with herdr resolution pinned to a stand-in runner."""
    options = {
        "herdr_bin": HERDR_BIN,
        "env": {},
        "runner": runner,
        "timeout": 5.0,
    }
    options.update(kwargs)
    return HerdrSessionClient(**options)


# ---------------------------------------------------------------------------
# Agent naming
# ---------------------------------------------------------------------------


class TestAgentNames:
    """Names must always satisfy herdr's rule and stay deterministic."""

    def test_short_child_id_is_drive_prefixed_and_lowercased(self):
        assert herdr_session.agent_name_for_child("SA-0MVPHXNTJ006TNFB") == (
            "drive-sa-0mvphxntj006tnfb"
        )

    def test_name_is_valid_for_herdr(self):
        name = herdr_session.agent_name_for_child("SA-0MVPHXNTJ006TNFB")
        assert herdr_session.is_valid_agent_name(name)

    def test_unsafe_characters_are_collapsed(self):
        name = herdr_session.agent_name_for_child("SA 0MV.7/EU")
        assert name == "drive-sa-0mv-7-eu"
        assert herdr_session.is_valid_agent_name(name)

    def test_overlong_child_id_is_truncated_deterministically(self):
        child_id = "SA-0M" + "X" * 60
        first = herdr_session.agent_name_for_child(child_id)
        second = herdr_session.agent_name_for_child(child_id)
        assert first == second
        assert first.startswith("drive-sa-0mx")
        assert len(first) == herdr_session.AGENT_NAME_MAX_LENGTH
        assert herdr_session.is_valid_agent_name(first)

    def test_collision_gets_a_numeric_suffix_within_the_limit(self):
        child_id = "SA-0M" + "X" * 60
        base = herdr_session.agent_name_for_child(child_id)
        second = herdr_session.agent_name_for_child(child_id, existing=[base])
        third = herdr_session.agent_name_for_child(child_id, existing=[base, second])
        assert (second, third) == (base[:-2] + "-2", base[:-2] + "-3")
        assert len(second) == len(third) == herdr_session.AGENT_NAME_MAX_LENGTH
        assert all(
            herdr_session.is_valid_agent_name(name)
            for name in (base, second, third)
        )

    def test_distinct_child_ids_produce_distinct_names(self):
        names = {
            herdr_session.agent_name_for_child("SA-0MAAAAAAAAAAAAA"),
            herdr_session.agent_name_for_child("SA-0MBBBBBBBBBBBBB"),
            herdr_session.agent_name_for_child("SA-0MCCCCCCCCCCCCC"),
        }
        assert len(names) == 3

    def test_prefix_guarantees_a_leading_letter(self):
        name = herdr_session.agent_name_for_child("0-123")
        assert name.startswith("drive-")
        assert herdr_session.is_valid_agent_name(name)

    @pytest.mark.parametrize(
        "candidate",
        ["", "Drive-x", "1drive", "drive x", "drive-x" + "y" * 30, "-drive"],
    )
    def test_invalid_names_rejected(self, candidate):
        assert not herdr_session.is_valid_agent_name(candidate)

    def test_maximum_length_name_accepted(self):
        assert herdr_session.is_valid_agent_name("a" + "b" * 31)


# ---------------------------------------------------------------------------
# pane split
# ---------------------------------------------------------------------------


class TestSplitPane:
    def test_split_builds_current_pane_argv_with_cwd_env_and_no_focus(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.pane_info_response("w1:p9")}],
        )
        client = make_client(runner)

        result = client.split_pane(
            "/worktrees/wl-123", env={"IMPLEMENT_DRIVE_ACTIVE": "1"},
        )

        assert result["success"] is True
        assert result["pane_id"] == "w1:p9"
        assert runner.arguments() == [[
            "pane", "split",
            "--current",
            "--direction", "right",
            "--cwd", "/worktrees/wl-123",
            "--env", "IMPLEMENT_DRIVE_ACTIVE=1",
            "--no-focus",
        ]]

    def test_no_tab_or_workspace_is_created(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.pane_info_response("w1:p9")}],
        )
        make_client(runner).split_pane("/wt")

        assert [call[:2] for call in runner.arguments()] == [["pane", "split"]]
        assert not any(
            call[:2] in (["tab", "create"], ["workspace", "create"])
            for call in runner.arguments()
        )

    def test_focus_mode_passes_focus_flag(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.pane_info_response("w1:p9")}],
        )
        make_client(runner).split_pane("/wt", focus=True)

        assert runner.arguments()[0][-1] == "--focus"
        assert "--no-focus" not in runner.arguments()[0]

    def test_direction_can_be_omitted_and_pane_target_explicit(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.pane_info_response("w1:p9")}],
        )
        make_client(runner).split_pane(
            "/wt", direction=None, current=False, pane_id="w1:p1",
        )

        assert runner.arguments()[0] == [
            "pane", "split", "--pane", "w1:p1", "--cwd", "/wt", "--no-focus",
        ]

    def test_missing_pane_id_is_a_reported_failure(self):
        runner = fake.FakeHerdrRunner([{"stdout": {"result": {"type": "pane_info"}}}])

        result = make_client(runner).split_pane("/wt")

        assert result["success"] is False
        assert "result.pane.pane_id" in result["reason"]


# ---------------------------------------------------------------------------
# agent start
# ---------------------------------------------------------------------------


class TestStartAgent:
    def test_start_builds_agent_start_argv_and_parses_agent(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.agent_started_response("drive-sa-1", "w1:p9")}],
        )
        client = make_client(runner)

        result = client.start_agent(
            "drive-sa-1", "w1:p9", args=["--approve", "--no-extensions"],
        )

        assert result["success"] is True
        assert result["agent_status"] == "idle"
        assert result["pane_id"] == "w1:p9"
        assert runner.arguments() == [[
            "agent", "start", "drive-sa-1",
            "--kind", "pi",
            "--pane", "w1:p9",
            "--", "--approve", "--no-extensions",
        ]]

    def test_start_timeout_flag_is_forwarded(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.agent_started_response("drive-sa-1", "w1:p9")}],
        )
        make_client(runner).start_agent("drive-sa-1", "w1:p9", timeout_ms=45000)

        assert runner.arguments()[0][-2:] == ["--timeout", "45000"]

    def test_invalid_name_is_rejected_without_invoking_herdr(self):
        runner = fake.FakeHerdrRunner([])

        result = make_client(runner).start_agent("Drive SA", "w1:p9")

        assert result["success"] is False
        assert "invalid herdr agent name" in result["reason"]
        assert runner.calls == []

    def test_agent_not_ready_without_budget_is_surfaced(self):
        runner = fake.FakeHerdrRunner([{
            "stdout": fake.error_response("agent_not_ready", "shell busy"),
            "exit_code": 1,
        }])
        sleeps: list[float] = []

        result = make_client(runner, sleep=sleeps.append).start_agent(
            "drive-sa-1", "w1:p9",
        )

        assert result["success"] is False
        assert result["code"] == "agent_not_ready"
        assert result["attempts"] == 1
        assert sleeps == []
        assert len(runner.calls) == 1

    def test_agent_not_ready_is_retried_within_the_budget(self):
        runner = fake.FakeHerdrRunner([
            {"stdout": fake.error_response("agent_not_ready"), "exit_code": 1},
            {"stdout": fake.agent_started_response("drive-sa-1", "w1:p9")},
        ])
        sleeps: list[float] = []
        now = [0.0]

        def advance(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        result = make_client(
            runner, sleep=advance, clock=lambda: now[0],
        ).start_agent(
            "drive-sa-1", "w1:p9", ready_budget_seconds=10, poll_interval=0.5,
        )

        assert result["success"] is True
        assert result["attempts"] == 2
        assert sleeps == [0.5]

    def test_agent_not_ready_budget_exhaustion_stops_retrying(self):
        runner = fake.FakeHerdrRunner([
            {"stdout": fake.error_response("agent_not_ready"), "exit_code": 1},
            {"stdout": fake.error_response("agent_not_ready"), "exit_code": 1},
            {"stdout": fake.error_response("agent_not_ready"), "exit_code": 1},
        ])
        now = [0.0]

        def advance(seconds):
            now[0] += seconds

        result = make_client(
            runner, sleep=advance, clock=lambda: now[0],
        ).start_agent(
            "drive-sa-1", "w1:p9", ready_budget_seconds=1, poll_interval=0.5,
        )

        assert result["success"] is False
        assert result["code"] == "agent_not_ready"
        assert result["attempts"] == 3
        assert now[0] == pytest.approx(1.0)

    def test_non_readiness_error_is_not_retried(self):
        runner = fake.FakeHerdrRunner([{
            "stdout": fake.error_response("pane_not_found", "no such pane"),
            "exit_code": 1,
        }])

        result = make_client(runner).start_agent(
            "drive-sa-1", "w1:p9", ready_budget_seconds=10,
        )

        assert result["success"] is False
        assert result["code"] == "pane_not_found"
        assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# prompt / wait state transitions
# ---------------------------------------------------------------------------


class TestPromptAndWait:
    def test_prompt_builds_wait_argv_and_returns_state(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.agent_prompted_response("drive-sa-1", "w1:p9")}],
        )
        client = make_client(runner)

        result = client.prompt_agent(
            "drive-sa-1", "/skill:implement SA-1", timeout_ms=120000,
        )

        assert result["success"] is True
        assert result["agent_status"] == "done"
        assert runner.arguments() == [[
            "agent", "prompt", "drive-sa-1", "/skill:implement SA-1",
            "--wait", "--timeout", "120000",
        ]]

    def test_prompt_until_states_imply_wait(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.agent_prompted_response("drive-sa-1", "w1:p9",
                                                     status="blocked")}],
        )

        result = make_client(runner).prompt_agent(
            "drive-sa-1", "go", wait=False, until=["done", "blocked"],
            timeout_ms=5000,
        )

        assert result["agent_status"] == "blocked"
        assert runner.arguments()[0] == [
            "agent", "prompt", "drive-sa-1", "go",
            "--wait", "--until", "done", "--until", "blocked",
            "--timeout", "5000",
        ]

    @pytest.mark.parametrize("status", ["done", "blocked", "unknown"])
    def test_wait_reports_the_matched_state(self, status):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.agent_info_response("drive-sa-1", "w1:p9",
                                                status=status)}],
        )

        result = make_client(runner).wait_agent(
            "drive-sa-1", until=["done", "blocked"], timeout_ms=30000,
        )

        assert result["success"] is True
        assert result["agent_status"] == status
        assert runner.arguments() == [[
            "agent", "wait", "drive-sa-1",
            "--until", "done", "--until", "blocked", "--timeout", "30000",
        ]]

    def test_wait_timeout_is_a_reported_reason(self):
        runner = fake.FakeHerdrRunner([{
            "stdout": fake.error_response("timeout", "no matching state"),
            "exit_code": 1,
        }])

        result = make_client(runner).wait_agent(
            "drive-sa-1", until="done", timeout_ms=1000,
        )

        assert result["success"] is False
        assert result["code"] == "timeout"
        assert "timeout" in result["reason"]

    def test_prompt_on_blocked_agent_is_reported(self):
        runner = fake.FakeHerdrRunner([{
            "stdout": fake.error_response("agent_blocked", "approval pending"),
            "exit_code": 1,
        }])

        result = make_client(runner).prompt_agent("drive-sa-1", "go")

        assert result["success"] is False
        assert result["code"] == "agent_blocked"


# ---------------------------------------------------------------------------
# pane lifecycle
# ---------------------------------------------------------------------------


class TestCloseAndRename:
    def test_close_pane_argv(self):
        runner = fake.FakeHerdrRunner([{"stdout": fake.ok_response()}])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is True
        assert runner.arguments() == [["pane", "close", "w1:p9"]]

    def test_rename_pane_argv(self):
        runner = fake.FakeHerdrRunner(
            [{"stdout": fake.pane_info_response("w1:p9", label="New label")}],
        )

        result = make_client(runner).rename_pane("w1:p9", "New label")

        assert result["success"] is True
        assert result["label"] == "New label"
        assert runner.arguments() == [
            ["pane", "rename", "w1:p9", "New label"],
        ]


# ---------------------------------------------------------------------------
# fail-open behaviour
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Every failure mode is a reported reason; the client never raises."""

    def test_missing_herdr_binary_short_circuits(self):
        runner = fake.FakeHerdrRunner([])
        client = HerdrSessionClient(env={}, runner=runner, which=lambda _name: None)

        result = client.split_pane("/wt")

        assert result["success"] is False
        assert result["reason"] == "herdr CLI unavailable"
        assert runner.calls == []

    def test_herdr_bin_path_override_is_honoured(self):
        runner = fake.FakeHerdrRunner([{"stdout": fake.ok_response()}])
        client = HerdrSessionClient(
            env={"HERDR_BIN_PATH": "/custom/herdr"}, runner=runner,
        )

        assert client.resolve_bin() == "/custom/herdr"

    def test_runner_exception_is_swallowed(self):
        runner = fake.FakeHerdrRunner([{"raises": "oserror"}])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is False
        assert "herdr call failed" in result["reason"]

    def test_runner_timeout_is_swallowed(self):
        runner = fake.FakeHerdrRunner([{"raises": "timeout"}])

        result = make_client(runner).wait_agent("drive-sa-1")

        assert result["success"] is False
        assert result["reason"] == "herdr call timed out"

    def test_nonzero_exit_without_error_payload_is_reported(self):
        runner = fake.FakeHerdrRunner([{"stdout": "", "exit_code": 2,
                                       "stderr": "boom"}])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is False
        assert "exited with code 2" in result["reason"]

    def test_malformed_json_is_reported(self):
        runner = fake.FakeHerdrRunner([{"stdout": "not json"}])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is False
        assert result["reason"] == "malformed herdr output"

    def test_missing_result_envelope_is_reported(self):
        runner = fake.FakeHerdrRunner([{"stdout": {"id": "cli:pane:close"}}])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is False
        assert "missing result" in result["reason"]

    def test_unexpected_agent_shape_is_reported(self):
        runner = fake.FakeHerdrRunner([{"stdout": {"result": {"type": "ok"}}}])

        result = make_client(runner).start_agent("drive-sa-1", "w1:p9")

        assert result["success"] is False
        assert "result.agent" in result["reason"]

    def test_unscripted_call_fails_loudly(self):
        runner = fake.FakeHerdrRunner([])

        result = make_client(runner).close_pane("w1:p9")

        assert result["success"] is False
        assert "exited with code 127" in result["reason"]


# ---------------------------------------------------------------------------
# The executable shim: exact invocations across a real subprocess boundary
# ---------------------------------------------------------------------------


class TestFakeCliShim:
    """The shim records the exact argv of a full pane-session sequence."""

    def test_drive_sequence_records_exact_arguments_and_order(self, tmp_path):
        shim = fake.ensure_executable()
        script_file = fake.write_script(
            str(tmp_path / "script.json"),
            {
                "responses": [
                    {"stdout": fake.pane_info_response("w1:p9")},
                    {"stdout": fake.agent_started_response("drive-sa-1", "w1:p9")},
                    {"stdout": fake.agent_prompted_response("drive-sa-1", "w1:p9")},
                    {"stdout": fake.agent_info_response("drive-sa-1", "w1:p9",
                                                        status="done")},
                    {"stdout": fake.ok_response()},
                ],
            },
        )
        record_file = str(tmp_path / "calls.jsonl")
        env = dict(os.environ)
        env[fake.SCRIPT_ENV] = script_file
        env[fake.RECORD_ENV] = record_file
        client = HerdrSessionClient(herdr_bin=shim, env=env, timeout=30)

        assert client.split_pane("/wt/child", focus=False)["pane_id"] == "w1:p9"
        started = client.start_agent(
            "drive-sa-1", "w1:p9", args=["--approve", "--no-extensions"],
        )
        assert started["success"] is True and started["agent_status"] == "idle"
        prompted = client.prompt_agent(
            "drive-sa-1", "/skill:implement SA-1", timeout_ms=120000,
        )
        assert prompted["agent_status"] == "done"
        waited = client.wait_agent("drive-sa-1", until=["done", "blocked"])
        assert waited["agent_status"] == "done"
        assert client.close_pane("w1:p9")["success"] is True

        assert fake.read_record(record_file) == [
            ["pane", "split", "--current", "--direction", "right",
             "--cwd", "/wt/child", "--no-focus"],
            ["agent", "start", "drive-sa-1", "--kind", "pi",
             "--pane", "w1:p9", "--", "--approve", "--no-extensions"],
            ["agent", "prompt", "drive-sa-1", "/skill:implement SA-1",
             "--wait", "--timeout", "120000"],
            ["agent", "wait", "drive-sa-1", "--until", "done",
             "--until", "blocked"],
            ["pane", "close", "w1:p9"],
        ]

    def test_shim_surfaces_error_payload_and_exit_code(self, tmp_path):
        shim = fake.ensure_executable()
        script_file = fake.write_script(
            str(tmp_path / "script.json"),
            {"responses": [{
                "stdout": fake.error_response("agent_not_ready", "shell busy"),
                "exit_code": 1,
            }]},
        )
        record_file = str(tmp_path / "calls.jsonl")
        env = dict(os.environ)
        env[fake.SCRIPT_ENV] = script_file
        env[fake.RECORD_ENV] = record_file
        client = HerdrSessionClient(herdr_bin=shim, env=env, timeout=30)

        result = client.start_agent("drive-sa-1", "w1:p9")

        assert result["success"] is False
        assert result["code"] == "agent_not_ready"
        assert result["returncode"] == 1
        assert fake.read_record(record_file) == [[
            "agent", "start", "drive-sa-1", "--kind", "pi", "--pane", "w1:p9",
        ]]

    def test_shim_without_script_reports_unscripted_call(self, tmp_path):
        shim = fake.ensure_executable()
        record_file = str(tmp_path / "calls.jsonl")
        env = dict(os.environ)
        env[fake.RECORD_ENV] = record_file
        env.pop(fake.SCRIPT_ENV, None)
        client = HerdrSessionClient(herdr_bin=shim, env=env, timeout=30)

        result = client.close_pane("w1:p9")

        assert result["success"] is False
        assert "exited with code 127" in result["reason"]
        assert fake.read_record(record_file) == [["pane", "close", "w1:p9"]]

    def test_script_file_flags_override_environment(self, tmp_path):
        shim = fake.ensure_executable()
        script_file = fake.write_script(
            str(tmp_path / "script.json"),
            {"responses": [{"stdout": fake.ok_response()}]},
        )
        record_file = str(tmp_path / "calls.jsonl")
        env = dict(os.environ)
        env[fake.SCRIPT_ENV] = str(tmp_path / "missing.json")
        env[fake.RECORD_ENV] = record_file

        completed = subprocess.run(
            [shim, "--script", script_file, "--record", record_file,
             "pane", "close", "w1:p9"],
            capture_output=True, text=True, env=env, timeout=30, check=False,
        )

        assert completed.returncode == 0
        assert json.loads(completed.stdout)["result"]["type"] == "ok"
        assert fake.read_record(record_file)[0] == ["pane", "close", "w1:p9"]
