"""Regression tests for the content-scoped producer-review flag lifecycle.

SA-0MVR9B1UH002BUST — the audit runner must stop re-arming
``needsProducerReview`` on a passing re-audit of an item the producer has
already reviewed (over unchanged content), and must start setting it on any
independent failing (``Ready to close: No``) top-level audit.

Acceptance criteria pinned here (each test names the AC it protects):

* **AC1** — reviewed pass preserved (no ``--needs-producer-review`` write).
* **AC2** — never-reviewed pass still flags.
* **AC3** — independent ``No`` flags with and without ``merge_gate_blocker``;
  the cascade-demotion-suppressed ``No`` leaves the flag untouched (Q2 = no).
* **AC4** — the signal is content-scoped (content change re-flags) and the
  decision is recorded in the persisted report as a metadata line.
* **AC5** — all four transitions, the child exemption, the cascade case and
  the ``wl audit-show``-unavailable fail-safe run in the default suite.
* **AC6** — children are never flagged.

The mock ``wl`` runner models the real worklog closely enough that the
runner's post-update readback verification is satisfied
(``stateful_wl_side_effect``) and the stored audit record is served by
``audit_show_response`` (the pre-audit audit-record knob).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.wl_helpers import (
    audit_show_response,
    stateful_wl_side_effect,
)

# The mocked work item. Its content fingerprint is what the stored passing
# audit must match for a producer review to be preserved (AC1/AC4).
_ITEM = {
    "id": "TEST-1",
    "title": "Producer review flag lifecycle",
    "description": (
        "## Acceptance Criteria\n"
        "- AC1: preserve a producer review across a passing re-audit\n"
        "- AC2: flag a never-reviewed pass\n"
    ),
}

_YES_REPORT = "Ready to close: Yes\n\n## Summary\nAll met."
_NO_REPORT = "Ready to close: No\n\n## Summary\nGenuine gap."


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _current_fingerprint(item: dict | None = None) -> str:
    """The pre-audit content fingerprint for the mocked item."""
    wi = dict(_ITEM)
    if item:
        wi.update(item)
    return audit_runner._compute_content_fingerprint(
        lambda cmd: None, "TEST-1", work_item=wi,
    )


_FINGERPRINT = _current_fingerprint()
_CHANGED_FINGERPRINT = _current_fingerprint(
    {"description": "## Acceptance Criteria\n- AC1: materially different\n"},
)


def _make_runner(updates, *, status="completed", stage="in_review",
                 parent_id=None, needs_producer_review=False,
                 prior_audit_record="none", audit_show_returncode=0,
                 post_persist_audit=False, item=None):
    """Build a stateful mock ``wl`` runner for the review-lifecycle paths.

    *prior_audit_record* selects the stored pre-audit record served by
    ``wl audit-show``: ``"none"`` (no prior audit), ``"reviewed"`` (a passing
    audit of the current content), ``"changed"`` (a passing audit of content
    that has since changed) or a ready-made ``SimpleNamespace`` response.
    With *post_persist_audit*, every ``audit-show`` after the pre-audit fetch
    serves a freshly persisted passing audit, modelling the runner's
    post-persist readback verification.
    """
    mock_runner = mock.MagicMock()
    base_item = dict(_ITEM)
    if item:
        base_item.update(item)
    audit_show_calls = 0

    def _response():
        if prior_audit_record == "none":
            return audit_show_response(ready_to_close=None)
        if prior_audit_record == "reviewed":
            return audit_show_response(
                ready_to_close=True, fingerprint=_FINGERPRINT,
                work_item_id="TEST-1",
            )
        if prior_audit_record == "changed":
            return audit_show_response(
                ready_to_close=True, fingerprint=_CHANGED_FINGERPRINT,
                work_item_id="TEST-1",
            )
        return prior_audit_record

    def _side_effect(cmd):
        nonlocal audit_show_calls
        cmd_str = " ".join(cmd)
        if "audit-show" in cmd_str:
            audit_show_calls += 1
            if post_persist_audit and audit_show_calls > 1:
                return audit_show_response(
                    ready_to_close=True, fingerprint=_FINGERPRINT,
                    work_item_id="TEST-1",
                )
            if audit_show_returncode:
                return SimpleNamespace(
                    returncode=audit_show_returncode, stdout="",
                    stderr="wl audit-show unavailable",
                )
            return _response()
        if "update" in cmd_str:
            updates.append(list(cmd))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )
        if "show" in cmd_str and "--children" in cmd_str:
            # Deliberately no acceptance criteria here: the parent AC
            # screening would otherwise invoke a real ``pi`` model call.
            # Only the plain ``wl show`` below (used for the content
            # fingerprint) carries the item's title/description.
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": base_item["id"],
                        "title": base_item["title"],
                        "description": "",
                        "status": status,
                        "stage": stage,
                    },
                    "children": [],
                }),
                stderr="",
            )
        if "show" in cmd_str:
            wi = dict(base_item, status=status, stage=stage,
                      needsProducerReview=needs_producer_review)
            if parent_id is not None:
                wi["parentId"] = parent_id
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True, "workItem": wi}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
    return mock_runner


def _run_issue(updates, report, *, persist=False, runner_kwargs=None):
    """Run ``cmd_issue`` with a controlled verdict and no real subprocesses."""
    kwargs = dict(runner_kwargs or {})
    runner = _make_runner(updates, **kwargs)
    with (
        mock.patch.object(
            audit_runner, "_assemble_issue_report", return_value=report,
        ),
        mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            return_value={"success": True, "findings": [], "fixes_applied": 0},
        ),
    ):
        return audit_runner.cmd_issue(
            "TEST-1", persist=persist, force=True, runner=runner,
            batch_drain=False,
        )


def _last_update(updates):
    """The terminal ``wl update`` command (leading ``--worklog-dir`` stripped)."""
    assert updates, "expected at least one wl update command"
    cmd = updates[-1]
    if cmd[:1] == ["wl"] and len(cmd) >= 3 and cmd[1] == "--worklog-dir":
        cmd = ["wl"] + cmd[3:]
    return cmd


def _flag_written(cmd) -> bool:
    """Whether a ``wl update`` command writes the producer-review flag."""
    if "--needs-producer-review" not in cmd:
        return False
    value = cmd[cmd.index("--needs-producer-review") + 1]
    assert value == "yes", "the runner must never clear the review flag"
    return True


# ---------------------------------------------------------------------------
# Pure predicate (AC1/AC2/AC4)
# ---------------------------------------------------------------------------

def _assert_cas_lifecycle_write(updates, *, status, stage, producer_flag=None):
    """Assert the terminal lifecycle write's contract, CAS guard included.

    AC5 (SA-0MWFMV31W00532T3): restore/demotion writes carry
    ``--if-status``/``--if-stage`` so they can never blind-overwrite a
    concurrent writer. The exact CAS *values* (the pair equals the state read
    immediately before the write) are pinned by
    ``test_audit_runner_restore_race.py::TestRestoreCasGuards``, which drives a
    harness with full control of the observed lifecycle; here the guard's
    presence and the unchanged lifecycle contract are what matter.
    """
    assert updates, "expected at least one wl update command"
    cmd = updates[-1]
    if cmd[:1] == ["wl"] and len(cmd) >= 3 and cmd[1] == "--worklog-dir":
        cmd = ["wl"] + cmd[3:]

    def value(command, flag):
        return command[command.index(flag) + 1] if flag in command else None

    assert cmd[:3] == ["wl", "update", "TEST-1"], cmd
    assert value(cmd, "--status") == status, cmd
    assert value(cmd, "--stage") == stage, cmd
    assert value(cmd, "--assignee") == "", cmd
    if producer_flag is None:
        assert "--needs-producer-review" not in cmd, cmd
    else:
        assert value(cmd, "--needs-producer-review") == producer_flag, cmd
    assert value(cmd, "--if-status"), cmd
    assert value(cmd, "--if-stage"), cmd
    assert cmd[-1] == "--json", cmd


class TestProducerReviewPreservedPredicate:
    """Unit tests for the single decision point ``_producer_review_preserved``."""

    def test_never_reviewed_is_not_preserved(self):
        """AC2: no prior audit means the pass must flag."""
        wi = {"needsProducerReview": False}
        assert audit_runner._producer_review_preserved(wi, None) is False

    def test_prior_failing_audit_is_not_preserved(self):
        """A prior ``No`` audit is not a producer review of passing work."""
        wi = {"needsProducerReview": False}
        prior = {
            "readyToClose": False,
            "fingerprint": _FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        assert audit_runner._producer_review_preserved(wi, prior) is False

    def test_reviewed_unchanged_content_is_preserved(self):
        """AC1: a cleared flag over an unchanged passing audit is preserved."""
        wi = {"needsProducerReview": False}
        prior = {
            "readyToClose": True,
            "fingerprint": _FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        assert audit_runner._producer_review_preserved(wi, prior) is True

    def test_changed_content_is_not_preserved(self):
        """AC4: content changed since the review invalidates it."""
        wi = {"needsProducerReview": False}
        prior = {
            "readyToClose": True,
            "fingerprint": _CHANGED_FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        assert audit_runner._producer_review_preserved(wi, prior) is False

    def test_already_flagged_is_never_preserved(self):
        """The runner never clears an existing flag."""
        wi = {"needsProducerReview": True}
        prior = {
            "readyToClose": True,
            "fingerprint": _FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        assert audit_runner._producer_review_preserved(wi, prior) is False

    def test_missing_fingerprint_fails_safe_to_flag(self):
        """Legacy/fingerprint-less audits are ambiguous → fail safe → flag."""
        wi = {"needsProducerReview": False}
        prior = {
            "readyToClose": True,
            "fingerprint": None,
            "currentFingerprint": _FINGERPRINT,
        }
        assert audit_runner._producer_review_preserved(wi, prior) is False


# ---------------------------------------------------------------------------
# End-to-end lifecycle through cmd_issue (AC1/AC2/AC3/AC5/AC6)
# ---------------------------------------------------------------------------

class TestProducerReviewLifecycle:
    """The four transitions plus the child exemption and fail-safe, end to end."""

    def test_never_reviewed_pass_flags(self):
        """AC2: a never-reviewed top-level pass still enters the queue."""
        updates = []
        rc = _run_issue(
            updates, _YES_REPORT,
            runner_kwargs={"prior_audit_record": "none"},
        )
        assert rc == 0
        assert _flag_written(_last_update(updates))

    def test_reviewed_pass_is_preserved(self):
        """AC1: a producer-reviewed unchanged pass writes no flag at all."""
        updates = []
        rc = _run_issue(
            updates, _YES_REPORT,
            runner_kwargs={
                "prior_audit_record": "reviewed",
                "needs_producer_review": False,
            },
        )
        assert rc == 0
        cmd = _last_update(updates)
        assert "--needs-producer-review" not in cmd
        assert cmd == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--assignee", "", "--json",
        ]

    def test_review_then_content_change_flags_again(self):
        """AC4: content changed since the review → a fresh review is required."""
        updates = []
        rc = _run_issue(
            updates, _YES_REPORT,
            runner_kwargs={
                "prior_audit_record": "changed",
                "needs_producer_review": False,
            },
        )
        assert rc == 0
        assert _flag_written(_last_update(updates))

    def test_already_flagged_pass_never_clears_the_flag(self):
        """The runner only ever writes 'yes' — it never clears a review."""
        updates = []
        rc = _run_issue(
            updates, _YES_REPORT,
            runner_kwargs={
                "prior_audit_record": "reviewed",
                "needs_producer_review": True,
            },
        )
        assert rc == 0
        assert _flag_written(_last_update(updates))

    def test_independent_fail_flags(self):
        """AC3: an ordinary independent ``No`` surfaces the item."""
        updates = []
        rc = _run_issue(
            updates, _NO_REPORT,
            runner_kwargs={"prior_audit_record": "none"},
        )
        assert rc == 0
        cmd = _last_update(updates)
        assert "--status" in cmd and "open" in cmd
        assert _flag_written(cmd)

    def test_cascade_suppressed_fail_does_not_flag(self):
        """AC3/Q2: a parent-triggered re-audit of an in_review item leaves
        the flag untouched even though the verdict is ``No``."""
        updates = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            rc = _run_issue(updates, _NO_REPORT)
        assert rc == 0
        _assert_cas_lifecycle_write(
            updates, status="completed", stage="in_review",
        )

    def test_child_pass_never_flags(self):
        """AC6: a passing child is covered by the parent's review."""
        updates = []
        rc = _run_issue(updates, _YES_REPORT,
                        runner_kwargs={"parent_id": "PARENT-1"})
        assert rc == 0
        assert "--needs-producer-review" not in _last_update(updates)

    def test_child_independent_fail_never_flags(self):
        """AC6: a failing child does not flood the producer queue."""
        updates = []
        rc = _run_issue(updates, _NO_REPORT,
                        runner_kwargs={"parent_id": "PARENT-1"})
        assert rc == 0
        assert "--needs-producer-review" not in _last_update(updates)

    def test_audit_show_unavailable_fails_safe_to_flag(self):
        """AC5: an unreadable prior audit is ambiguous → flag (never preserve)."""
        updates = []
        rc = _run_issue(
            updates, _YES_REPORT,
            runner_kwargs={"audit_show_returncode": 1},
        )
        assert rc == 0
        assert _flag_written(_last_update(updates))

    def test_failing_audit_does_not_touch_a_tainted_no(self):
        """An infra-fallback ``No`` restores and leaves the flag untouched."""
        updates = []
        runner = _make_runner(updates)
        with (
            mock.patch.object(
                audit_runner, "_assemble_issue_report",
                return_value=_NO_REPORT,
            ),
            mock.patch.object(
                audit_runner, "_evidence_has_infra_failure_markers",
                return_value=True,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
                batch_drain=False,
            )
        assert rc == 0
        assert "--needs-producer-review" not in _last_update(updates)


# ---------------------------------------------------------------------------
# Terminal lifecycle unit tests (AC3 merge-gate parity, Q2 cascade exception)
# ---------------------------------------------------------------------------

def _make_ctx(*, merge_gate_blocker="", original_stage="in_review",
              runner=None, ship_triggered=False):
    """Build a minimal ``_AuditContext`` for lifecycle unit tests.

    ``ship_triggered`` mirrors the resolved ``--ship-triggered`` CLI flag
    (SA-0MW92TODX00064XQ): the field does not exist on the dataclass yet
    (the implementing child adds it), so it is set as an instance attribute
    — the same shape the implementation child assigns from the parser.
    """
    if runner is None:
        runner = lambda cmd: SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )
    ctx = audit_runner._AuditContext(
        issue_id="TEST-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local", runner=runner,
        json_mode=False, debug_log=None, force=True, worklog_dir=None,
        batch_phase2=False, green_run=None, audit_children=False,
        max_child_audits=None, run_tests=False,
        original_status="completed", original_stage=original_stage,
        merge_gate_blocker=merge_gate_blocker,
    )
    ctx.ship_triggered = ship_triggered
    return ctx


def _decision_ctx(*, wi=None, prior_audit=None, ship_triggered=False):
    """Minimal ``_AuditContext`` for direct decision-predicate tests.

    Exercises ``_resolve_producer_review_decision()`` hermetically (AC6):
    no live ``wl``/audit-runner calls. ``wi`` defaults to a top-level item
    (no ``parentId``); ``ship_triggered`` defaults to unset so a marker-less
    run is the default shape, exactly as today.
    """
    ctx = audit_runner._AuditContext(
        issue_id="TEST-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local",
        runner=lambda cmd: SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        ),
        json_mode=False, debug_log=None, force=True, worklog_dir=None,
        batch_phase2=False, green_run=None, audit_children=False,
        max_child_audits=None, run_tests=False,
    )
    ctx.wi = dict(_ITEM) if wi is None else wi
    ctx.prior_audit = prior_audit
    ctx.ship_triggered = ship_triggered
    return ctx


def _lifecycle_runner(calls):
    def _side_effect(cmd):
        calls.append(list(cmd))
        cmd_str = " ".join(cmd)
        if "show" in cmd_str and "audit-show" not in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "status": "completed",
                        "stage": "in_review",
                    },
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    return stateful_wl_side_effect(_side_effect)


class TestProducerReviewTerminalLifecycle:
    """The terminal write follows the resolved decision in every branch."""

    def _apply(self, ctx):
        ctx.audit_verdict = "no"
        ctx.audit_completed = True
        ctx.runner = _lifecycle_runner(ctx._calls)
        audit_runner._apply_terminal_lifecycle(ctx)
        updates = [c for c in ctx._calls if "update" in " ".join(c)]
        assert updates, "expected a lifecycle wl update"
        return updates[-1]

    def test_merge_gate_blocker_fail_flags(self):
        """AC3: a merge-gate-blocked ``No`` still flags (restored contract)."""
        ctx = _make_ctx(merge_gate_blocker="integration failed")
        ctx._calls = []
        cmd = self._apply(ctx)
        assert _flag_written(cmd)

    def test_merge_gate_blocker_cascade_suppressed_does_not_flag(self):
        """AC3/Q2: a cascade-suppressed ``No`` does not flag even with a
        merge-gate blocker — the flag follows the suppressed demotion."""
        ctx = _make_ctx(
            merge_gate_blocker="integration failed", original_stage="in_review",
        )
        ctx._calls = []
        with mock.patch.dict(
            os.environ, {audit_runner.AUDIT_CASCADE_AUDIT_ENV: "1"}, clear=False,
        ):
            cmd = self._apply(ctx)
        assert "--needs-producer-review" not in cmd


# ---------------------------------------------------------------------------
# Persisted-report metadata line (AC4)
# ---------------------------------------------------------------------------

class TestProducerReviewReportMetadata:
    """The decision is durable: it is written into the persisted report."""

    def test_report_records_preserved_decision(self):
        """AC4: a preserved review is recapped in the stored report."""
        updates = []
        captured = {}

        def _fake_persist(issue_id, report_text, **_kwargs):
            captured["report"] = report_text
            return 0

        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_fake_persist,
        ):
            rc = _run_issue(
                updates, _YES_REPORT, persist=True,
                runner_kwargs={
                    "prior_audit_record": "reviewed",
                    "needs_producer_review": False,
                },
            )

        assert rc == 0
        report = captured["report"]
        assert audit_runner.PRODUCER_REVIEW_REPORT_PREFIX in report
        assert "preserved" in report
        assert _FINGERPRINT in report
        assert not _flag_written(_last_update(updates))

    def test_report_records_required_decision(self):
        """AC4: a required review is recapped in the stored report."""
        updates = []
        captured = {}

        def _fake_persist(issue_id, report_text, **_kwargs):
            captured["report"] = report_text
            return 0

        with mock.patch.object(
            audit_runner, "persist_audit", side_effect=_fake_persist,
        ):
            rc = _run_issue(
                updates, _YES_REPORT, persist=True,
                runner_kwargs={
                    "prior_audit_record": "none",
                    "post_persist_audit": True,
                },
            )

        assert rc == 0
        assert "Producer review: required" in captured["report"]
        assert _flag_written(_last_update(updates))

    def test_dry_run_skips_the_report_line(self):
        """``--do-not-persist`` stores no report, so it writes no line."""
        decision = {
            "action": audit_runner.PRODUCER_REVIEW_PRESERVED,
            "reason": "preserved",
            "fingerprint": _FINGERPRINT,
        }
        line = audit_runner._format_producer_review_line(decision)
        assert line is not None
        # Idempotent: appending twice never duplicates the metadata line.
        once = audit_runner._append_producer_review_metadata(
            _YES_REPORT, decision,
        )
        twice = audit_runner._append_producer_review_metadata(once, decision)
        assert twice == once
        assert once.count(audit_runner.PRODUCER_REVIEW_REPORT_PREFIX) == 1


# ---------------------------------------------------------------------------
# Ship-triggered runs (SA-0MW92TODX00064XQ AC1-AC4)
# ---------------------------------------------------------------------------
#
# A ``--ship-triggered`` audit is a release-automation verification run, NOT
# an independent review: it must never write ``--needs-producer-review`` for
# either a top-level 'Yes' or a top-level 'No' verdict (the flag is left
# untouched, neither set nor cleared; ``SA-0MW92TODX00064XQ`` AC1/AC2/AC3).
# Everything the ACs pin here is exercised against PRODUCTION code — the
# decision predicate ``_resolve_producer_review_decision()`` with a
# controlled ``_AuditContext`` (AC6 hermetic: no live ``wl``/audit-runner
# calls, no re-implemented truth tables) and the terminal lifecycle's flag
# write decision.
#
# Red-first convention: the implementing child SA-0MWJ7K962006646G adds the
# flag to the ``issue`` parser and wires ``ctx.ship_triggered`` into
# ``_resolve_producer_review_decision()``. Until it lands, the planned
# ship-triggered assertions are guarded with an imperative ``pytest.xfail``
# naming that child — the marker fires only while the suppression is absent
# and disappears on its own once the implementation lands (established
# red-first convention, cf. test_persist_audit_resilience.py). The
# marker-absence assertions below are ORDINARY tests: they pin that an
# unmarked run behaves exactly as today and that the marker cannot leak.

_SHIP_TRIGGERED_REASON = "ship-triggered audit - flag untouched"
_IMPLEMENT_SHIP_TRIGGERED = "SA-0MWJ7K962006646G"


class TestShipTriggeredReviewDecision:
    """AC1/AC2/AC3: the marker suppresses only the flag write, at the
    decision predicate (hermetic — controlled ``_AuditContext`` objects)."""

    def test_ship_triggered_yes_never_reviewed_untouched(self):
        """AC2: a ship-triggered top-level 'Yes' never requires a producer
        decision — the flag is untouched instead of armed."""
        ctx = _decision_ctx(ship_triggered=True)
        decision = audit_runner._resolve_producer_review_decision(ctx, "yes")
        if decision["action"] != audit_runner.PRODUCER_REVIEW_UNTOUCHED:
            pytest.xfail(
                "--ship-triggered suppression not implemented "
                f"({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED
        assert decision["reason"] == _SHIP_TRIGGERED_REASON

    def test_ship_triggered_yes_reviewed_unchanged_untouched(self):
        """AC1/AC2: even a would-be-preserved review is untouched under the
        marker — the ship run is a verification, not a re-review."""
        prior = {
            "readyToClose": True,
            "fingerprint": _FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        ctx = _decision_ctx(prior_audit=prior, ship_triggered=True)
        decision = audit_runner._resolve_producer_review_decision(ctx, "yes")
        if decision["action"] != audit_runner.PRODUCER_REVIEW_UNTOUCHED:
            pytest.xfail(
                "--ship-triggered suppression not implemented "
                f"({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED
        assert decision["reason"] == _SHIP_TRIGGERED_REASON

    def test_ship_triggered_no_top_level_untouched(self):
        """AC3: a ship-triggered independent top-level 'No' also leaves the
        flag untouched — only the write is suppressed, never the demotion."""
        ctx = _decision_ctx(ship_triggered=True)
        decision = audit_runner._resolve_producer_review_decision(ctx, "no")
        if decision["action"] != audit_runner.PRODUCER_REVIEW_UNTOUCHED:
            pytest.xfail(
                "--ship-triggered suppression not implemented "
                f"({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_UNTOUCHED
        assert decision["reason"] == _SHIP_TRIGGERED_REASON

    def test_marker_absence_yes_never_reviewed_requires(self):
        """AC1/AC4: WITHOUT the marker a top-level 'Yes' behaves exactly as
        today — a never-reviewed pass still arms the producer queue."""
        ctx = _decision_ctx(ship_triggered=False)
        decision = audit_runner._resolve_producer_review_decision(ctx, "yes")
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED

    def test_marker_absence_no_flags(self):
        """AC3/AC4: an unmarked independent top-level 'No' still flags."""
        ctx = _decision_ctx(ship_triggered=False)
        decision = audit_runner._resolve_producer_review_decision(ctx, "no")
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_REQUIRED

    def test_marker_absence_preserved_unchanged(self):
        """AC4: unmarked reviewed-unchanged 'Yes' still preserves the
        review (SA-0MVR9B1UH002BUST carve-out is untouched by the marker)."""
        prior = {
            "readyToClose": True,
            "fingerprint": _FINGERPRINT,
            "currentFingerprint": _FINGERPRINT,
        }
        ctx = _decision_ctx(prior_audit=prior, ship_triggered=False)
        decision = audit_runner._resolve_producer_review_decision(ctx, "yes")
        assert decision["action"] == audit_runner.PRODUCER_REVIEW_PRESERVED

    def test_marker_cannot_leak_into_unmarked_runs(self):
        """AC1: a run without the marker never suppresses — the ship-triggered
        reason is unreachable without the flag, for either verdict."""
        for verdict in ("yes", "no"):
            ctx = _decision_ctx(ship_triggered=False)
            decision = audit_runner._resolve_producer_review_decision(
                ctx, verdict,
            )
            assert decision["reason"] != _SHIP_TRIGGERED_REASON


class TestShipTriggeredReportMetadata:
    """AC2: the persisted report records the untouched decision with the
    ship-triggered reason verbatim."""

    def test_report_line_records_ship_triggered_untouched(self):
        decision = {
            "action": audit_runner.PRODUCER_REVIEW_UNTOUCHED,
            "reason": _SHIP_TRIGGERED_REASON,
            "fingerprint": None,
        }
        line = audit_runner._format_producer_review_line(decision)
        assert line == (
            f"{audit_runner.PRODUCER_REVIEW_REPORT_PREFIX}untouched "
            f"({_SHIP_TRIGGERED_REASON})"
        )
        report = audit_runner._append_producer_review_metadata(
            _YES_REPORT, decision,
        )
        assert _SHIP_TRIGGERED_REASON in report
        assert audit_runner.PRODUCER_REVIEW_REPORT_PREFIX in report


class TestShipTriggeredTerminalLifecycle:
    """AC1/AC3: the terminal write follows the ship-triggered decision —
    'Yes' still advances, 'No' still demotes, and neither writes the flag."""

    def _apply(self, ctx, verdict):
        ctx.audit_verdict = verdict
        ctx.audit_completed = True
        ctx.runner = _lifecycle_runner(ctx._calls)
        audit_runner._apply_terminal_lifecycle(ctx)
        updates = [c for c in ctx._calls if "update" in " ".join(c)]
        assert updates, "expected a lifecycle wl update"
        return updates[-1]

    def test_ship_triggered_yes_advances_without_flag(self):
        """AC1: ship-triggered 'Yes' still advances to completed/in_review
        but never writes --needs-producer-review."""
        ctx = _make_ctx(ship_triggered=True)
        ctx._calls = []
        cmd = self._apply(ctx, "yes")
        if _flag_written(cmd):
            pytest.xfail(
                "--ship-triggered suppression not implemented "
                f"({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert not _flag_written(cmd)
        assert cmd[cmd.index("--status") + 1] == "completed"
        assert cmd[cmd.index("--stage") + 1] == "in_review"

    def test_ship_triggered_no_demotes_without_flag(self):
        """AC3: ship-triggered 'No' still demotes to open/plan_complete but
        never writes --needs-producer-review."""
        ctx = _make_ctx(ship_triggered=True)
        ctx._calls = []
        cmd = self._apply(ctx, "no")
        if _flag_written(cmd):
            pytest.xfail(
                "--ship-triggered suppression not implemented "
                f"({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert not _flag_written(cmd)
        assert cmd[cmd.index("--status") + 1] == "open"
        assert cmd[cmd.index("--stage") + 1] == "plan_complete"

    def test_marker_absence_yes_advance_still_flags(self):
        """AC1/AC4: an unmarked never-reviewed 'Yes' still arms the producer
        queue at the write site — the marker never leaks into unmarked runs."""
        ctx = _make_ctx(ship_triggered=False)
        ctx._calls = []
        cmd = self._apply(ctx, "yes")
        assert _flag_written(cmd)

    def test_marker_absence_no_demotes_and_flags(self):
        """AC3/AC4: an unmarked independent 'No' still demotes AND flags."""
        ctx = _make_ctx(ship_triggered=False)
        ctx._calls = []
        cmd = self._apply(ctx, "no")
        assert cmd[cmd.index("--status") + 1] == "open"
        assert cmd[cmd.index("--stage") + 1] == "plan_complete"
        assert _flag_written(cmd)


class TestShipTriggeredParser:
    """AC5: the marker is parsed as an ``issue``-subcommand boolean and must
    never be added to the ``batch`` or ``check-freshness`` subparsers."""

    def test_ship_triggered_flag_parses_on_issue(self):
        """AC5: ``--ship-triggered`` is accepted by the issue subparser."""
        try:
            args = audit_runner.build_parser().parse_args(
                ["issue", "TEST-1", "--ship-triggered"]
            )
        except SystemExit:
            pytest.xfail(
                "--ship-triggered flag not yet accepted on the issue "
                f"subparser ({_IMPLEMENT_SHIP_TRIGGERED})"
            )
        assert args.command == "issue"
        assert args.issue_id == "TEST-1"
        assert args.ship_triggered is True

    def test_ship_triggered_flag_rejected_on_batch(self):
        """AC5: the marker is NOT added to the batch subparser — a
        ship-triggered batch drain must fail loudly, never silently treat
        drained items as ship-verification runs."""
        with pytest.raises(SystemExit):
            audit_runner.build_parser().parse_args(
                ["batch", "--ship-triggered"]
            )

    def test_ship_triggered_flag_rejected_on_check_freshness(self):
        """AC5: the marker is NOT added to the check-freshness subparser —
        the read-only probe stays marker-free."""
        with pytest.raises(SystemExit):
            audit_runner.build_parser().parse_args(
                ["check-freshness", "TEST-1", "--ship-triggered"]
            )
