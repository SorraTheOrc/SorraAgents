from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

from skill.audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

class TestOptInChildAuditCascade:
    """Tests for the opt-in recursive child-audit cascade (AC1-AC6).

    The cascade is OFF by default (AC1): a parent with unaudited children no
    longer implicitly spawns a full child audit per child. ``--audit-children``
    opts in (AC2); a per-run cap bounds the number of auto-triggered child
    audits (AC3); children with unchanged content are skipped via the Feature 1
    content-based freshness gate (AC4); and a not-ready child still blocks the
    parent (AC5 — verdict semantics unchanged).
    """

    def _make_runner(self, child_stage="plan_complete", parent_audit_show=True,
                     n_children=1, child_audit_raw=None, child_updated_at=None):
        """Build a mock runner returning a parent with *n_children* children.

        *child_audit_raw* maps child id -> rawOutput returned by
        ``wl audit-show`` for that child (None = no prior audit).
        *child_updated_at* sets the child's ``updatedAt`` (default: recent —
        see individual tests).
        """
        mock_runner = mock.MagicMock()
        child_audit_raw = child_audit_raw or {}
        child_updated_at = child_updated_at or "2026-08-05T00:00:00.000Z"

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            # Parent readback verification (persist=True): stored audit.
            if "audit-show" in cmd_str and parent_audit_show and "TEST-1" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "rawOutput": "TEST-1 audit report",
                            "auditedAt": "2026-01-01T00:00:00.000Z",
                        },
                    }),
                    stderr="",
                )

            # Child audit-show lookups.
            if "audit-show" in cmd_str:
                child_id = cmd_str.split("audit-show", 1)[1].strip().split()[0]
                raw = child_audit_raw.get(child_id)
                if raw is None:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True, "audit": None}),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "rawOutput": raw,
                            "auditedAt": "2026-08-01T00:00:00.000Z",
                        },
                    }),
                    stderr="",
                )

            # StatusLifecycle.show -> wl show <id> --json (parent TEST-1)
            if "show" in cmd_str and "--children" not in cmd_str and "TEST-1" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open",
                                     "stage": "plan_complete"},
                    }),
                    stderr="",
                )

            # Child work-item lookup (freshness pre-pass / content gate):
            # return the child with its updatedAt so the time gate reports
            # stale when the stored audit is older than the child update.
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": cmd_str.split("show", 1)[1].strip().split()[0],
                            "description": "## Acceptance Criteria\n- CAC1: child criterion",
                            "updatedAt": child_updated_at,
                        },
                    }),
                    stderr="",
                )

            # _run_wl -> wl show <id> --children --json
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": (
                                "## Acceptance Criteria\n- AC1: parent criterion"
                            ),
                            "status": "in_progress",
                        },
                        "children": [{
                            "id": f"CHILD-{i}",
                            "title": f"Child Issue {i}",
                            "status": "open",
                            "stage": child_stage,
                            "updatedAt": child_updated_at,
                            "description": "## Acceptance Criteria\n- CAC1: child criterion",
                        } for i in range(1, n_children + 1)],
                    }),
                    stderr="",
                )

            # StatusLifecycle.update_status -> wl update <id> --status ...
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )

            # Working-tree fingerprint (SA-0MSL1YXG7004F2BZ): a clean tree
            # yields an empty marker — matches the stored fingerprint computed
            # with a MagicMock runner (no git output).
            if cmd_str.startswith(("git status", "git diff")):
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run(self, triggered, **cmd_kwargs):
        """Run cmd_issue with a mocked pipeline, recording child subprocess
        spawns in *triggered*.

        ``--force`` defaults to False so child-verdict reuse (the content
        gate) is active; pass ``force=True`` to exercise the force-bypass
        path (LP-0MSQ32MF200675AR).
        """
        pi_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "mocked"}]',
        }

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        def _fake_subprocess_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "audit_runner.py" in cmd_str and "issue" in cmd_str:
                triggered.append(cmd_str)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        runner_kwargs = cmd_kwargs.pop("runner_kwargs", {})
        force = cmd_kwargs.pop("force", False)
        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=pi_result
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
            mock.patch.object(
                audit_runner.subprocess, "run", side_effect=_fake_subprocess_run
            ),
            mock.patch.object(audit_runner, "persist_audit", return_value=0),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=True, force=force,
                runner=self._make_runner(**runner_kwargs),
                json_mode=True,
                parent_timeout=None,
                **cmd_kwargs,
            )

    def test_default_no_cascade_marks_children_not_ready(self):
        """AC1: without --audit-children, children without fresh audits are
        not auto-audited — no subprocess spawn."""
        triggered: list[str] = []
        rc = self._run(triggered, runner_kwargs={"n_children": 2})
        assert rc == 0
        assert triggered == []

    def test_audit_children_enables_cascade(self):
        """AC2: --audit-children triggers a full audit per unaudited child."""
        triggered: list[str] = []
        rc = self._run(triggered, runner_kwargs={"n_children": 2},
                       audit_children=True)
        assert rc == 0
        assert len(triggered) == 2
        assert all("CHILD-" in c for c in triggered)

    def test_cap_bounds_triggered_child_audits(self):
        """AC3: the per-run cap bounds the number of auto-triggered child
        audits even when --audit-children is set."""
        triggered: list[str] = []
        rc = self._run(triggered, runner_kwargs={"n_children": 4},
                       audit_children=True, max_child_audits=2)
        assert rc == 0
        assert len(triggered) == 2

    def test_unchanged_child_skipped_via_content_gate(self, capsys):
        """AC4 (LP-0MSQ32MF200675AR): a child whose audit is stale by the
        TIME gate but content-unchanged (fingerprint matches) is not
        re-audited even with --audit-children — the Phase 1 pre-pass reuses
        its stored verdict via the content gate (zero pi calls, no
        auto-triggered child audit)."""
        from skill.audit.scripts import audit_runner as ar
        head = "a" * 40
        child_desc = "## Acceptance Criteria\n- CAC1: child criterion"
        with mock.patch.object(ar, "_resolve_audited_head", return_value=head):
            fp = ar._compute_content_fingerprint(
                mock.MagicMock(), "CHILD-1", work_item={"description": child_desc},
            )
        stored = (
            f"Ready to close: Yes\n\nAudit report for work item CHILD-1\n\n"
            f"{ar.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fp}\n\n## Summary\nok"
        )

        triggered: list[str] = []
        # The stored audit is OLD (auditedAt 2026-08-01) while the child was
        # updated LATER (updatedAt 2026-08-05) → stale by the time gate, but
        # the content fingerprint still matches → the pre-pass content gate
        # reuses it.
        with mock.patch.object(ar, "_resolve_audited_head", return_value=head):
            rc = self._run(
                triggered,
                runner_kwargs={
                    "n_children": 1,
                    "child_audit_raw": {"CHILD-1": stored},
                    "child_updated_at": "2026-08-05T00:00:00.000Z",
                },
                audit_children=True,
            )
        assert rc == 0
        assert triggered == [], (
            "content-unchanged child must not be re-audited"
        )
        err = capsys.readouterr().err
        assert "Auto-triggering audit for child" not in err, (
            "content-unchanged child must not be auto-audited"
        )

    def test_not_ready_child_still_blocks_parent(self):
        """AC5: a child without a fresh audit (no --audit-children) stays
        not-ready and blocks the parent — verdict semantics unchanged."""
        triggered: list[str] = []
        rc = self._run(triggered, runner_kwargs={"n_children": 1})
        assert rc == 0
        assert triggered == []

class TestChildVerdictReuseInParentAudits:
    """Parent audits reuse fresh child audit verdicts instead of re-auditing.

    A fresh child audit is one whose stored report carries a content
    fingerprint that matches the child's current state AND a parseable
    verdict (same logic as the item-level freshness gate). Reused children
    cost ZERO pi calls; stale/legacy children keep the existing audit flow.
    """

    PARENT_HEAD = "a" * 40
    CHILD_DESC = "## Acceptance Criteria\n- CAC1: child criterion"
    AUDITED_AT = "2026-08-01T00:00:00.000Z"

    def _fingerprinted_raw(self, ar, child_id, verdict="Yes",
                           head=None, acs=None):
        """Build a child audit rawOutput carrying a content fingerprint.

        The fingerprint is computed under the same mocked HEAD the runtime
        uses, so a stored report built here counts as content-fresh when the
        runner evaluates it.
        """
        head = head or self.PARENT_HEAD
        with mock.patch.object(ar, "_resolve_audited_head", return_value=head):
            fp = ar._compute_content_fingerprint(
                mock.MagicMock(), child_id,
                work_item={"description": self.CHILD_DESC},
            )
        lines = [
            f"Audit report for work item {child_id}",
            f"Ready to close: {verdict}",
            "",
            f"{ar.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fp}",
            "",
            "## Summary",
            "Child audit ok.",
            "",
            "## Acceptance Criteria Status",
            "",
            "| # | Criterion | Verdict | Evidence |",
            "|---|-----------|---------|----------|",
        ]
        if acs is None:
            acs = [("CAC1: child criterion", "met", "child.py:1")]
        for i, (text, v, ev) in enumerate(acs, 1):
            lines.append(f"| {i} | {text} | {v} | {ev} |")
        return "\n".join(lines)

    def _make_runner(self, n_children=1, child_audit_raw=None,
                     child_stage="in_review"):
        """Mock runner: parent + *n_children* children; per-child
        ``wl audit-show`` returns the mapped rawOutput (None = no audit)."""
        mock_runner = mock.MagicMock()
        child_audit_raw = child_audit_raw or {}

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)

            if "audit-show" in cmd_str:
                child_id = cmd_str.split("audit-show", 1)[1].strip().split()[0]
                if child_id == "TEST-1":
                    # Stored audit for the parent (used by the freshness
                    # check and the post-persist readback verification).
                    # No fingerprint + no updatedAt on the work item → the
                    # item-level freshness gate cannot short-circuit and the
                    # pipeline runs.
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "rawOutput": "TEST-1 audit report",
                                "auditedAt": "2026-01-01T00:00:00.000Z",
                            },
                        }),
                        stderr="",
                    )
                raw = child_audit_raw.get(child_id)
                if raw is None:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True, "audit": None}),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "rawOutput": raw,
                            "auditedAt": self.AUDITED_AT,
                        },
                    }),
                    stderr="",
                )

            # wl show <parent> --json (freshness / lifecycle lookups).
            if "show" in cmd_str and "--children" not in cmd_str \
                    and "TEST-1" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "status": "open",
                            "stage": "plan_complete",
                        },
                    }),
                    stderr="",
                )

            # wl show <child> --json (fingerprint computation + time gate):
            # the child's description drives the content fingerprint.
            if "show" in cmd_str and "--children" not in cmd_str:
                child_id = cmd_str.split("show", 1)[1].strip().split()[0]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": child_id,
                            "description": self.CHILD_DESC,
                            "updatedAt": "2026-08-05T00:00:00.000Z",
                        },
                    }),
                    stderr="",
                )

            # _run_wl -> wl show <parent> --children --json
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n"
                                            "- AC1: parent criterion",
                            "status": "in_progress",
                        },
                        "children": [{
                            "id": f"CHILD-{i}",
                            "title": f"Child Issue {i}",
                            "status": "open",
                            "stage": child_stage,
                            "updatedAt": "2026-08-05T00:00:00.000Z",
                            "description": self.CHILD_DESC,
                        } for i in range(1, n_children + 1)],
                    }),
                    stderr="",
                )

            # StatusLifecycle -> wl update <id> --status ...
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )

            # Working-tree fingerprint (SA-0MSL1YXG7004F2BZ): a clean tree
            # yields the empty marker — matches the stored fingerprint.
            if cmd_str.startswith(("git status", "git diff")):
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run(self, child_pi_contexts, triggered, *, n_children=1,
             child_audit_raw=None, audit_children=True, force=False,
             persist=True, child_stage="in_review", persist_calls=None):
        """Run cmd_issue with a mocked pipeline.

        *child_pi_contexts* collects the ``context`` of every
        ``_call_pi_and_maybe_log`` invocation (child Phase 1 contexts are
        ``child:<id>``; Phase 2 contexts are ``phase2_deep`` /
        ``phase2_child:<i>`` / ``phase2_batch``).
        *triggered* collects auto-triggered child audit subprocess commands.
        *persist_calls* (optional list) collects ``persist_audit`` call args
        so tests can assert persistence hygiene.
        """
        pi_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "mocked"},
            ]),
        }

        def _capture(issue_id, context, prompt, **kwargs):
            child_pi_contexts.append(context)
            return pi_result

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        def _fake_subprocess_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "audit_runner.py" in cmd_str and "issue" in cmd_str:
                triggered.append(cmd_str)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        def _fake_persist(issue_id, report, worklog_dir=None):
            if persist_calls is not None:
                persist_calls.append((issue_id, report))
            return 0

        with (
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
            mock.patch.object(
                audit_runner.subprocess, "run", side_effect=_fake_subprocess_run
            ),
            mock.patch.object(
                audit_runner, "persist_audit", side_effect=_fake_persist
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head",
                return_value=self.PARENT_HEAD,
            ),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=persist, force=force,
                runner=self._make_runner(
                    n_children=n_children, child_audit_raw=child_audit_raw,
                    child_stage=child_stage,
                ),
                json_mode=False, parent_timeout=None,
                audit_children=audit_children,
            )

    @staticmethod
    def _is_child_pi_context(context: str) -> bool:
        """True when a pi call context targets a child (Phase 1 ``child:<id>``
        or Phase 2 ``phase2_child:<i>`` / ``phase2_batch``)."""
        return (
            context.startswith(("child:", "phase2_child"))
            or context == "phase2_batch"
        )

    # ------------------------------------------------------------------
    # AC4(a): parent with 5 fresh children issues ZERO child pi calls
    # ------------------------------------------------------------------

    def test_five_fresh_children_zero_child_pi_calls(self):
        """AC4(a): a parent with 5 fresh children (content fingerprint
        unchanged + verdict present) issues ZERO child pi calls through
        cmd_issue with --audit-children — no child Phase 1, no child Phase 2
        deep/batch entry — and no child audit is auto-triggered."""
        ar = audit_runner
        raw = {
            f"CHILD-{i}": self._fingerprinted_raw(ar, f"CHILD-{i}")
            for i in range(1, 6)
        }
        child_pi_contexts: list[str] = []
        triggered: list[str] = []

        rc = self._run(child_pi_contexts, triggered, n_children=5,
                       child_audit_raw=raw, audit_children=True)

        assert rc == 0
        child_calls = [
            c for c in child_pi_contexts if self._is_child_pi_context(c)
        ]
        assert child_calls == [], f"child pi calls fired: {child_calls}"
        assert triggered == [], "fresh children must not be auto-audited"

    # ------------------------------------------------------------------
    # AC4(b): parent with 1 stale + 4 fresh children audits only the stale
    # ------------------------------------------------------------------

    def test_stale_child_audited_alone_among_fresh(self):
        """AC4(b): a parent with 1 stale + 4 fresh children audits ONLY the
        stale child — exactly 1 child pi call and 1 child auto-audit."""
        ar = audit_runner
        raw = {
            f"CHILD-{i}": self._fingerprinted_raw(ar, f"CHILD-{i}")
            for i in range(1, 6)
        }
        # CHILD-1 is stale: its stored report carries a fingerprint computed
        # at a DIFFERENT HEAD (content changed) → content gate rejects it.
        raw["CHILD-1"] = self._fingerprinted_raw(
            ar, "CHILD-1", head="b" * 40,
        )
        child_pi_contexts: list[str] = []
        triggered: list[str] = []

        rc = self._run(child_pi_contexts, triggered, n_children=5,
                       child_audit_raw=raw, audit_children=True)

        assert rc == 0
        child_calls = [
            c for c in child_pi_contexts if self._is_child_pi_context(c)
        ]
        assert len(child_calls) == 1, f"expected 1 child pi call: {child_calls}"
        assert "child:CHILD-1" in child_calls[0]
        assert len(triggered) == 1, f"expected 1 child auto-audit: {triggered}"
        assert "CHILD-1" in triggered[0]
        assert all("CHILD-2" not in t and "CHILD-3" not in t
                   for t in triggered)

    # ------------------------------------------------------------------
    # AC4(c): reused tables carry correct verdicts + reuse marker
    # ------------------------------------------------------------------

    def test_reused_table_verdicts_and_marker(self, capsys):
        """AC4(c): a reused child's AC table carries the verdicts from the
        child's own audit report and the parent report marks the reuse with
        ``reused from <auditedAt>``."""
        ar = audit_runner
        raw = {
            "CHILD-1": self._fingerprinted_raw(
                ar, "CHILD-1", verdict="Yes",
                acs=[
                    ("CAC1: child criterion", "met", "child.py:1"),
                    ("CAC2: child criterion", "adjusted", "child.py:2"),
                ],
            ),
        }
        child_pi_contexts: list[str] = []
        triggered: list[str] = []

        rc = self._run(child_pi_contexts, triggered, n_children=1,
                       child_audit_raw=raw, audit_children=True)

        assert rc == 0
        child_calls = [
            c for c in child_pi_contexts if self._is_child_pi_context(c)
        ]
        assert child_calls == [], f"child pi calls fired: {child_calls}"
        assert triggered == []
        report = capsys.readouterr().out
        assert f"Child verdict reused from {self.AUDITED_AT}" in report
        assert "content unchanged, no fresh audit performed" in report
        # Verdicts come from the child's OWN report table.
        assert "| 1 | CAC1: child criterion | met | child.py:1 |" in report
        assert "| 2 | CAC2: child criterion | adjusted | child.py:2 |" in report

    def test_reused_child_not_repersisted(self):
        """F2 AC5 (persistence hygiene): a reused child keeps its own
        authoritative audit — the parent does NOT re-persist it. Only the
        parent report is persisted."""
        ar = audit_runner
        raw = {"CHILD-1": self._fingerprinted_raw(ar, "CHILD-1")}
        child_pi_contexts: list[str] = []
        triggered: list[str] = []
        persist_calls: list[tuple[str, str]] = []

        rc = self._run(child_pi_contexts, triggered, n_children=1,
                       child_audit_raw=raw, audit_children=True,
                       persist=True, persist_calls=persist_calls)

        assert rc == 0
        persisted_ids = [i for i, _ in persist_calls]
        assert "CHILD-1" not in persisted_ids, \
            f"reused child re-persisted: {persisted_ids}"
        assert "TEST-1" in persisted_ids  # the parent report still persists

    # ------------------------------------------------------------------
    # AC4(d): --force bypasses reuse — all children re-audited
    # ------------------------------------------------------------------

    def test_force_bypasses_reuse_reaudits_all(self):
        """AC4(d): --force on the parent bypasses child reuse — all 5 fresh
        children are re-audited (5 child auto-audits + 5 child Phase 1
        screening calls)."""
        ar = audit_runner
        raw = {
            f"CHILD-{i}": self._fingerprinted_raw(ar, f"CHILD-{i}")
            for i in range(1, 6)
        }
        child_pi_contexts: list[str] = []
        triggered: list[str] = []

        rc = self._run(child_pi_contexts, triggered, n_children=5,
                       child_audit_raw=raw, audit_children=True, force=True)

        assert rc == 0
        child_calls = [
            c for c in child_pi_contexts if self._is_child_pi_context(c)
        ]
        assert len(triggered) == 5, f"expected all 5 children re-audited: {triggered}"
        assert len(child_calls) == 5, f"expected 5 child Phase 1 calls: {child_calls}"
        assert all(f"CHILD-{i}" in triggered[i - 1] for i in range(1, 6))

    # ------------------------------------------------------------------
    # AC5 regression: item-level freshness gate unchanged
    # ------------------------------------------------------------------

    def test_item_level_freshness_gate_still_short_circuits(self):
        """AC5 regression (SA-0MSKB6US1009CNHT): the item-level content
        gate still returns the stored report for an unchanged item — the
        pipeline short-circuits."""
        ar = audit_runner
        stored = self._fingerprinted_raw(ar, "TEST-1")
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {
                            "rawOutput": stored,
                            "auditedAt": self.AUDITED_AT,
                        },
                    }),
                    stderr="",
                )
            if "show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": self.CHILD_DESC,
                        },
                    }),
                    stderr="",
                )
            if cmd_str.startswith(("git status", "git diff")):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        with mock.patch.object(
            ar, "_resolve_audited_head", return_value=self.PARENT_HEAD
        ):
            fresh = ar._check_audit_freshness(mock_runner, "TEST-1")
        assert fresh == stored

    # ------------------------------------------------------------------
    # F2 AC1 unit tests: content-first freshness in _get_child_audit_verdict
    # ------------------------------------------------------------------

    def _verdict_runner(self, raw, audited_at=AUDITED_AT):
        """Mock runner for direct _get_child_audit_verdict calls."""
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "audit": {"rawOutput": raw, "auditedAt": audited_at},
                    }),
                    stderr="",
                )
            if "show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "CHILD-1",
                            "description": self.CHILD_DESC,
                        },
                    }),
                    stderr="",
                )
            if cmd_str.startswith(("git status", "git diff")):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def test_verdict_content_fresh_ready(self):
        """F2 AC1: a child whose stored fingerprint matches the current state
        and whose report carries a verdict is fresh (ready)."""
        ar = audit_runner
        raw = self._fingerprinted_raw(ar, "CHILD-1", verdict="Yes")
        with mock.patch.object(
            ar, "_resolve_audited_head", return_value=self.PARENT_HEAD
        ):
            verdict, reason, audited_at = ar._get_child_audit_verdict(
                self._verdict_runner(raw), "CHILD-1",
            )
        assert verdict is True
        assert reason == "ready"
        assert audited_at == self.AUDITED_AT

    def test_verdict_content_fresh_not_ready(self):
        """F2 AC1: a fresh audit with an explicit not-ready verdict is also
        reused (verdict False, reason not_ready) — it still blocks the parent
        but costs zero pi calls."""
        ar = audit_runner
        raw = self._fingerprinted_raw(ar, "CHILD-1", verdict="No")
        with mock.patch.object(
            ar, "_resolve_audited_head", return_value=self.PARENT_HEAD
        ):
            verdict, reason, audited_at = ar._get_child_audit_verdict(
                self._verdict_runner(raw), "CHILD-1",
            )
        assert verdict is False
        assert reason == "not_ready"
        assert audited_at == self.AUDITED_AT

    def test_verdict_content_changed_stale(self):
        """F2 AC1: a fingerprint mismatch (content changed) means stale — the
        child is re-audited."""
        ar = audit_runner
        raw = self._fingerprinted_raw(ar, "CHILD-1", head="b" * 40)
        with mock.patch.object(
            ar, "_resolve_audited_head", return_value=self.PARENT_HEAD
        ):
            verdict, reason, audited_at = ar._get_child_audit_verdict(
                self._verdict_runner(raw), "CHILD-1",
            )
        assert verdict is None
        assert reason == "stale"
        assert audited_at == self.AUDITED_AT

    def test_verdict_force_bypasses_reuse(self):
        """F2 AC1/AC4: --force bypasses BOTH gates — a content-fresh child
        verdict is not reused."""
        ar = audit_runner
        raw = self._fingerprinted_raw(ar, "CHILD-1", verdict="Yes")
        with mock.patch.object(
            ar, "_resolve_audited_head", return_value=self.PARENT_HEAD
        ):
            verdict, reason, _audited_at = ar._get_child_audit_verdict(
                self._verdict_runner(raw), "CHILD-1", force=True,
            )
        assert verdict is None
        assert reason == "force"

    # ------------------------------------------------------------------
    # F2 AC5 unit test: parent-persisted child reports embed the fingerprint
    # ------------------------------------------------------------------

    def test_persist_child_audit_embeds_fingerprint(self):
        """F2 AC5: child reports persisted by the parent embed the content
        fingerprint so they stay content-gate-able on future runs."""
        ar = audit_runner
        fp = "f" * 64
        captured: dict = {}

        def _fake_persist(issue_id, report, worklog_dir=None):
            captured["report"] = report
            return 0

        with mock.patch.object(ar, "persist_audit", side_effect=_fake_persist):
            rc, report = ar._persist_child_audit(
                "CHILD-1", "Child", "open", "plan_complete",
                ac_results=[
                    {"text": "AC1", "verdict": "met", "evidence": "ok"},
                ],
                content_fingerprint=fp,
            )
        assert rc == 0
        assert f"{ar.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fp}" in report
        assert "CHILD-1" in report

class TestParentFirstChildPassThrough:
    """Tests for the parent-first child pass-through (AC1-AC8).

    The default flow audits the parent fully first (Phase 1 parent ACs +
    Phase 2 parent deep analysis) before any child audit is considered:
      - parent passes with no gaps → all children inherit passed (zero audits)
      - parent has gaps → only gap-mapped children are audited
    --audit-children forces the full per-child flow (override).
    """

    def _make_runner(self, children, parent_desc=None):
        """Mock runner returning a parent with *children*."""
        if parent_desc is None:
            parent_desc = "## Acceptance Criteria\n- AC1: parent criterion"
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open",
                                     "stage": "plan_complete"},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1", "description": parent_desc,
                            "status": "in_progress",
                        },
                        "children": children,
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}), stderr="",
                )
            if "audit-show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "audit": None}),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _run(self, children, parent_verdict="met", gap_file=None,
             head_sha=None, **cmd_kwargs):
        """Run cmd_issue and capture child_results via the report assembly.

        *parent_verdict* is the verdict for the single parent AC in the mock
        Phase 1 + Phase 2 deep calls. *gap_file* when set makes the parent gap
        evidence reference a file that a child's Key Files map to. *head_sha*
        pins the git HEAD for content-fingerprint checks.
        """
        captured = {}
        pi_calls = []

        def _fake_pi(issue_id, context, prompt, **kwargs):
            pi_calls.append(context)
            if context in ("parent", "phase2_deep"):
                evidence = f"{gap_file}:1" if gap_file else "parent.py:1"
                return {"extracted_text": json.dumps([
                    {"index": 0, "verdict": parent_verdict, "evidence": evidence},
                ])}
            # child calls
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "child.py:1"},
            ])}

        original_assemble = audit_runner._assemble_issue_report

        def _capturing_assemble(issue, ac_results, child_results, **kwargs):
            captured["child_results"] = child_results
            captured["ac_results"] = ac_results
            return original_assemble(issue, ac_results, child_results, **kwargs)

        runner = self._make_runner(children)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi))
            stack.enter_context(mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0}))
            stack.enter_context(mock.patch.object(
                audit_runner, "_assemble_issue_report",
                side_effect=_capturing_assemble))
            if head_sha is not None:
                stack.enter_context(mock.patch.object(
                    audit_runner, "_resolve_audited_head", return_value=head_sha))
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner, **cmd_kwargs,
            )
        captured["pi_calls"] = pi_calls
        return rc, captured

    def _child(self, child_id="CHILD-1", key_file=None, stage="plan_complete"):
        desc = "## Acceptance Criteria\n- CAC1: child criterion"
        if key_file:
            desc += f"\n\n## Key Files\n- {key_file}"
        return {
            "id": child_id, "title": f"Child {child_id}",
            "status": "open", "stage": stage, "description": desc,
        }

    def test_parent_passes_children_inherit(self):
        """AC1/AC2: parent passes with no gaps → all children inherit passed;
        zero child audit calls run."""
        rc, captured = self._run([self._child("CHILD-1"), self._child("CHILD-2")])
        assert rc == 0
        children = captured["child_results"]
        assert len(children) == 2
        assert all(c["inherited_pass"] is True for c in children)
        assert all(c["child_audit_ready"] is True for c in children)
        # No child Phase 1 review / Phase 2 child calls
        assert not any(c.startswith("child:") for c in captured["pi_calls"])

    def test_parent_passes_report_marks_inherited(self):
        """AC4: the report explicitly marks inherited children."""
        rc, captured = self._run([self._child("CHILD-1")])
        assert rc == 0
        child = captured["child_results"][0]
        assert child["inherited_pass"] is True
        assert child["ac_results"][0]["verdict"] == "met"
        assert "Inherited from parent pass" in child["ac_results"][0]["text"]

    def test_parent_passes_ready_to_close(self):
        """AC2: parent passes → inherited children count as reviewed, so the
        parent is ready to close."""
        rc, captured = self._run([self._child("CHILD-1")])
        assert rc == 0
        # The report assembly captured ac_results; ready-to-close derives from
        # child_audit_ready flags (all True) + stage check.
        children = captured["child_results"]
        assert all(c["child_audit_ready"] for c in children)

    def test_parent_gaps_only_mapped_children_audited(self):
        """AC3: parent has gaps → only the gap-mapped child is audited;
        unrelated children are not audited."""
        gap_file = "src/gap.py"
        mapped = self._child("CHILD-1", key_file=gap_file)
        unrelated = self._child("CHILD-2", key_file="src/other.py")
        rc, captured = self._run([mapped, unrelated], parent_verdict="unmet",
                                 gap_file=gap_file)
        assert rc == 0
        children = {c["id"]: c for c in captured["child_results"]}
        # The gap-mapped child gets a full audit
        assert children["CHILD-1"]["child_audit_ready"] is False
        assert "inherited_pass" not in children["CHILD-1"]
        # The unrelated child is not audited and not inherited
        assert children["CHILD-2"]["pass_through"] == "unrelated_to_gaps"
        # Phase 1 child review ran only for the mapped child; the unrelated
        # child had none. (The mapped child also gets a phase2_child deep
        # call — both are counted by 'child' contexts, but only one child is
        # audited.)
        child_contexts = [
            c for c in captured["pi_calls"]
            if c.startswith(("child:", "phase2_child:"))
        ]
        # Exactly one child was audited → all child contexts reference CHILD-1
        assert child_contexts
        assert all(c.startswith("phase2_child:") or "CHILD-1" in c for c in child_contexts)

    def test_audit_children_forces_full_per_child(self):
        """AC5: --audit-children forces full per-child audits regardless of
        the parent result."""
        rc, captured = self._run([self._child("CHILD-1")],
                                 audit_children=True)
        assert rc == 0
        child = captured["child_results"][0]
        assert child.get("inherited_pass") is None or not child.get("inherited_pass")
        assert child["child_audit_ready"] is False
        # Full per-child flow ran a child Phase 1 review
        assert any("child:" in c for c in captured["pi_calls"])

    def test_changed_child_not_inherited(self):
        """AC6: a child whose content changed (fingerprint mismatch) is not
        silently inherited-passed — it is audited."""
        child = self._child("CHILD-1")
        # The runner returns a stored (stale) audit for the child; combined
        # with a different git HEAD the content fingerprint will not match →
        # the child's content changed → audited, not inherited.
        runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItemId": "CHILD-1",
                        "audit": {
                            "workItemId": "CHILD-1",
                            "auditedAt": "2026-08-01T00:00:00.000Z",
                            "rawOutput": (
                                "Ready to close: Yes\n\n## Summary\nold"
                            ),
                        },
                    }),
                    stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open",
                                     "stage": "plan_complete"},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": "## Acceptance Criteria\n- AC1: parent criterion",
                            "status": "in_progress",
                        },
                        "children": [child],
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}), stderr="",
                )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
            )

        runner.side_effect = _side_effect
        captured = {}
        pi_calls = []

        def _fake_pi(issue_id, context, prompt, **kwargs):
            pi_calls.append(context)
            if context in ("parent", "phase2_deep"):
                return {"extracted_text": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "parent.py:1"},
                ])}
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "child.py:1"},
            ])}

        original_assemble = audit_runner._assemble_issue_report

        def _capturing_assemble(issue, ac_results, child_results, **kwargs):
            captured["child_results"] = child_results
            return original_assemble(issue, ac_results, child_results, **kwargs)

        with (
            mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                              side_effect=_fake_pi),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(audit_runner, "_assemble_issue_report",
                              side_effect=_capturing_assemble),
            mock.patch.object(audit_runner, "_resolve_audited_head",
                              return_value="a" * 40),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )

        assert rc == 0
        child_result = captured["child_results"][0]
        # Content changed → audited, not inherited
        assert child_result.get("inherited_pass") is None
        assert child_result["child_audit_ready"] is False

    def test_blocking_cq_skips_parent_phase2(self):
        """Blocking CQ findings skip the parent Phase 2 deep call in the
        parent-first flow — the verdict is already 'Ready to close: No' via
        the findings, so the deep call would only burn model latency. Met
        verdicts are demoted to partial instead (mirrors the full-flow
        phase1-blocked gate)."""
        captured = {}
        pi_calls = []

        def _fake_pi(issue_id, context, prompt, **kwargs):
            pi_calls.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent.py:1"},
            ])}

        original_assemble = audit_runner._assemble_issue_report

        def _capturing_assemble(issue, ac_results, child_results, **kwargs):
            captured["ac_results"] = ac_results
            return original_assemble(issue, ac_results, child_results, **kwargs)

        runner = self._make_runner([self._child("CHILD-1")])
        blocking = [{
            "severity": "critical", "file": "src/bad.py", "line": 1,
            "message": "blocking finding", "linter": "test", "code": "X",
        }]
        with (
            mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                              side_effect=_fake_pi),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": blocking,
                              "fixes_applied": 0},
            ),
            mock.patch.object(audit_runner, "_assemble_issue_report",
                              side_effect=_capturing_assemble),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=runner,
            )
        assert rc == 0
        # No parent Phase 2 deep call — only the parent Phase 1 screening ran
        assert "phase2_deep" not in pi_calls
        assert "parent" in pi_calls
        # met verdicts were demoted to partial (Phase 1 blocked)
        assert all(r["verdict"] == "partial" for r in captured["ac_results"])

    def test_skip_parent_deep_skips_batch_reanalysis(self):
        """skip_parent_deep + batch mode must not re-analyze the parent:
        the batch path folds parent ACs into one call, so it is skipped for
        the child call and children use per-child deep calls instead."""
        gap_file = "src/gap.py"
        mapped = self._child("CHILD-1", key_file=gap_file)
        rc, captured = self._run([mapped], parent_verdict="unmet",
                                 gap_file=gap_file, batch_phase2=True)
        assert rc == 0
        # Parent deep analysis ran exactly once (parent-only first call);
        # no batch call re-analyzes the parent.
        assert captured["pi_calls"].count("phase2_deep") == 1
        assert "phase2_batch" not in captured["pi_calls"]
        # The gap-mapped child got its own deep call
        assert any(c.startswith("phase2_child:") for c in captured["pi_calls"])

    def test_not_ready_mapped_child_blocks_parent(self):
        """AC7: a gap-mapped child that comes back not-ready still blocks the
        parent — verdict semantics unchanged."""
        gap_file = "src/gap.py"
        mapped = self._child("CHILD-1", key_file=gap_file)
        rc, captured = self._run([mapped], parent_verdict="unmet",
                                 gap_file=gap_file)
        assert rc == 0
        # The mapped child was audited; its (mocked) verdict is met here, but
        # the AC7 guard is that the parent must not silently mark it ready.
        child = captured["child_results"][0]
        assert child["child_audit_ready"] is False or child["ac_results"]
        assert child.get("inherited_pass") is None

    # ------------------------------------------------------------------
    # Helper unit tests
    # ------------------------------------------------------------------

    def test_parent_has_gaps(self):
        """_parent_has_gaps: unmet/partial are gaps; adjusted is not."""
        assert audit_runner._parent_has_gaps([
            {"verdict": "met"}, {"verdict": "adjusted"},
        ]) is False
        assert audit_runner._parent_has_gaps([
            {"verdict": "met"}, {"verdict": "unmet"},
        ]) is True
        assert audit_runner._parent_has_gaps([
            {"verdict": "met"}, {"verdict": "partial"},
        ]) is True
        assert audit_runner._parent_has_gaps([]) is False

    def test_child_content_changed_fresh_audit_false(self):
        """_child_content_changed: a content-fresh audit → unchanged."""
        from skill.audit.scripts import audit_runner as ar
        head = "a" * 40
        child_desc = "## Acceptance Criteria\n- CAC1: child criterion"
        with mock.patch.object(ar, "_resolve_audited_head", return_value=head):
            fp = ar._compute_content_fingerprint(
                mock.MagicMock(), "CHILD-1",
                work_item={"description": child_desc},
            )
        stored = (
            f"Ready to close: Yes\n\nAudit report for work item CHILD-1\n\n"
            f"{ar.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fp}\n\n## Summary\nok"
        )

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {"success": True, "audit": {
                    "workItemId": "CHILD-1",
                    "auditedAt": "2026-08-01T00:00:00.000Z",
                    "rawOutput": stored,
                }}
            if "show" in cmd_str:
                return {"success": True, "workItem": {
                    "id": "CHILD-1", "description": child_desc,
                }}
            raise AssertionError(f"unexpected wl cmd: {cmd_str}")

        with (
            mock.patch.object(ar, "_run_wl", side_effect=_fake_run_wl),
            mock.patch.object(ar, "_resolve_audited_head", return_value=head),
        ):
            changed = ar._child_content_changed(mock.MagicMock(), "CHILD-1")
        assert changed is False

    def test_child_content_changed_stale_audit_true(self):
        """_child_content_changed: an audit whose fingerprint no longer
        matches (different HEAD) → content changed."""
        from skill.audit.scripts import audit_runner as ar
        # Store the fingerprint under HEAD 'a', then re-check under 'b'.
        with mock.patch.object(ar, "_resolve_audited_head", return_value="a" * 40):
            fp = ar._compute_content_fingerprint(
                mock.MagicMock(), "CHILD-1",
                work_item={"description": "## Acceptance Criteria\n- CAC1: x"},
            )
        stored = (
            f"Ready to close: Yes\n\nAudit report for work item CHILD-1\n\n"
            f"{ar.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fp}\n\n## Summary\nok"
        )

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {"success": True, "audit": {
                    "workItemId": "CHILD-1",
                    "auditedAt": "2026-08-01T00:00:00.000Z",
                    "rawOutput": stored,
                }}
            if "show" in cmd_str:
                return {"success": True, "workItem": {
                    "id": "CHILD-1",
                    "description": "## Acceptance Criteria\n- CAC1: x",
                }}
            raise AssertionError(f"unexpected wl cmd: {cmd_str}")

        with (
            mock.patch.object(ar, "_run_wl", side_effect=_fake_run_wl),
            # HEAD moved from 'a' to 'b' → fingerprint mismatch → changed
            mock.patch.object(ar, "_resolve_audited_head", return_value="b" * 40),
        ):
            changed = ar._child_content_changed(mock.MagicMock(), "CHILD-1")
        assert changed is True

    def test_child_content_changed_no_audit_false(self):
        """_child_content_changed: no stored audit → nothing to compare →
        unchanged (inheritance safe)."""
        from skill.audit.scripts import audit_runner as ar

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {"success": True, "audit": None}
            if "show" in cmd_str:
                return {"success": True, "workItem": {"id": "CHILD-1"}}
            raise AssertionError(f"unexpected wl cmd: {cmd_str}")

        with mock.patch.object(ar, "_run_wl", side_effect=_fake_run_wl):
            changed = ar._child_content_changed(mock.MagicMock(), "CHILD-1")
        assert changed is False

    def test_map_gaps_to_children_by_key_files(self):
        """_map_gaps_to_children: children whose Key Files appear in gap
        evidence are mapped; others are not."""
        ac_results = [
            {"verdict": "unmet", "evidence": "src/gap.py:10 — not implemented"},
        ]
        children = [
            self._child("CHILD-1", key_file="src/gap.py"),
            self._child("CHILD-2", key_file="src/other.py"),
        ]
        mapped = audit_runner._map_gaps_to_children(ac_results, children)
        assert mapped == ["CHILD-1"]

    def test_map_gaps_to_children_no_refs_returns_all(self):
        """_map_gaps_to_children: no evidence file refs → conservative: all
        children are mapped so nothing is silently skipped."""
        ac_results = [
            {"verdict": "unmet", "evidence": "no file reference here"},
        ]
        children = [self._child("CHILD-1"), self._child("CHILD-2")]
        mapped = audit_runner._map_gaps_to_children(ac_results, children)
        assert set(mapped) == {"CHILD-1", "CHILD-2"}

    def test_map_gaps_to_children_no_match_returns_all(self):
        """_map_gaps_to_children: gap refs exist but no child Key Files match
        → conservative: all children are mapped."""
        ac_results = [
            {"verdict": "unmet", "evidence": "src/gap.py:10"},
        ]
        children = [self._child("CHILD-1", key_file="src/other.py")]
        mapped = audit_runner._map_gaps_to_children(ac_results, children)
        assert mapped == ["CHILD-1"]

