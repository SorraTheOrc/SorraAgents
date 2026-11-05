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
from audit.scripts.checkpoint_store import (
    PHASE_CHILDREN,
    PHASE_PARENT,
    PHASE_PHASE2,
    CheckpointStore,
)
from audit.tests.test_audit_runner_checkpoint import (
    HEAD_BASE,
    _make_ctx,
)
from audit.tests.test_audit_runner_phase1 import (
    _make_phase1_runner,
    _phase1_child,
)
from audit.tests.wl_helpers import stateful_wl_side_effect


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
        captured = capsys.readouterr()
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in captured.err
        assert "phase1_child_screen" in captured.err
        assert "CHILD-1" in captured.err
        assert audit_runner.IN_MAIN_SLOT_COMPACT_MARKER in captured.err
        # The report must surface the explicit gate, never a silent partial
        # (SA-0MU32TE120012T49 AC2).
        assert "Ready to close: No" in captured.out
        assert "Child screens pending: 1 (main-slot) — CHILD-1" in captured.out

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
        captured = capsys.readouterr()
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in captured.err
        assert "Child screens pending" not in captured.out


class TestChildScreensPendingGate:
    """AC2/AC3: in-main-slot pending screens are explicit and block closure."""

    def _pending_child(self):
        return {
            "id": "CHILD-1",
            "title": "Child CHILD-1",
            "status": "in_progress",
            "stage": "in_review",
            "ac_results": [
                {"text": "CAC1", "verdict": "partial",
                 "evidence": "In-main-slot child Phase-1 AC screen emitted to "
                             "the main LLM slot; verdict pending main-session "
                             "review."},
            ],
        }

    def _parent_acs(self):
        return [{"text": "AC one", "verdict": "met", "evidence": "x.py:1"}]

    def test_report_surfaces_pending_gate_and_blocks_yes(self):
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, self._parent_acs(), [self._pending_child()],
        )
        assert "Ready to close: No" in report
        assert "Child screens pending: 1 (main-slot) — CHILD-1" in report
        assert "Child-audit execution gate:" in report

    def test_json_payload_includes_pending_screens(self):
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, self._parent_acs(), [self._pending_child()],
        )
        assert payload["ready_to_close"] is False
        assert payload["pending_child_screens"] == ["CHILD-1"]

    def test_pending_signal_written_and_cleared(self, tmp_path):
        audit_runner._write_pending_screens_signal(
            ["CHILD-1", "CHILD-2"], "TEST-1", tmp_path, str(tmp_path),
        )
        signal = tmp_path / "TEST-1.pending-screens.json"
        assert signal.exists()
        data = json.loads(signal.read_text(encoding="utf-8"))
        assert data["pending_child_screens"] == ["CHILD-1", "CHILD-2"]

        audit_runner._write_pending_screens_signal(
            [], "TEST-1", tmp_path, str(tmp_path),
        )
        assert not signal.exists()

    def test_no_pending_child_produces_no_gate(self):
        child = self._pending_child()
        child["ac_results"][0] = {
            "text": "CAC1", "verdict": "met", "evidence": "x.py:1 — ok",
        }
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, self._parent_acs(), [child],
        )
        assert "Child screens pending" not in report
        assert "Ready to close: Yes" in report


#: Evidence text the in-main-slot Phase-1 screen call leaves on a child whose
#: ``[AUDIT_IN_MAIN_SLOT_WORK]`` screens were emitted but never serviced
#: (partial + ``MAIN_SLOT_PENDING_EVIDENCE`` — the single detection key of
#: ``_pending_main_slot_child_ids``).
_MAIN_SLOT_PENDING_SCREEN_EVIDENCE = (
    "In-main-slot child Phase-1 AC screen emitted to the main LLM slot; "
    "verdict pending main-session review."
)


def _child_in_main_slot(**overrides):
    """Fixture child carrying the unserviced in-main-slot pending marker
    (partial AC verdict + main-session-review evidence)."""
    child = {
        "id": "CHILD-1",
        "title": "Child CHILD-1",
        "status": "in_progress",
        "stage": "in_review",
        "ac_results": [
            {"text": "CAC1", "verdict": "partial",
             "evidence": _MAIN_SLOT_PENDING_SCREEN_EVIDENCE},
        ],
    }
    child.update(overrides)
    return child


def unserviced_child_with_stored_verdict():
    """An unserviced in-main-slot child that HAS a previously stored verdict.

    The child's ``ac_results`` still carry the in-main-slot pending marker
    (an earlier session serviced the screens and persisted the child audit
    without rewriting the work item's ``ac_results``), but a stored verdict
    exists (``reused_from`` + ``child_audit_ready`` — the same markers the
    child-verdict reuse path, LP-0MSQ32MF200675AR, records).
    """
    return _child_in_main_slot(
        child_audit_ready=True,
        reused_from="2026-10-01T00:00:00.000Z",
    )


def unserviced_child_no_verdict():
    """An unserviced in-main-slot child with NO stored verdict.

    The ``[AUDIT_IN_MAIN_SLOT_WORK]`` screens were emitted by an earlier
    session and never serviced (no persisted child audit, no ``reused_from``
    marker) — the child is reportable only as NOT-ASSESSED.
    """
    return _child_in_main_slot()


def genuinely_not_ready_child():
    """A genuinely NOT-READY child: a blocking verdict whose evidence carries
    NO in-main-slot pending marker (distinct from an unserviced child)."""
    return {
        "id": "CHILD-1",
        "title": "Child CHILD-1",
        "status": "in_progress",
        "stage": "plan_complete",
        "ac_results": [
            {"text": "CAC1", "verdict": "unmet",
             "evidence": "child.py:4 — blocking"},
        ],
        "child_audit_ready": False,
        "child_audit_not_ready": True,
    }


def serviced_child():
    """An in-main-slot child whose screens WERE serviced by the invoking
    session (the interactive case).

    The agent performed the emitted ``[AUDIT_IN_MAIN_SLOT_WORK]`` screen and
    persisted the child audit, so the child now carries a FRESH stored audit
    (``reused_from`` + ``child_audit_ready``, the LP-0MSQ32MF200675AR reuse
    markers) with met AC verdicts and NO in-main-slot pending marker. Such a
    child must never be classified unserviced/not-assessed: the ready path is
    unchanged and carries no assessment diagnostic
    (SA-0MVAWFQ4A002XCAO AC4).
    """
    return {
        "id": "CHILD-1",
        "title": "Child CHILD-1",
        "status": "completed",
        "stage": "in_review",
        "ac_results": [
            {"text": "CAC1", "verdict": "met", "evidence": "child.py:1"},
        ],
        "child_audit_ready": True,
        "reused_from": "2026-10-01T00:00:00.000Z",
    }


#: Parent ACs (all met) shared by the unserviced-verdict fixtures so the
#: report/JSON assessment depends only on the child under test.
_UNSERVICED_PARENT_ACS = [
    {"text": "AC one", "verdict": "met", "evidence": "parent.py:1"},
]


class TestUnservicedMainSlotVerdicts:
    """TDD fixture suite for the parent's handling of UN-SERVICED in-main-slot
    child verdicts (parent AC2, SA-0MUH6H87Z001HYJ7).

    An *unserviced* child is one whose ``ac_results`` still carry the
    in-main-slot pending evidence (``partial`` + main-session-review marker)
    because an EARLIER invoking session emitted its
    ``[AUDIT_IN_MAIN_SLOT_WORK]`` screens and never serviced them. Such
    children are identified by the persisted ``<issue_id>.pending-screens.json``
    signal written by the PREVIOUS run (the *stale* pending ids) — the parent
    must not re-block its readiness-to-close decision on them:

    - with a previously stored verdict → the parent INHERITS the stored
      verdict rather than blocking (the ``stale_pending_ids``-driven
      subtraction, SA-0MVAWFUU0004VW45);
    - without a stored verdict → the child is NOT-ASSESSED (does not block
      via the pending gate and is never reported ready);
    - a genuinely not-ready child (blocking verdict, no pending marker)
      still blocks — covered GREEN in
      ``test_audit_runner_children.py::TestUnservicedVerdictGuardAndAgreement``;
    - freshly emitted screens in the CURRENT run (no stale signal) still
      force ``Ready to close: No``.

    Asserts on ``_build_issue_json`` / ``_assemble_issue_report`` directly
    with fixture child results (this item's Minimal Implementation).
    """

    # ------------------------------------------------------------------
    # AC2 — unserviced child WITH a previously stored verdict is inherited
    # ------------------------------------------------------------------

    def test_inherits_stored_ready_verdict_rather_than_blocking(self):
        """A child whose ac_results stay ``partial`` (unserviced screens) but
        which has a previously STORED ready verdict is inherited: the parent
        does not re-block on the pending gate — the JSON payload reports
        ``ready_to_close: True`` with no pending screens and the markdown
        report says ``Ready to close: Yes`` with no gate line."""
        stale_pending_ids = {"CHILD-1"}
        child = unserviced_child_with_stored_verdict()

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert payload["ready_to_close"] is True
        assert payload["pending_child_screens"] == []

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert "Ready to close: Yes" in report
        assert "Child screens pending" not in report

    def test_not_assessed_without_stored_verdict_no_gate_never_ready(self):
        """An unserviced child with NO stored verdict is NOT-ASSESSED: it
        does not trigger the pending-screens gate (no ``Child screens
        pending`` line, not listed in ``pending_child_screens``) and is
        never reported ready (``ready_to_close: False`` / ``Ready to close:
        No``)."""
        stale_pending_ids = {"CHILD-1"}
        child = unserviced_child_no_verdict()

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert payload["ready_to_close"] is False  # never reported ready
        assert "CHILD-1" not in payload["pending_child_screens"]  # no gate

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale_pending_ids,
        )
        assert "Ready to close: No" in report
        assert "Child screens pending" not in report

    # ------------------------------------------------------------------
    # Controls that are GREEN against the current runner
    # ------------------------------------------------------------------

    def test_fresh_pending_without_stale_signal_still_gate_blocks(self):
        """A child whose in-main-slot screens were freshly emitted in the
        CURRENT run (no stale pending-screens signal — ``stale_pending_ids``
        absent) still forces ``Ready to close: No`` and the child-audit
        execution gate (SA-0MU32TE120012T49)."""
        child = _child_in_main_slot()

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert payload["ready_to_close"] is False
        assert payload["pending_child_screens"] == ["CHILD-1"]

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert "Ready to close: No" in report
        assert "Child screens pending: 1 (main-slot) — CHILD-1" in report
        # The child-audit execution gate (SA-0MU32TE120012T49) is unchanged:
        # a fresh pending screen is never presented as a normal result.
        assert "Child-audit execution gate:" in report
        assert "CHILD-1" in report

    def test_reused_stored_verdict_child_already_inherited_today(self):
        """GREEN baseline: a child whose ac_results carry the STORED verdict
        (child-verdict reuse, LP-0MSQ32MF200675AR — ``reused_from`` + met
        acs, in_review stage) is already treated as inherited today: the
        parent reports ready and never emits a gate line. The unserviced
        xfail fixtures above extend THIS contract to children whose
        ac_results still carry the stale pending marker."""
        child = unserviced_child_with_stored_verdict()
        child["ac_results"] = [
            {"text": "CAC1", "verdict": "met", "evidence": "child.py:1"},
        ]

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert payload["ready_to_close"] is True

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert "Ready to close: Yes" in report
        assert "Child screens pending" not in report


class TestPendingScreensSignalRead:
    """The stale pending-screens signal is the unserviced-child detection
    key (SA-0MVAWFUU0004VW45 AC2).

    ``_read_pending_screens_signal`` must read the ``<issue_id>.pending-
    screens.json`` file written by a previous run, and degrade to an empty
    set when the file is absent or unreadable (fail-open).
    """

    def test_reads_persisted_signal(self, tmp_path):
        audit_runner._write_pending_screens_signal(
            ["CHILD-1", "CHILD-2"], "TEST-1", tmp_path, str(tmp_path),
        )
        signal = tmp_path / "TEST-1.pending-screens.json"
        assert signal.is_file()
        assert audit_runner._read_pending_screens_signal(
            "TEST-1", tmp_path, str(tmp_path),
        ) == {"CHILD-1", "CHILD-2"}

    def test_missing_signal_reads_empty(self, tmp_path):
        assert audit_runner._read_pending_screens_signal(
            "TEST-1", tmp_path, str(tmp_path),
        ) == set()

    def test_corrupt_signal_reads_empty(self, tmp_path):
        (tmp_path / "TEST-1.pending-screens.json").write_text(
            "{not valid json", encoding="utf-8",
        )
        assert audit_runner._read_pending_screens_signal(
            "TEST-1", tmp_path, str(tmp_path),
        ) == set()

    def test_signal_round_trip_drives_inheritance(self, tmp_path):
        """End-to-end detection: a persisted stale signal plus the child's
        unserviced pending marker drives the inheritance subtraction through
        ``_build_issue_json`` — no explicit set is hand-passed by the test."""
        child = unserviced_child_with_stored_verdict()
        audit_runner._write_pending_screens_signal(
            ["CHILD-1"], "TEST-1", tmp_path, str(tmp_path),
        )
        stale = audit_runner._read_pending_screens_signal(
            "TEST-1", tmp_path, str(tmp_path),
        )
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale,
        )
        assert payload["ready_to_close"] is True
        assert payload["pending_child_screens"] == []

    def test_stale_signal_for_other_child_does_not_unservice_fresh_child(
        self, tmp_path,
    ):
        """A stale signal naming a DIFFERENT child must not suppress the
        current run's freshly emitted pending screen for this child: the
        fresh child still gates."""
        child = _child_in_main_slot()  # fresh pending marker, id CHILD-1
        audit_runner._write_pending_screens_signal(
            ["CHILD-OTHER"], "TEST-1", tmp_path, str(tmp_path),
        )
        stale = audit_runner._read_pending_screens_signal(
            "TEST-1", tmp_path, str(tmp_path),
        )
        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale,
        )
        assert payload["ready_to_close"] is False
        assert payload["pending_child_screens"] == ["CHILD-1"]


class TestInteractiveRegressionGuard:
    """AC4 regression guard (SA-0MVAWFQ4A002XCAO): the unserviced-verdict
    handling and child-assessment diagnostics introduced by the sibling
    items (SA-0MVAWFUU0004VW45 / SA-0MVAWFYZE0040SQ3) do not change the
    behaviour of INTERACTIVE in-main-slot audit runs — those in which the
    invoking agent session CAN service the emitted
    ``[AUDIT_IN_MAIN_SLOT_WORK]`` screens.

    The unserviced handling keys on the *stale* pending-screens signal (a
    signal naming a child whose ``ac_results`` STILL carry the in-main-slot
    pending marker). An interactive run either:

    - freshly emits the screens (no stale signal) — the child-audit
      execution gate (SA-0MU32TE120012T49) still blocks closure; or
    - has its screens serviced — the child carries a fresh stored audit with
      met verdicts and NO pending marker, so the ready path is unchanged and
      carries no assessment diagnostic.
    """

    #: A stored child audit that ``wl audit-show`` returns once the agent has
    #: serviced the emitted screen (ready verdict + parseable AC table).
    _STORED_READY_CHILD_AUDIT = (
        "Ready to close: Yes\n\n"
        "Audit report for work item CHILD-1\n\n"
        "## Acceptance Criteria Status\n\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | CAC1: child criterion 1 | met | child.py:1 |"
    )

    def _run_interactive(self, capsys, *, stored_child_audit=None,
                         force=False, checkpoint_dir=None,
                         parent_audit_raw=None, json_mode=False):
        """Run ``cmd_issue`` in in-main-slot mode like an interactive
        session, returning ``(rc, stdout, stderr, child_pi_contexts)``.

        *stored_child_audit*, when given, is served by ``wl audit-show`` for
        CHILD-1 (the serviced case); otherwise the child has no stored audit
        and is freshly screened in the main slot (the pending case).
        *force* exercises the ``--force`` bypass of child-verdict reuse.
        *checkpoint_dir* enables phase checkpointing (``--checkpoint-dir``)
        so the composed resume cycle can be exercised end to end, and
        *parent_audit_raw* seeds the stored parent report the freshness gate
        sees (a v2-fingerprinted, fresh report leaves the pipeline's
        pending-screens bypass as the only reason to re-run). *json_mode*
        makes the run emit the structured JSON payload instead of the
        markdown report.
        """
        context_log: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            context_log.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent.py:1"},
            ])}

        child = _phase1_child(1, stage="in_review")
        child_audit_raw = (
            {"CHILD-1": stored_child_audit} if stored_child_audit else None
        )
        runner, _ = _e2e_runner(
            [child], child_audit_raw=child_audit_raw,
            parent_audit_raw=parent_audit_raw,
        )
        extra: dict = {}
        if checkpoint_dir is not None:
            extra["checkpoint_dir"] = str(checkpoint_dir)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [],
                          "fixes_applied": 0},
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=force, runner=runner,
                audit_children=True, child_in_main_slot=True,
                json_mode=json_mode, **extra,
            )

        captured = capsys.readouterr()
        child_ctx = [
            c for c in context_log
            if c.startswith(("child:", "phase2_child"))
        ]
        return rc, captured.out, captured.err, child_ctx

    # ------------------------------------------------------------------
    # Serviced run — no assessment diagnostic on the ready path (AC4)
    # ------------------------------------------------------------------

    def test_serviced_child_ready_path_has_no_assessment_diagnostic(self):
        """A serviced in-main-slot child (fresh stored audit, met verdicts,
        no pending marker) leaves the ready path unchanged: the parent is
        ready and neither the report nor the JSON payload emits a
        child-assessment diagnostic or a pending-screens gate."""
        child = serviced_child()

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert payload["ready_to_close"] is True
        assert payload["pending_child_screens"] == []
        assert payload["child_reasons"] == {}
        assert payload["not_assessed_children"] == []

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
        )
        assert "Ready to close: Yes" in report
        assert "Child screens pending" not in report
        assert "Child assessment reasons:" not in report

    def test_serviced_child_lingering_stale_signal_still_clean(self):
        """A serviced child named by a LINGERING stale signal is still not
        misclassified: its ac_results carry no pending marker, so it is
        never an unserviced child — the ready path and the diagnostics are
        unchanged."""
        child = serviced_child()
        stale = {"CHILD-1"}

        payload = audit_runner._build_issue_json(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale,
        )
        assert payload["ready_to_close"] is True
        assert payload["pending_child_screens"] == []
        assert payload["child_reasons"] == {}
        assert payload["not_assessed_children"] == []

        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, _UNSERVICED_PARENT_ACS, [child],
            stale_pending_ids=stale,
        )
        assert "Ready to close: Yes" in report
        assert "Child screens pending" not in report
        assert "Child assessment reasons:" not in report

    # ------------------------------------------------------------------
    # Interactive lifecycle, end-to-end through cmd_issue (AC4)
    # ------------------------------------------------------------------

    def test_interactive_service_then_rerun_reports_ready(self, capsys):
        """The full interactive lifecycle is unchanged: the first run emits
        the pending screens and blocks with the execution gate; once the
        agent services them (the child gains a fresh stored audit), the
        re-run reuses the child verdict, reports ready, and emits no
        assessment diagnostic and no fresh screen."""
        rc1, out1, err1, ctx1 = self._run_interactive(capsys)
        assert rc1 == 0
        assert ctx1 == []  # in-main-slot: no child pi subprocess
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err1
        assert "Ready to close: No" in out1
        assert "Child screens pending: 1 (main-slot) — CHILD-1" in out1
        assert "Child-audit execution gate:" in out1

        rc2, out2, err2, ctx2 = self._run_interactive(
            capsys, stored_child_audit=self._STORED_READY_CHILD_AUDIT,
        )
        assert rc2 == 0
        assert ctx2 == []
        assert "Child verdict reused from" in out2
        assert "Ready to close: Yes" in out2
        assert "Child screens pending" not in out2
        assert "Child assessment reasons:" not in out2
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in err2

    # ------------------------------------------------------------------
    # --force still re-audits all children (AC4)
    # ------------------------------------------------------------------

    def test_force_reaudits_children_in_main_slot(self, capsys):
        """``--force`` still bypasses child-verdict reuse in in-main-slot
        mode: a child with a fresh stored audit is reused by default but
        re-audited when forced (its screen is freshly emitted)."""
        # Baseline (no --force): the fresh stored child audit is reused.
        _, out_reuse, err_reuse, _ = self._run_interactive(
            capsys, stored_child_audit=self._STORED_READY_CHILD_AUDIT,
        )
        assert "Child verdict reused from" in out_reuse
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER not in err_reuse

        # --force bypasses reuse: the child is re-audited in the main slot.
        rc, out_force, err_force, ctx = self._run_interactive(
            capsys, stored_child_audit=self._STORED_READY_CHILD_AUDIT,
            force=True,
        )
        assert rc == 0
        assert ctx == []  # still no child pi subprocess
        assert "Child verdict reused from" not in out_force
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err_force
        assert "phase1_child_screen" in err_force
        assert "CHILD-1" in err_force


#: Title/description of the child whose in-main-slot screens are re-resolved
#: from a ``phase1_children`` checkpoint. The stored child audit the runner
#: must reuse is fingerprinted against exactly this content.
_RESUME_CHILD_TITLE = "Child CHILD-1"
_RESUME_CHILD_DESC = (
    "## Acceptance Criteria\n- CAC1: child criterion 1"
)


def _stored_child_audit_report(fingerprint: str) -> str:
    """A stored, v2-fingerprinted child audit saying ``Ready to close: Yes``.

    Shaped like a real persisted child audit so the production reader
    (``_child_content_changed`` → ``_get_child_audit_verdict``) judges it
    fresh and ``_child_acs_from_own_audit`` can parse the AC table.
    """
    return (
        "Ready to close: Yes\n\n"
        "Audit report for work item CHILD-1\n\n"
        f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
        "## Acceptance Criteria Status\n\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | CAC1: child criterion 1 | met | child.py:1 |"
    )


def _stored_child_audit_not_ready_report(fingerprint: str) -> str:
    """A stored, v2-fingerprinted child audit saying ``Ready to close: No``.

    Models a serviced in-main-slot child whose own audit judged its criteria
    and found them unmet (parent AC3).
    """
    return (
        "Ready to close: No\n\n"
        "Audit report for work item CHILD-1\n\n"
        f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
        "## Acceptance Criteria Status\n\n"
        "| # | Criterion | Verdict | Evidence |\n"
        "|---|-----------|---------|----------|\n"
        "| 1 | CAC1: child criterion 1 | unmet | child.py:4 — blocking |"
    )


def _proc(stdout: str):
    """A canned successful ``wl`` result (the runner's return shape)."""
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


def _resume_child_runner(stored_report: str | None):
    """Mock ``wl`` runner serving CHILD-1's item + its stored audit.

    ``stored_report=None`` models a child with NO stored audit (AC4).
    """
    runner = mock.MagicMock()

    def _side_effect(cmd):
        cmd_str = " ".join(str(c) for c in cmd)
        if "audit-show" in cmd_str:
            if stored_report is None:
                return _proc(json.dumps({"success": True, "audit": None}))
            return _proc(json.dumps({
                "success": True,
                "audit": {
                    "auditedAt": "2026-10-29T00:10:00.000Z",
                    "rawOutput": stored_report,
                },
            }))
        if "show" in cmd_str and "--json" in cmd_str:
            return _proc(json.dumps({
                "success": True,
                "workItem": {
                    "id": "CHILD-1",
                    "title": _RESUME_CHILD_TITLE,
                    "description": _RESUME_CHILD_DESC,
                    "status": "in_progress",
                    "stage": "in_review",
                },
                "comments": [],
            }))
        return _proc(json.dumps({"success": True}))

    runner.side_effect = _side_effect
    return runner


#: Parent item identity served to the composed end-to-end resume tests. The
#: stored parent report's v2 fingerprint is computed from exactly these
#: fields, so the freshness gate sees the report as content-fresh.
_E2E_TITLE = "In-main-slot resume cycle"
_E2E_DESC = (
    "## Acceptance Criteria\n- AC1: collect the serviced child audits"
)
_E2E_PARENT_AUDITED_AT = "2026-10-29T00:20:00.000Z"


def _e2e_runner(children: list[dict], *, child_audit_raw: dict | None = None,
                parent_audit_raw: str | None = None,
                head: str = HEAD_BASE):
    """Mock ``wl``/``git`` runner for the composed resume-cycle tests.

    Wraps :func:`_make_phase1_runner` (same parent/child ``wl`` surface) and
    adds the three things the composed cycle needs beyond Phase 1:

    - ``git rev-parse HEAD`` — enables the production ``CheckpointStore``
      (without a 40-hex HEAD the runner disables checkpointing).
    - ``wl audit-show TEST-1`` — the stored parent report the freshness gate
      inspects (``None`` models the pre-service first run).
    - ``wl show TEST-1 --json`` — the parent item with a real title and
      description, so the content fingerprint is computed from real data.
    """
    base_runner, audit_shows = _make_phase1_runner(
        children, child_audit_raw=child_audit_raw,
    )
    inner = base_runner.side_effect

    def _side_effect(cmd):
        cmd_list = list(cmd)
        cmd_str = " ".join(str(c) for c in cmd_list)
        if cmd_str == "git rev-parse HEAD":
            return _proc(head)
        if "audit-show" in cmd_list:
            target = cmd_list[cmd_list.index("audit-show") + 1]
            if target == "TEST-1" and parent_audit_raw is not None:
                return _proc(json.dumps({
                    "success": True,
                    "workItemId": "TEST-1",
                    "audit": {
                        "workItemId": "TEST-1",
                        "auditedAt": _E2E_PARENT_AUDITED_AT,
                        "rawOutput": parent_audit_raw,
                    },
                }))
        if (
            "show" in cmd_list
            and "--children" not in cmd_list
            and "TEST-1" in cmd_list
        ):
            return _proc(json.dumps({
                "success": True,
                "workItem": {
                    "id": "TEST-1",
                    "title": _E2E_TITLE,
                    "description": _E2E_DESC,
                    "status": "open",
                    "stage": "in_progress",
                },
                "comments": [],
            }))
        return inner(cmd)

    base_runner.side_effect = stateful_wl_side_effect(_side_effect)
    return base_runner, audit_shows


def _stored_parent_report() -> str:
    """A v2-fingerprinted parent report whose child screens are outstanding.

    Shaped like a real persisted in-main-slot report: ``Ready to close: No``
    with the ``Child screens pending`` gate header. The fingerprint is
    computed from :data:`_E2E_TITLE` / :data:`_E2E_DESC` — the item the
    freshness gate reads — so the report is FRESH by content and only the
    pending-screens signal can legitimately force the pipeline to re-run.
    """
    fingerprint = audit_runner._compute_content_fingerprint(
        None, "TEST-1",
        work_item={"title": _E2E_TITLE, "description": _E2E_DESC},
    )
    assert fingerprint is not None
    return (
        "Ready to close: No\n\n"
        "Audit report for work item TEST-1\n\n"
        f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
        "Child screens pending: 1 (main-slot) — CHILD-1"
    )


def _seed_phase1_pending_checkpoint(tmp_path, child: dict,
                                    head: str = HEAD_BASE) -> CheckpointStore:
    """Seed a ``phase1_children``-completed checkpoint whose child still
    carries the unserviced in-main-slot placeholder (the state a previous
    session leaves behind when it dies after emitting the screens).

    Returns the reopened store so the caller can assert ``is_resuming``.
    """
    store = CheckpointStore("TEST-1", head, tmp_path)
    store.mark_completed(PHASE_PARENT, {
        "ac_results": [
            {"text": "AC1: x", "verdict": "met", "evidence": "parent"},
        ],
    })
    store.mark_completed(PHASE_CHILDREN, {
        "ac_results": [
            {"text": "AC1: x", "verdict": "met", "evidence": "parent"},
        ],
        "child_results": [{
            "id": "CHILD-1",
            "title": child.get("title", ""),
            "status": child.get("status", ""),
            "stage": child.get("stage", ""),
            "child_audit_ready": False,
            "ac_results": [{
                "text": "CAC1: child criterion 1",
                "verdict": "partial",
                "evidence": _MAIN_SLOT_PENDING_SCREEN_EVIDENCE,
            }],
        }],
        "child_persist_results": [],
        "phase2_completed": False,
        "phase2_skip_note": None,
    })
    reopened = CheckpointStore("TEST-1", head, tmp_path)
    assert reopened.is_resuming is True
    return reopened


class TestCheckpointResumeReResolvesPendingChildren:
    """D2 reproduction (parent AC1/AC2/AC3): a ``phase1_children``
    checkpoint whose restored ``child_results`` still carry
    ``MAIN_SLOT_PENDING_EVIDENCE`` must NOT be treated as a completed phase —
    the serviced children are re-resolved through the existing child-verdict
    reuse path.

    Observed production symptom (LP-0MVLNBCTN0084MT0): the parent run was
    resumed from a ``phase1_children`` checkpoint whose children still held
    the in-main-slot placeholders. The resume branch skipped child
    orchestration entirely, ``_pending_main_slot_child_ids`` re-derived the
    same pending list from the restored placeholders, and the (already
    serviced and persisted) child audits were never consulted — so the
    ``Child screens pending`` gate could never clear and the pending-screens
    signal was rewritten run after run.

    RED before the D2 fix: the checkpoint restore short-circuited the
    orchestration block, so the pending list stayed ``['CHILD-1']``.
    GREEN after the D2 fix: the restored pending children are re-resolved
    through the child-verdict reuse path before the pending gate is computed.
    """

    def _seed_resume_store(self, tmp_path, phase2=False) -> CheckpointStore:
        """Seed a ``phase1_children``-completed checkpoint whose child still
        carries the unserviced in-main-slot placeholder, then reopen it as a
        resuming store (production ``CheckpointStore``, not a fake).

        ``phase2=True`` additionally marks Phase 2 completed (the early-return
        resume path).
        """
        store = CheckpointStore("TEST-1", HEAD_BASE, tmp_path)
        store.mark_completed(PHASE_CHILDREN, {
            "ac_results": [
                {"text": "AC1: x", "verdict": "met", "evidence": "parent"},
            ],
            "child_results": [{
                "id": "CHILD-1",
                "title": _RESUME_CHILD_TITLE,
                "status": "in_progress",
                "stage": "in_review",
                "child_audit_ready": False,
                "ac_results": [{
                    "text": "CAC1: child criterion 1",
                    "verdict": "partial",
                    "evidence": _MAIN_SLOT_PENDING_SCREEN_EVIDENCE,
                }],
            }],
            "child_persist_results": [],
            "phase2_completed": False,
            "phase2_skip_note": None,
        })
        if phase2:
            store.mark_completed(PHASE_PHASE2, {})
        reopened = CheckpointStore("TEST-1", HEAD_BASE, tmp_path)
        assert reopened.is_resuming is True
        return reopened

    def _resume_ctx(self, tmp_path, runner):
        """A resuming ctx whose restored child still holds the placeholder."""
        ctx = _make_ctx(checkpoint=self._seed_resume_store(tmp_path))
        ctx.runner = runner
        ctx.checkpoint_dir = str(tmp_path)
        ctx.owning_root = tmp_path
        ctx.audit_children = True
        ctx.children = [{
            "id": "CHILD-1",
            "title": _RESUME_CHILD_TITLE,
            "description": _RESUME_CHILD_DESC,
            "status": "in_progress",
            "stage": "in_review",
        }]
        return ctx

    def _write_signal(self, tmp_path):
        signal = tmp_path / f"TEST-1{audit_runner.PENDING_SCREENS_SIGNAL_SUFFIX}"
        audit_runner._write_pending_screens_signal(
            ["CHILD-1"], "TEST-1", tmp_path, str(tmp_path),
        )
        assert signal.is_file()
        return signal

    def _fingerprint(self) -> str:
        fingerprint = audit_runner._compute_content_fingerprint(
            None, "CHILD-1",
            work_item={
                "title": _RESUME_CHILD_TITLE,
                "description": _RESUME_CHILD_DESC,
            },
        )
        assert fingerprint is not None
        return fingerprint

    def _run_resume(self, ctx):
        """Run the resume pipeline with every pi entry point mocked."""
        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs"
        ) as child_screen, mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as pi_call, mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=lambda issue, ac, cr, **kw: (ac, cr, True),
        ):
            rc = audit_runner._phase_children(ctx)
        return rc, child_screen, pi_call

    def test_resume_reuses_serviced_child_audit_and_clears_pending(
        self, tmp_path,
    ):
        fingerprint = audit_runner._compute_content_fingerprint(
            None, "CHILD-1",
            work_item={
                "title": _RESUME_CHILD_TITLE,
                "description": _RESUME_CHILD_DESC,
            },
        )
        assert fingerprint is not None
        runner = _resume_child_runner(_stored_child_audit_report(fingerprint))

        # The pending-screens signal from the previous run is on disk (written
        # through the production writer, so the test exercises the real
        # reader/writer pair).
        signal = tmp_path / f"TEST-1{audit_runner.PENDING_SCREENS_SIGNAL_SUFFIX}"
        audit_runner._write_pending_screens_signal(
            ["CHILD-1"], "TEST-1", tmp_path, str(tmp_path),
        )
        assert signal.is_file()

        reopened = self._seed_resume_store(tmp_path)
        ctx = _make_ctx(checkpoint=reopened)
        ctx.runner = runner
        ctx.checkpoint_dir = str(tmp_path)
        ctx.owning_root = tmp_path
        ctx.audit_children = True
        ctx.children = [{
            "id": "CHILD-1",
            "title": _RESUME_CHILD_TITLE,
            "description": _RESUME_CHILD_DESC,
            "status": "in_progress",
            "stage": "in_review",
        }]

        with mock.patch.object(
            audit_runner, "_phase1_review_child_acs"
        ) as child_screen, mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as pi_call, mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis",
            side_effect=lambda issue, ac, cr, **kw: (ac, cr, True),
        ):
            rc = audit_runner._phase_children(ctx)

        assert rc is None
        # The freshly persisted child audit is reused: zero pi calls, no
        # in-main-slot screen re-emitted.
        child_screen.assert_not_called()
        pi_call.assert_not_called()
        assert ctx.child_results[0].get("reused_from"), "child audit not reused"
        assert ctx.child_results[0].get("child_audit_ready") is True
        # D2 core assertion: the replay of the pending placeholders is gone.
        pending = audit_runner._pending_main_slot_child_ids(ctx.child_results)
        assert pending == [], f"D2: checkpoint resume replays pending {pending}"

    def test_resume_then_report_clears_gate_and_signal(
        self, tmp_path, capsys,
    ):
        """AC2: after a serviced child audit is reused, the report carries no
        ``Child screens pending`` gate, the JSON payload's
        ``pending_child_screens`` is empty and the pending-screens signal file
        is removed."""
        runner = _resume_child_runner(
            _stored_child_audit_report(self._fingerprint()),
        )
        signal = self._write_signal(tmp_path)
        ctx = self._resume_ctx(tmp_path, runner)
        ctx.json_mode = True

        rc, child_screen, pi_call = self._run_resume(ctx)
        assert rc is None
        child_screen.assert_not_called()
        pi_call.assert_not_called()

        assert audit_runner._phase_report(ctx) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["pending_child_screens"] == []
        assert not signal.exists(), "pending-screens signal not cleared"

    def test_phase2_checkpoint_resume_also_re_resolves_pending(self, tmp_path):
        """AC1: the ``phase2``-completed early return must not replay the
        pending child placeholders either — the serviced child audit is
        collected there too (zero pi calls)."""
        runner = _resume_child_runner(
            _stored_child_audit_report(self._fingerprint()),
        )
        ctx = _make_ctx(
            checkpoint=self._seed_resume_store(tmp_path, phase2=True),
        )
        ctx.runner = runner
        ctx.checkpoint_dir = str(tmp_path)
        ctx.owning_root = tmp_path
        ctx.audit_children = True
        ctx.children = [{
            "id": "CHILD-1",
            "title": _RESUME_CHILD_TITLE,
            "description": _RESUME_CHILD_DESC,
            "status": "in_progress",
            "stage": "in_review",
        }]

        rc, child_screen, pi_call = self._run_resume(ctx)
        assert rc is None
        child_screen.assert_not_called()
        pi_call.assert_not_called()
        assert ctx.child_results[0].get("reused_from")
        assert (
            audit_runner._pending_main_slot_child_ids(ctx.child_results) == []
        )

    def test_resume_serviced_not_ready_child_is_assessed_and_blocks(
        self, tmp_path, capsys,
    ):
        """AC3: a serviced child whose own audit says ``Ready to close: No``
        is reported as assessed (``child_audit_not_ready``, never
        ``not_assessed``) and still blocks the parent's readiness."""
        runner = _resume_child_runner(
            _stored_child_audit_not_ready_report(self._fingerprint()),
        )
        signal = self._write_signal(tmp_path)
        ctx = self._resume_ctx(tmp_path, runner)
        ctx.json_mode = True

        rc, _, pi_call = self._run_resume(ctx)
        assert rc is None
        pi_call.assert_not_called()
        child = ctx.child_results[0]
        assert child.get("child_audit_not_ready") is True
        assert child.get("reused_from")
        # Screens were serviced: no child remains pending.
        assert audit_runner._pending_main_slot_child_ids(ctx.child_results) == []

        assert audit_runner._phase_report(ctx) == 0
        # The pending-screens signal is cleared by the report phase once the
        # pending list is empty.
        assert not signal.exists()
        payload = json.loads(capsys.readouterr().out)
        assert "CHILD-1" not in payload["child_reasons"]
        assert payload["not_assessed_children"] == []
        # Assessed is not ready: the child's blocking verdicts still block.
        assert payload["ready_to_close"] is False

    def test_resume_no_stored_audit_keeps_child_pending(self, tmp_path):
        """AC4: a child with NO stored audit stays pending — the fast path
        never fabricates a serviced state (and makes no pi call)."""
        runner = _resume_child_runner(None)
        signal = self._write_signal(tmp_path)
        ctx = self._resume_ctx(tmp_path, runner)

        rc, child_screen, pi_call = self._run_resume(ctx)
        assert rc is None
        child_screen.assert_not_called()
        pi_call.assert_not_called()
        assert ctx.child_results[0].get("reused_from") is None
        assert ctx.child_results[0].get("child_audit_ready") is False
        assert audit_runner._pending_main_slot_child_ids(
            ctx.child_results
        ) == ["CHILD-1"]
        assert signal.is_file()

    def test_resume_stale_child_audit_keeps_child_pending(self, tmp_path):
        """AC4: a stored audit whose content fingerprint no longer matches the
        child (title/description changed) is stale — the child stays
        pending."""
        runner = _resume_child_runner(
            _stored_child_audit_report("stale-fingerprint-mismatch"),
        )
        signal = self._write_signal(tmp_path)
        ctx = self._resume_ctx(tmp_path, runner)

        rc, _, pi_call = self._run_resume(ctx)
        assert rc is None
        pi_call.assert_not_called()
        assert ctx.child_results[0].get("reused_from") is None
        assert audit_runner._pending_main_slot_child_ids(
            ctx.child_results
        ) == ["CHILD-1"]
        assert signal.is_file()

    def test_resume_writes_resolved_child_results_back_to_checkpoint(
        self, tmp_path,
    ):
        """AC6: the refreshed ``child_results`` are persisted into the
        checkpoint before Phase 2, so a later resume sees resolved children
        instead of replaying the placeholders."""
        runner = _resume_child_runner(
            _stored_child_audit_report(self._fingerprint()),
        )
        ctx = self._resume_ctx(tmp_path, runner)

        rc, _, _ = self._run_resume(ctx)
        assert rc is None

        stored = CheckpointStore(
            "TEST-1", HEAD_BASE, tmp_path,
        ).accumulated_state()["child_results"]
        assert audit_runner._pending_main_slot_child_ids(stored) == []
        assert stored[0].get("reused_from")
        assert stored[0].get("child_audit_ready") is True


class TestResumeCycleEndToEnd:
    """Composed end-to-end coverage of the in-main-slot resume cycle
    (parent AC2/AC5/AC6; SA-0MW2J7OPP007MAPV AC1/AC2).

    The two mechanism fixes (D1: the freshness gate must not treat a report
    with outstanding child screens as a fresh final verdict; D2: a
    ``phase1_children`` checkpoint restore must re-resolve its pending
    placeholders) compose with the pre-existing child-verdict reuse path into
    one reachable cycle:

    ``fresh parent audit + <id>.pending-screens.json`` → no freshness
    short-circuit → collect the serviced child audits → no
    ``Child screens pending`` gate, empty ``pending_child_screens``, signal
    removed, zero child-screening pi calls.

    Two service cases are distinguished, mirroring the documented contract:

    - **same session** — the invoking session services the screens and
      re-runs the parent in the same process. The first run's checkpoint was
      cleared on its success, so the re-run converges through the ordinary
      Phase-1 child-verdict pre-pass.
    - **later session** — a previous session died after completing the child
      orchestration, leaving a ``phase1_children`` checkpoint with the
      unserviced placeholders on disk. The next session services the screens
      and re-runs the parent; the checkpoint resume re-resolves the pending
      children (D2) instead of replaying the placeholders.
    """

    def _child(self) -> dict:
        return _phase1_child(1, stage="in_review")

    def _child_fingerprint(self) -> str:
        fingerprint = audit_runner._compute_content_fingerprint(
            None, "CHILD-1", work_item=self._child(),
        )
        assert fingerprint is not None
        return fingerprint

    def _signal(self, tmp_path) -> Path:
        signal = tmp_path / f"TEST-1{audit_runner.PENDING_SCREENS_SIGNAL_SUFFIX}"
        return signal

    def _write_signal(self, tmp_path) -> Path:
        audit_runner._write_pending_screens_signal(
            ["CHILD-1"], "TEST-1", tmp_path, str(tmp_path),
        )
        signal = self._signal(tmp_path)
        assert signal.is_file()
        return signal

    # ------------------------------------------------------------------
    # Case (a) — the invoking session services the screens in the SAME
    # session as the first run
    # ------------------------------------------------------------------

    def test_same_session_service_then_rerun_collects_and_clears(
        self, tmp_path, capsys,
    ):
        """Same-session case: the first run emits the screens and persists
        the resumable signal; the session services them and re-runs the
        parent. The re-run is not short-circuited by the content-fresh stored
        parent report, collects the serviced child audit with zero
        child-screening pi calls, and leaves no gate, no pending child and no
        signal behind."""
        signal = self._signal(tmp_path)

        # Run 1 — pre-service: no stored parent audit, no stored child audit.
        rc1, out1, err1, ctx1 = self._run(capsys, tmp_path)
        assert rc1 == 0
        assert ctx1 == []  # in-main-slot: no child pi subprocess
        assert "Child screens pending: 1 (main-slot) — CHILD-1" in out1
        assert audit_runner.IN_MAIN_SLOT_WORK_MARKER in err1
        assert signal.is_file(), "first run did not persist the signal"
        # The first run succeeded, so its checkpoint was cleared — the
        # same-session re-run never resumes from it.
        assert not (tmp_path / "TEST-1.checkpoint.json").exists()

        # The invoking session services the screens: the child now has a
        # fresh persisted audit and the parent its (content-fresh) report.
        rc2, out2, err2, ctx2 = self._run(
            capsys, tmp_path,
            stored_child_audit=_stored_child_audit_report(
                self._child_fingerprint(),
            ),
            parent_audit_raw=_stored_parent_report(),
            json_mode=True,
        )

        assert rc2 == 0
        assert ctx2 == [], "a serviced child must cost zero child pi calls"
        # D1: the content-fresh stored report did NOT short-circuit — the
        # outstanding signal forced the pipeline to resume.
        assert "Skipping: audit still fresh" not in out2
        assert "Freshness gate bypassed" in err2
        assert "CHILD-1" in err2

        payload = json.loads(out2)
        assert payload["pending_child_screens"] == []
        assert payload["ready_to_close"] is True
        assert not signal.exists(), "pending-screens signal not cleared"

    # ------------------------------------------------------------------
    # Case (b) — a LATER session services the screens and re-runs the parent
    # ------------------------------------------------------------------

    def test_later_session_resume_collects_without_replaying_placeholders(
        self, tmp_path, capsys,
    ):
        """Later-session case: the previous session died after completing the
        child orchestration, leaving a ``phase1_children`` checkpoint whose
        children still carry the in-main-slot placeholders plus the signal.
        The next session services the screens and re-runs the parent; the
        checkpoint resume re-resolves the pending children (D2) instead of
        replaying them, so the report has no gate and no pending child and
        the signal is removed — with zero child-screening pi calls."""
        _seed_phase1_pending_checkpoint(tmp_path, self._child())
        checkpoint_path = tmp_path / "TEST-1.checkpoint.json"
        assert checkpoint_path.is_file()
        signal = self._write_signal(tmp_path)

        rc, out, err, child_ctx = self._run(
            capsys, tmp_path,
            stored_child_audit=_stored_child_audit_report(
                self._child_fingerprint(),
            ),
            parent_audit_raw=_stored_parent_report(),
        )

        assert rc == 0
        assert child_ctx == [], "a serviced child must cost zero child pi calls"
        # D1: the content-fresh stored report did not short-circuit.
        assert "Skipping: audit still fresh" not in out
        assert "Freshness gate bypassed" in err
        # D2: the checkpoint resume ran and re-resolved the pending child.
        assert "[checkpoint] Skipping completed phase" in err
        assert "Child verdict reused from" in out
        # The composed result: no gate, ready, signal cleared.
        assert "Child screens pending" not in out
        assert "Child assessment reasons:" not in out
        assert "Ready to close: Yes" in out
        assert not signal.exists(), "pending-screens signal not cleared"
        assert not checkpoint_path.exists(), "resumed checkpoint not cleared"

    # ------------------------------------------------------------------
    # Harness
    # ------------------------------------------------------------------

    def _run(self, capsys, tmp_path, *, stored_child_audit=None,
             parent_audit_raw=None, json_mode=False):
        """Drive ``cmd_issue`` through the shared interactive harness with
        the composed resume-cycle fixtures."""
        return TestInteractiveRegressionGuard._run_interactive(
            self, capsys,
            stored_child_audit=stored_child_audit,
            checkpoint_dir=tmp_path,
            parent_audit_raw=parent_audit_raw,
            json_mode=json_mode,
        )
