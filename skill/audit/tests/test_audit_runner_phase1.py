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
from skill.audit.tests.wl_helpers import stateful_wl_side_effect


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

class TestPhase1IntakeNormalizesVerdict:
    """AC2: Phase 1 parent AC review records normalized verdicts.

    Drives ``cmd_issue`` end-to-end with a mocked wl runner and a mocked pi
    returning 'pass' verdicts for the parent AC review; the assembled report
    must show the criteria as met and ready-to-close Yes.
    """

    def _make_mock_runner(self, description: str):
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "TEST-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "TEST-1",
                            "description": description,
                            "status": "in_progress",
                        },
                        "children": [],
                    }),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )

        mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
        return mock_runner

    def test_parent_ac_review_normalizes_pass(self, capsys):
        """A Phase 1 'pass' batch produces met verdicts and Ready to close: Yes."""
        description = (
            "# Test\n\n## Acceptance Criteria\n\n"
            "- AC1: The first criterion\n- AC2: The second criterion\n"
        )
        mock_runner = self._make_mock_runner(description)
        pass_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "file.py:1"},
                {"index": 1, "verdict": "pass", "evidence": "file.py:2"},
            ]),
        }

        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=pass_batch
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality", mock_cq
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        report = capsys.readouterr().out
        assert "Ready to close: Yes" in report
        assert "| 1 |" in report

_PHASE1_READY_RAW = (
    "Audit report for work item CHILD-1\n"
    "Ready to close: Yes\n\n"
    "## Summary\nChild audit passed.\n"
)

def _phase1_parent_desc(key_file: str | None = None) -> str:
    desc = (
        "# Parent\n\n"
        "## Acceptance Criteria\n\n"
        "- AC1: parent criterion\n"
    )
    if key_file:
        desc += f"\n## Key Files\n- {key_file}\n"
    return desc

def _phase1_child(ci: int, child_id: str = "CHILD-1",
                  stage: str = "in_progress",
                  key_file: str | None = None) -> dict:
    desc = f"## Acceptance Criteria\n1. CAC{ci}: child criterion {ci}\n"
    if key_file:
        desc += f"\n## Key Files\n- {key_file}\n"
    return {
        "id": child_id,
        "title": f"Child {child_id}",
        "status": "in_progress",
        "stage": stage,
        "description": desc,
    }

def _make_phase1_runner(children: list[dict],
                        parent_desc: str | None = None,
                        child_audit_raw: dict | None = None):
    """Mock runner driving cmd_issue through Phase 1 with the given children.

    *child_audit_raw* maps child id -> rawOutput returned by ``wl audit-show``.
    Children absent from the map have no persisted audit and go through the
    Phase 1 child AC review path. Returns (runner, audit_show_call_log).
    """
    if parent_desc is None:
        parent_desc = _phase1_parent_desc()
    audit_shows: list[str] = []

    def _side_effect(cmd):
        cmd_list = list(cmd)
        cmd_str = " ".join(cmd_list)
        if "audit-show" in cmd_list:
            child_id = cmd_list[cmd_list.index("audit-show") + 1]
            audit_shows.append(child_id)
            raw = (child_audit_raw or {}).get(child_id)
            if raw is None:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItemId": child_id,
                    "audit": {
                        "workItemId": child_id,
                        "auditedAt": "2026-07-20T10:00:00.000Z",
                        "rawOutput": raw,
                    },
                }),
                stderr="",
            )
        if "show" in cmd_str and "--children" not in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "TEST-1", "status": "open"},
                }),
                stderr="",
            )
        if "update" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )
        if "--children" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "description": parent_desc,
                        "status": "in_progress",
                    },
                    "children": children,
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True}),
            stderr="",
        )

    mock_runner = mock.MagicMock()
    mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
    return mock_runner, audit_shows

class TestPhase1PromptFileScope:
    """AC1: Phase 1 parent and child AC review prompts include the
    file-scope manifest and SCANNING block (Phase 2 performance pattern)."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_parent_prompt_includes_manifest_and_scanning(self, capsys):
        key_file = "skill/audit/scripts/audit_runner.py"
        mock_runner, _audit_shows = _make_phase1_runner(
            [_phase1_child(1)], parent_desc=_phase1_parent_desc(key_file),
        )
        prompts: dict[str, str] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            if context == "parent":
                prompts["parent"] = prompt
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

        assert rc == 0
        prompt = prompts["parent"]
        assert "READ-ONLY" in prompt  # existing guard language preserved
        assert "FILE SCOPE" in prompt
        assert "SCANNING" in prompt
        assert "scan.py" in prompt
        assert "list-files" in prompt
        assert key_file in prompt  # Key Files manifest injected

    def test_child_prompt_includes_manifest_and_scanning(self, capsys):
        key_file = "skill/audit/scripts/audit_runner.py"
        child = _phase1_child(1, key_file=key_file)
        mock_runner, _audit_shows = _make_phase1_runner([child])
        prompts: dict[str, str] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            if context == "child:CHILD-1":
                prompts["child"] = prompt
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        prompt = prompts["child"]
        assert "READ-ONLY" in prompt
        assert "FILE SCOPE" in prompt
        assert "SCANNING" in prompt
        assert "scan.py" in prompt
        assert key_file in prompt  # child's Key Files manifest injected

    def test_prompts_never_embed_related_work_report(self, capsys):
        """P11: audit prompts never carry the auto-appended related-work report.

        The find-related report bloats descriptions (~58% of chars measured on
        SA-0MSF4AFX9007INSP). Even when a description contains a large
        'Related work (automated report)' section with raw keyword word-lists,
        no Phase 1 or Phase 2 prompt may embed it — only extracted ACs and the
        file-scope manifest are injected (regression guard for prompt size).
        """
        blob_kw = "zzwordlistmarker42"
        parent_desc = (
            "# Parent\n\n"
            "## Acceptance Criteria\n\n"
            "- AC1: parent criterion\n"
            "\n## Related work (automated report)\n"
            "### Related work items\n"
            "- **REL-001** – Some related item (open)\n"
            "### Repository file matches\n"
            f"- `skill/audit/scripts/audit_runner.py` — matched: "
            f"{blob_kw}, kw2, kw3, kw4, kw5, kw6, kw7, kw8, kw9\n"
        )
        child = _phase1_child(1)
        mock_runner, _audit_shows = _make_phase1_runner(
            [child], parent_desc=parent_desc,
        )
        prompts: dict[str, str] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            prompts[context] = prompt
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        # Both Phase 1 paths must have produced prompts
        assert "parent" in prompts
        assert "child:CHILD-1" in prompts
        for context, prompt in prompts.items():
            assert "Related work (automated report)" not in prompt, (
                f"{context} prompt must not embed the related-work report heading"
            )
            assert blob_kw not in prompt, (
                f"{context} prompt must not embed the related-work word-list blobs"
            )

class TestPhase1EnableTools:
    """AC2: Phase 1 parent and child AC review calls run with read-only tools
    (enable_tools=True, which adds --tools read,bash,grep,find,ls)."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_parent_and_child_phase1_calls_enable_tools(self, capsys):
        mock_runner, _audit_shows = _make_phase1_runner([_phase1_child(1)])
        seen: dict[str, bool] = {}

        def _capture(issue_id, context, prompt, **kwargs):
            seen[context] = kwargs.get("enable_tools")
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        assert seen.get("parent") is True
        assert seen.get("child:CHILD-1") is True

class TestPhase1ChildAuditReuse:
    """AC3/AC5: ready children skip the Phase 1 child AC review; the
    pre-computed verdict is reused (no second lookup in the auto-trigger
    loop) and their AC results come from their own persisted audit."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_ready_child_skips_phase1_review_and_reuses_verdict(self, capsys):
        child = _phase1_child(1, stage="in_review")
        mock_runner, audit_shows = _make_phase1_runner(
            [child], child_audit_raw={"CHILD-1": _PHASE1_READY_RAW},
        )
        contexts: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            contexts.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        # No Phase 1 child AC review call for the ready child.
        assert "child:CHILD-1" not in contexts
        # Verdict computed once in the pre-pass and reused: exactly two
        # audit-show calls (pre-pass verdict + own-audit AC extraction),
        # none from the auto-trigger loop.
        assert audit_shows.count("CHILD-1") == 2
        report = capsys.readouterr().out
        assert "CHILD-1" in report
        assert "CAC1: child criterion" in report

    def test_child_acs_from_own_audit_falls_back_to_met(self):
        child = _phase1_child(1)
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": _PHASE1_READY_RAW},
        )
        acs = audit_runner._child_acs_from_own_audit(child, mock_runner)
        assert len(acs) == 1
        assert acs[0]["text"] == "CAC1: child criterion 1"
        assert acs[0]["verdict"] == "met"
        assert "child's own fresh audit" in acs[0]["evidence"]

    def test_child_acs_from_own_audit_uses_parsed_table(self):
        child = _phase1_child(1)
        raw_with_table = (
            "Audit report for work item CHILD-1\n"
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | CAC1: child criterion 1 | met | child.py:10 |\n"
        )
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": raw_with_table},
        )
        acs = audit_runner._child_acs_from_own_audit(child, mock_runner)
        assert acs == [
            {"text": "CAC1: child criterion 1", "verdict": "met", "evidence": "child.py:10"}
        ]

    def test_no_audit_child_still_phase1_reviewed(self, capsys):
        """Children without a persisted audit keep the Phase 1 review call."""
        mock_runner, _audit_shows = _make_phase1_runner([_phase1_child(1)])
        contexts: list[str] = []

        def _capture(issue_id, context, prompt, **kwargs):
            contexts.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        assert "child:CHILD-1" in contexts

class TestPhase1ChildParallelism:
    """AC3: pending Phase 1 child AC reviews run with bounded parallelism,
    falling back to sequential execution when parallelism=1."""

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def test_pending_children_reviewed_concurrently(self, capsys):
        import threading
        import time as _time

        children = [_phase1_child(1, "CHILD-1"), _phase1_child(2, "CHILD-2")]
        mock_runner, _audit_shows = _make_phase1_runner(children)
        started = threading.Barrier(2)  # both child calls must be in-flight

        def _slow(issue_id, context, prompt, **kwargs):
            if context.startswith("child:"):
                started.wait(timeout=5)  # raises if not concurrent
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_slow
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            _t0 = _time.monotonic()
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )
            _elapsed = _time.monotonic() - _t0

        assert rc == 0
        # If sequential, the barrier would have raised (deadlock/timeout).
        assert _elapsed < 10

    def test_pending_children_sequential_when_parallelism_one(self, capsys):
        children = [_phase1_child(1, "CHILD-1"), _phase1_child(2, "CHILD-2")]
        mock_runner, _audit_shows = _make_phase1_runner(children)
        order: list[str] = []

        def _ordered(issue_id, context, prompt, **kwargs):
            if context.startswith("child:"):
                order.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "1"},
            clear=False,
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_ordered
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                audit_children=True,  # child Phase 1 review is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        assert order == ["child:CHILD-1", "child:CHILD-2"]

class TestPhase1ParseAuditReportAcs:
    """The persisted-audit AC table parser used to reuse ready children."""

    def test_parses_acceptance_criteria_table(self):
        raw = (
            "Audit report for work item SA-X\n"
            "Ready to close: Yes\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | AC one | met | file.py:1 |\n"
            "| 2 | AC two | adjusted | file.py:2 — acceptable variance |\n"
        )
        acs = audit_runner._parse_audit_report_acs(raw)
        assert acs == [
            {"text": "AC one", "verdict": "met", "evidence": "file.py:1"},
            {
                "text": "AC two",
                "verdict": "adjusted",
                "evidence": "file.py:2 — acceptable variance",
            },
        ]

    def test_returns_none_when_no_table(self):
        assert (
            audit_runner._parse_audit_report_acs(
                "Ready to close: Yes\n\n## Summary\nOK."
            )
            is None
        )

class TestPhase1ChildWorkerExceptionSafety:
    """The Phase 1 child AC review worker never raises and records failures."""

    def test_worker_records_script_failure_on_pi_error(self):
        child = _phase1_child(1)
        mock_runner, _audit_shows = _make_phase1_runner([])
        failures: list[tuple[str, str]] = []

        def _boom(issue_id, context, prompt, **kwargs):
            raise RuntimeError("pi exploded")

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_boom
        ):
            ci, acs = audit_runner._phase1_review_child_acs(
                0, child,
                phase1_model="test-model",
                full_model="test-model",
                pi_bin="pi",
                debug_log=None,
                timeout=None,
                runner=mock_runner,
                script_failure_callback=lambda ctx, exc: failures.append(
                    (ctx, str(exc))
                ),
            )

        assert ci == 0
        assert failures and "child AC review" in failures[0][0]
        # Parse-failure fallback yields a diagnostic 'partial' verdict,
        # matching the sequential Phase 1 child path.
        assert acs[0]["text"] == "CAC1: child criterion 1"
        assert acs[0]["verdict"] == "partial"

_PHASE1_NOT_READY_RAW = (
    "Audit report for work item CHILD-1\n"
    "Ready to close: No\n\n"
    "## Summary\nChild audit not ready.\n"
)

class TestPhase2NotReadyChildReuse:
    """P12: children whose own fresh audit returned 'not ready to close'
    (child_audit_not_ready=True) skip the duplicated phase2_child call and
    the parent reuses the child's own persisted audit findings."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    child_audit_ready: bool = False,
                    child_audit_not_ready: bool = True,
                    stage: str = "in_review",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": stage,
            "status": status,
            "child_audit_ready": child_audit_ready,
            "child_audit_not_ready": child_audit_not_ready,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": "phase1"}
                for i in range(ac_count)
            ],
        }

    def test_skips_deep_analysis_when_child_audit_not_ready(self):
        """AC1: no phase2_child call is made for a child whose own audit
        returned 'not ready'; its own persisted findings are reused."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=False,
                                 child_audit_not_ready=True)
        reused = [{"text": "Child AC 0", "verdict": "unmet", "evidence": "child.py:9"}]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call, mock.patch.object(
            audit_runner, "_child_acs_from_own_audit", return_value=reused,
        ) as mock_reuse:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # Only the parent phase2_deep call is made; no phase2_child call.
        child_calls = [
            c for c in mock_call.call_args_list if c[0][0] == "CHILD-1"
        ]
        assert child_calls == []
        # The child's own findings were fetched via the reuse helper.
        mock_reuse.assert_called_once()
        # Parent ACs still processed normally.
        assert updated_acs[0]["verdict"] == "met"
        assert phase2_completed is True
        # Child ACs now reflect its own audit findings (not the parent's).
        assert updated_children[0]["ac_results"] == reused

    def test_not_ready_reuse_keeps_phase1_results_on_parse_failure(self):
        """AC1 fallback: when the child's own audit table cannot be parsed,
        the parent keeps the child's Phase 1 screening results."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=False,
                                 child_audit_not_ready=True)
        phase1_acs = list(child["ac_results"])

        def _reuse_with_fallback(child, runner, worklog_dir=None, fallback=None):
            return fallback

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ), mock.patch.object(
            audit_runner, "_child_acs_from_own_audit",
            side_effect=_reuse_with_fallback,
        ) as mock_reuse:
            _updated_acs, updated_children, _completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # The Phase 1 screening results were passed as the fallback and kept.
        assert mock_reuse.call_args.kwargs["fallback"] == phase1_acs
        assert updated_children[0]["ac_results"] == phase1_acs

    def test_mixed_children_skip_only_ready_and_not_ready(self):
        """AC1: in a mixed set, only child_audit_ready=True and
        child_audit_not_ready=True children skip phase2_child; authentic
        pending children (no own audit) still get deep analysis."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        ready_child = self._make_child("READY-1", child_audit_ready=True,
                                       child_audit_not_ready=False)
        not_ready_child = self._make_child("NOTREADY-1", child_audit_ready=False,
                                           child_audit_not_ready=True)
        pending_child = self._make_child("PENDING-1", child_audit_ready=False,
                                         child_audit_not_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call, mock.patch.object(
            audit_runner, "_child_acs_from_own_audit",
            return_value=[{"text": "x", "verdict": "partial", "evidence": ""}],
        ):
            _updated_acs, _updated_children, _completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs,
                    [ready_child, not_ready_child, pending_child],
                    "test-model",
                )
            )

        ready_calls = [c for c in mock_call.call_args_list if c[0][0] == "READY-1"]
        not_ready_calls = [
            c for c in mock_call.call_args_list if c[0][0] == "NOTREADY-1"
        ]
        pending_calls = [c for c in mock_call.call_args_list if c[0][0] == "PENDING-1"]
        assert ready_calls == []
        assert not_ready_calls == []
        assert len(pending_calls) == 1

    def test_not_ready_child_not_batched(self):
        """AC1 (batch path): a not-ready child is excluded from the
        phase2_batch call and its reused findings are preserved."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        not_ready_child = self._make_child("NOTREADY-1", child_audit_ready=False,
                                           child_audit_not_ready=True)
        pending_child = self._make_child("PEND-1", child_audit_ready=False,
                                         child_audit_not_ready=False)

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p"},
                {"index": 1, "verdict": "met", "evidence": "c"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ) as mock_call, mock.patch.object(
            audit_runner, "_child_acs_from_own_audit",
            return_value=[
                {"text": "Child AC 0", "verdict": "partial", "evidence": "own"}
            ],
        ):
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs,
                    [not_ready_child, pending_child],
                    "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        prompt = mock_call.call_args.args[2]
        assert "NOTREADY-1" not in prompt
        assert "PEND-1" in prompt
        assert updated_children[0]["ac_results"][0]["verdict"] == "partial"

    def test_cmd_issue_marks_not_ready_child_for_phase2_reuse(self, capsys):
        """P12 wiring: a child whose own fresh audit says 'not ready to close'
        is marked child_audit_not_ready=True so Phase 2 skips the duplicated
        phase2_child call (verified via the field passed to
        _run_phase2_deep_analysis)."""
        child = _phase1_child(1, stage="in_review")
        mock_runner, _audit_shows = _make_phase1_runner(
            [child],
            child_audit_raw={"CHILD-1": _PHASE1_NOT_READY_RAW},
        )
        captured: dict = {}

        def _fake_phase2(issue, ac_results, child_results, **kwargs):
            captured["child_results"] = child_results
            return ac_results, child_results, True

        def _capture(issue_id, context, prompt, **kwargs):
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "f.py:1"},
            ])}

        with mock.patch.object(
            audit_runner, "_run_phase2_deep_analysis", side_effect=_fake_phase2
        ), mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_capture
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            mock.MagicMock(
                return_value={"success": True, "findings": [], "fixes_applied": 0}
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=mock_runner,
                audit_children=True,  # child flow is opt-in (SA-0MSKB6VJA005N43F)
            )

        assert rc == 0
        assert captured["child_results"][0]["child_audit_not_ready"] is True

class TestChildAcsFromOwnAuditFallback:
    """P12: _child_acs_from_own_audit honors an explicit fallback for the
    not-ready reuse path (unparseable child audit table)."""

    def test_fallback_used_when_table_unparseable(self):
        child = _phase1_child(1)
        raw_no_table = "Ready to close: No\n\n## Summary\nno table here\n"
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": raw_no_table},
        )
        fallback = [
            {"text": "CAC1: child criterion 1", "verdict": "partial",
             "evidence": "phase1"}
        ]
        acs = audit_runner._child_acs_from_own_audit(
            child, mock_runner, fallback=fallback,
        )
        assert acs == fallback

    def test_parsed_table_beats_fallback(self):
        child = _phase1_child(1)
        raw_with_table = (
            "Audit report for work item CHILD-1\n"
            "Ready to close: No\n\n"
            "## Acceptance Criteria Status\n\n"
            "| # | Criterion | Verdict | Evidence |\n"
            "|---|-----------|---------|----------|\n"
            "| 1 | CAC1: child criterion 1 | unmet | child.py:10 |\n"
        )
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": raw_with_table},
        )
        fallback = [
            {"text": "CAC1: child criterion 1", "verdict": "met",
             "evidence": "phase1"}
        ]
        acs = audit_runner._child_acs_from_own_audit(
            child, mock_runner, fallback=fallback,
        )
        assert acs[0]["verdict"] == "unmet"
        assert acs[0]["evidence"] == "child.py:10"

    def test_no_fallback_keeps_met_fallback(self):
        """Backward compatibility: without a fallback the met-with-reuse-note
        fallback is used, matching the P7 ready-child path."""
        child = _phase1_child(1)
        raw_no_table = "Ready to close: Yes\n\n## Summary\nno table here\n"
        mock_runner, _audit_shows = _make_phase1_runner(
            [], child_audit_raw={"CHILD-1": raw_no_table},
        )
        acs = audit_runner._child_acs_from_own_audit(child, mock_runner)
        assert len(acs) == 1
        assert acs[0]["verdict"] == "met"
        assert "child's own fresh audit" in acs[0]["evidence"]

class TestExtractAcsHeadingVariantsIntegration:
    """Re-audit AC table integration (SA-0MSJLC8XA00178YD AC3/AC5).

    Driving cmd_issue over a mocked wl runner whose parent description uses
    a parenthetical / bold / angle-bracket heading, the rendered report's
    Acceptance Criteria Status table lists the item's REAL ACs instead of
    the 'No acceptance criteria defined.' fallback — the same outcome the
    re-audit of LP-0MSG45I8Q0020N1F / NV-0MSGM4XQP007V6UM now produces.
    """

    _MET_JSON = json.dumps([
        {"index": 0, "verdict": "met", "evidence": "f.py:1"},
        {"index": 1, "verdict": "met", "evidence": "f.py:2"},
    ])

    def _mock_cq(self):
        return mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

    def _run(self, parent_desc):
        mock_runner, _audit_shows = _make_phase1_runner(
            [], parent_desc=parent_desc,
        )
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": self._MET_JSON},
        ), mock.patch(
            "skill.code_review.scripts.code_quality.run_code_quality",
            self._mock_cq(),
        ):
            return audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
            )

    def _assert_real_acs_in_table(self, capsys, *expected):
        report = capsys.readouterr().out
        assert "No acceptance criteria defined." not in report
        for ac in expected:
            assert ac in report

    def test_parenthetical_heading_acs_in_table(self, capsys):
        """AC3: parenthetical heading item reports its real ACs."""
        desc = (
            "## Acceptance criteria (testable)\n"
            "\n"
            "1. First criterion\n"
            "2. Second criterion\n"
        )
        rc = self._run(desc)
        assert rc == 0
        self._assert_real_acs_in_table(
            capsys, "First criterion", "Second criterion",
        )

    def test_bold_heading_acs_in_table(self, capsys):
        """AC5: bold-heading item reports its real ACs."""
        desc = (
            "**Acceptance criteria:**\n"
            "\n"
            "1. First criterion\n"
            "2. Second criterion\n"
        )
        rc = self._run(desc)
        assert rc == 0
        self._assert_real_acs_in_table(
            capsys, "First criterion", "Second criterion",
        )

    def test_angle_bracket_heading_acs_in_table(self, capsys):
        """Angle-bracket convention item reports its real ACs."""
        desc = (
            "<<Acceptance>> <<criteria>>\n"
            "\n"
            "1. First criterion\n"
            "2. Second criterion\n"
        )
        rc = self._run(desc)
        assert rc == 0
        self._assert_real_acs_in_table(
            capsys, "First criterion", "Second criterion",
        )

