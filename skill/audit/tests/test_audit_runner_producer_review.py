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
        cmd = _last_update(updates)
        assert cmd == [
            "wl", "update", "TEST-1",
            "--status", "completed", "--stage", "in_review",
            "--assignee", "", "--json",
        ]

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
              runner=None):
    """Build a minimal ``_AuditContext`` for lifecycle unit tests."""
    if runner is None:
        runner = lambda cmd: SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )
    return audit_runner._AuditContext(
        issue_id="TEST-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local", runner=runner,
        json_mode=False, debug_log=None, force=True, worklog_dir=None,
        batch_phase2=False, green_run=None, audit_children=False,
        max_child_audits=None, run_tests=False,
        original_status="completed", original_stage=original_stage,
        merge_gate_blocker=merge_gate_blocker,
    )


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
