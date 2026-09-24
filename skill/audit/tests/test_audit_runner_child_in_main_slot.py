"""Tests for the config-gated in-main-slot child-audit mode (SA-0MT2XRGEU0009QRE).

The audit runner selects between two child-audit execution modes via a
config gate — ``AUDIT_CHILD_IN_MAIN_SLOT`` env var / ``--child-in-main-slot``
CLI flag (flag wins), default ``true``:

- **In-main-slot mode (default True):** child Phase-1 AC-review screens and
  Phase-2 child deep analysis run in the main LLM slot — NO new ``pi``
  subprocess session is spawned per child. The runner emits structured
  ``[AUDIT_IN_MAIN_SLOT_WORK]`` work items for the invoking agent session
  and a ``[AUDIT_IN_MAIN_SLOT_COMPACT] /compact`` instruction after each
  child audit before continuing. Child verdicts stay pending (main-session
  review) until the child audit is persisted; a re-run reuses them.
- **Separate-process mode (gate False):** the unchanged historical path — a
  ``pi`` subprocess session per child (Phase 1 screens and Phase 2 deep).

The pre-existing audit test suite pins ``AUDIT_CHILD_IN_MAIN_SLOT=false``
in conftest (those tests assert the separate-process behavior). This file
covers in-main-slot mode explicitly plus the gate-resolution matrix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.test_audit_runner_phase1 import (
    _make_phase1_runner,
    _phase1_child,
)


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests."""
    import contextlib

    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


class TestChildInMainSlotGateResolution:
    """AC1/AC4: the config-gate resolution matrix (default / env / flag)."""

    def _clear_env(self):
        return mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV: ""},
            clear=False,
        )

    def test_default_true_when_unset(self):
        """Default is true (in-main-slot mode) with no env var and no flag."""
        with self._clear_env(), mock.patch.dict(
            audit_runner.os.environ, {}, clear=True
        ):
            assert audit_runner._resolve_child_in_main_slot(None) is True

    def test_env_truthy_values_enable(self):
        """Truthy env values (1/true/yes/on) enable in-main-slot mode."""
        for value in ("1", "true", "yes", "on", "TRUE", "True"):
            with mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV: value},
                clear=False,
            ):
                assert audit_runner._resolve_child_in_main_slot(None) is True

    def test_env_falsy_values_disable(self):
        """Falsy env values disable in-main-slot (separate-process path)."""
        for value in ("0", "false", "no", "off", "FALSE", "False"):
            with mock.patch.dict(
                audit_runner.os.environ,
                {audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV: value},
                clear=False,
            ):
                assert audit_runner._resolve_child_in_main_slot(None) is False

    def test_flag_wins_over_env(self):
        """The CLI flag wins over the env var in both directions."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV: "false"},
            clear=False,
        ):
            # Explicit True flag overrides a false env var.
            assert audit_runner._resolve_child_in_main_slot(True) is True
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV: "true"},
            clear=False,
        ):
            # Explicit False flag overrides a true env var.
            assert audit_runner._resolve_child_in_main_slot(False) is False

    def test_constant_name(self):
        """The env-var constant is stable for documentation/ops."""
        assert audit_runner.AUDIT_CHILD_IN_MAIN_SLOT_ENV == "AUDIT_CHILD_IN_MAIN_SLOT"

    def test_cli_flag_parsed(self):
        """build_parser exposes --child-in-main-slot / --no-child-in-main-slot."""
        parser = audit_runner.build_parser()
        args_true = parser.parse_args(["issue", "TEST-1", "--child-in-main-slot"])
        assert args_true.child_in_main_slot is True
        args_false = parser.parse_args(["issue", "TEST-1", "--no-child-in-main-slot"])
        assert args_false.child_in_main_slot is False
        args_default = parser.parse_args(["issue", "TEST-1"])
        assert args_default.child_in_main_slot is None  # → env / default true


class TestPhase1ChildScreenInMainSlot:
    """AC2: Phase 1 child AC screens run in the main slot (no pi subprocess)."""

    def _child(self):
        return _phase1_child(1)

    def test_in_main_slot_emits_work_and_compact_no_pi(self, capsys):
        """In-main-slot: the screen prompt is emitted as a work item + /compact
        and _call_phase1_screen is NOT invoked (no pi subprocess session)."""
        child = self._child()
        mock_runner, _ = _make_phase1_runner([child])

        with mock.patch.object(
            audit_runner, "_call_phase1_screen"
        ) as mock_screen:
            ci, acs = audit_runner._phase1_review_child_acs(
                0, child,
                phase1_model="test-model",
                full_model="test-model",
                pi_bin="pi",
                debug_log=None,
                timeout=None,
                runner=mock_runner,
                script_failure_callback=lambda ctx, exc: None,
                child_in_main_slot=True,
            )

        assert ci == 0
        mock_screen.assert_not_called()  # no pi subprocess per child
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err
        assert "phase1_child_screen" in err
        assert "CHILD-1" in err
        assert audit_runner.IN_MAIN_SLOT_COMPACT_MARKER in err
        assert "/compact after child audit CHILD-1" in err
        # Verdicts are pending main-session review (partial), not fabricated met.
        assert all(a["verdict"] == "partial" for a in acs)
        assert all("main" in a["evidence"] for a in acs)

    def test_separate_process_still_calls_pi(self, capsys):
        """Gate=false: the separate-process path is unchanged (screen calls pi)."""
        child = self._child()
        mock_runner, _ = _make_phase1_runner([child])

        def _fake_screen(issue_id, context, prompt, model, pi_bin, debug_log,
                         timeout, ac_fallback_used, on_runtime_error,
                         failure_label, **kwargs):
            return (
                {"verdict": "met", "evidence": "mock"},
                [{"index": 0, "verdict": "met", "evidence": "m"}],
                "",
            )

        # _call_phase1_screen is invoked in the separate-process path.
        with mock.patch.object(
            audit_runner, "_call_phase1_screen", side_effect=_fake_screen
        ) as mock_screen:
            ci, acs = audit_runner._phase1_review_child_acs(
                0, child,
                phase1_model="test-model",
                full_model="test-model",
                pi_bin="pi",
                debug_log=None,
                timeout=None,
                runner=mock_runner,
                script_failure_callback=lambda ctx, exc: None,
                child_in_main_slot=False,
            )

        assert ci == 0
        mock_screen.assert_called_once()
        # verdict parsed from the batched screen result (met), not pending
        assert acs[0]["verdict"] == "met"
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in err
        assert audit_runner.IN_MAIN_SLOT_COMPACT_MARKER not in err


class TestPhase2ChildDeepInMainSlot:
    """AC2: Phase 2 child deep analysis runs in the main slot (no pi)."""

    def _child(self):
        return {
            "id": "CHILD-1",
            "title": "Child CHILD-1",
            "status": "in_progress",
            "stage": "in_progress",
            "ac_results": [
                {"index": 0, "text": "CAC1", "verdict": "partial",
                 "evidence": "pending (main-session review)"},
            ],
        }

    def test_in_main_slot_emits_work_and_compact_no_pi(self, capsys):
        """In-main-slot: deep prompt emitted as a work item + /compact and
        _call_pi_and_maybe_log is NOT invoked."""
        child = self._child()
        mock_runner = mock.MagicMock()

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_log:
            ci, updated, timed_out = audit_runner._deep_analyze_child(
                0, child, "test-model", "pi", None, None, mock_runner,
                child_in_main_slot=True,
            )

        assert ci == 0
        assert timed_out is False
        mock_log.assert_not_called()  # no pi subprocess session per child
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err
        assert "phase2_child_deep" in err
        assert "CHILD-1" in err
        assert audit_runner.IN_MAIN_SLOT_COMPACT_MARKER in err
        # Phase 1 ac_results preserved (pending main-session review).
        assert updated["ac_results"][0]["verdict"] == "partial"

    def test_separate_process_still_calls_pi(self, capsys):
        """Gate=false: the separate-process deep path is unchanged."""
        child = self._child()
        mock_runner = mock.MagicMock()

        result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"}
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=result
        ) as mock_log:
            ci, updated, timed_out = audit_runner._deep_analyze_child(
                0, child, "test-model", "pi", None, None, mock_runner,
                child_in_main_slot=False,
            )

        assert ci == 0
        assert timed_out is False
        mock_log.assert_called_once()
        assert updated["ac_results"][0]["verdict"] == "met"
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in err


class TestCmdIssueInMainSlotEndToEnd:
    """AC2/AC3: cmd_issue routes child audits per the gate."""

    def test_in_main_slot_no_child_pi_subprocess(self, capsys):
        """A parent with an unaudited child in in-main-slot mode spawns NO
        child pi session: no phase1_child/phase2_child call context, the work
        item + /compact markers are emitted, and the child is pending."""
        context_log: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            context_log.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent.py:1"},
            ])}

        parent_desc = "## Acceptance Criteria\n1. AC one"
        child = _phase1_child(1)
        mock_runner, _ = _make_phase1_runner([child], parent_desc=parent_desc)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [],
                          "fixes_applied": 0},
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,
                child_in_main_slot=True,
            )

        assert rc == 0
        child_contexts = [
            c for c in context_log
            if c.startswith(("child:", "phase2_child"))
        ]
        assert child_contexts == [], (
            f"in-main-slot mode must not spawn child pi sessions: {child_contexts}"
        )
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err
        assert "phase1_child_screen" in err
        assert "CHILD-1" in err
        assert audit_runner.IN_MAIN_SLOT_COMPACT_MARKER in err

    def test_separate_process_child_pi_subprocess(self, capsys):
        """Gate=false: cmd_issue keeps the separate-process path (child pi
        context fires for an unaudited child)."""
        context_log: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            context_log.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent.py:1"},
            ])}

        parent_desc = "## Acceptance Criteria\n1. AC one"
        child = _phase1_child(1)
        mock_runner, _ = _make_phase1_runner([child], parent_desc=parent_desc)

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [],
                          "fixes_applied": 0},
        ), mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=_passthrough_phase2,
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,
                child_in_main_slot=False,
            )

        assert rc == 0
        assert any(
            c.startswith("child:") for c in context_log
        ), f"separate-process path must spawn child screens: {context_log}"
        err = capsys.readouterr().err
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in err