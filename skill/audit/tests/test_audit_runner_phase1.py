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
from audit.scripts import audit_runner
from audit.tests.wl_helpers import stateful_wl_side_effect


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
            "code_review.scripts.code_quality.run_code_quality", mock_cq
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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
            "code_review.scripts.code_quality.run_code_quality",
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



# ---------------------------------------------------------------------------
# AC5 (SA-0MVF1NKZQ009P3ZG / SA-0MVI6VRJT005M650): reduce the Phase 1 screening
# call count. A 10-child parent currently issues N+1 serial screens (one parent
# + one per child); batch mode folds all pending child screens into ONE call.
# ---------------------------------------------------------------------------


def _fake_runner():
    """A benign wl runner that succeeds on every call (no real wl needed)."""
    runner = mock.MagicMock()
    runner.side_effect = lambda cmd, **kw: mock.MagicMock(
        returncode=0, stdout=json.dumps({"success": True}), stderr=""
    )
    return runner


def _make_child(idx: int, acs: int = 3) -> dict:
    """A synthetic pending child with *acs* acceptance criteria."""
    bullets = "\n".join(f"- Criterion {i} for child {idx}" for i in range(acs))
    return {
        "id": f"SA-CHILD{idx:02d}",
        "title": f"Child {idx}",
        "status": "open",
        "stage": "in_progress",
        "priority": "medium",
        "description": f"## Acceptance Criteria\n\n{bullets}\n",
    }


def _phase1_children(ten: list[dict]):
    """Pending-children pairs as the dispatcher builds them."""
    return [(i, child) for i, child in enumerate(ten)]


def test_phase1_batch_issues_fewer_calls_than_n_plus_one(monkeypatch):
    """AC5: batching a 10-child parent issues materially fewer Phase 1 calls."""
    ten = [_make_child(i) for i in range(10)]
    pending = _phase1_children(ten)
    contexts: list[str] = []

    def fake_call(issue_id, context, prompt, **kwargs):
        contexts.append(context)
        # One verdict per requested AC, keyed by child + index.
        return {"extracted_text": json.dumps([
            {"child": ci, "index": j, "verdict": "met", "evidence": "x.py:1"}
            for ci, _child in pending
            for j in range(3)
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        results = audit_runner._phase1_review_children_batched(
            pending, "fast-model", "full-model", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )

    batched_calls = len(contexts)
    n_plus_one_children = len(pending)  # one screen per child
    # The N+1 structure also spends one parent screen; batching must beat it.
    assert batched_calls < n_plus_one_children + 1, (
        f"batched Phase 1 issued {batched_calls} calls, "
        f"N+1 would be {n_plus_one_children + 1}"
    )
    assert batched_calls == 1, "all pending children must fold into ONE call"
    # Every child still gets its own per-AC verdicts.
    assert len(results) == len(pending)
    for ci, _child in pending:
        assert [r["verdict"] for r in results[ci]] == ["met", "met", "met"]


def test_phase1_batch_preserves_verdict_semantics(monkeypatch):
    """AC5: batching must not change met/unmet/partial/adjusted meanings."""
    ten = [_make_child(i, acs=2) for i in range(3)]
    pending = _phase1_children(ten)
    scripted = {
        0: ["met", "unmet"],
        1: ["partial", "adjusted"],
        2: ["unmet", "met"],
    }

    def fake_call(issue_id, context, prompt, **kwargs):
        return {"extracted_text": json.dumps([
            {"child": ci, "index": j, "verdict": v, "evidence": f"f{ci}.py:{j}"}
            for ci, verdicts in scripted.items()
            for j, v in enumerate(verdicts)
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        results = audit_runner._phase1_review_children_batched(
            pending, "fast-model", "full_model", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    for ci, verdicts in scripted.items():
        assert [r["verdict"] for r in results[ci]] == verdicts


def test_phase1_batch_missing_child_falls_back_to_partial(monkeypatch):
    """A child absent from the batch response is 'partial', never 'met'."""
    ten = [_make_child(0, acs=2), _make_child(1, acs=2)]
    pending = _phase1_children(ten)

    def fake_call(issue_id, context, prompt, **kwargs):
        # Only child 0 is answered.
        return {"extracted_text": json.dumps([
            {"child": 0, "index": 0, "verdict": "met", "evidence": "a.py:1"},
            {"child": 0, "index": 1, "verdict": "met", "evidence": "a.py:2"},
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        results = audit_runner._phase1_review_children_batched(
            pending, "fast-model", "full_model", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    assert [r["verdict"] for r in results[0]] == ["met", "met"]
    assert [r["verdict"] for r in results[1]] == ["partial", "partial"]


def test_phase1_batch_unparseable_response_degrades_all_to_partial(monkeypatch):
    """A wholly unparseable batch response never yields 'met'."""
    pending = _phase1_children([_make_child(0, acs=2)])

    def fake_call(issue_id, context, prompt, **kwargs):
        return {"extracted_text": "the model rambled instead"}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        results = audit_runner._phase1_review_children_batched(
            pending, "fast-model", "full_model", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    assert [r["verdict"] for r in results[0]] == ["partial", "partial"]


def test_phase1_batch_prompt_lists_every_child(monkeypatch):
    """The batch prompt must enumerate all children with their ACs."""
    ten = [_make_child(i, acs=1) for i in range(3)]
    pending = _phase1_children(ten)
    prompts: list[str] = []

    def fake_call(issue_id, context, prompt, **kwargs):
        prompts.append(prompt)
        return {"extracted_text": json.dumps([
            {"child": ci, "index": 0, "verdict": "met", "evidence": "x.py:1"}
            for ci, _c in pending
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        audit_runner._phase1_review_children_batched(
            pending, "fast-model", "full_model", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    assert len(prompts) == 1
    prompt = prompts[0]
    for child in ten:
        assert child["id"] in prompt
    assert "child" in prompt.lower()
    assert "index" in prompt


# ---------------------------------------------------------------------------
# AC5: CLI/env surface for the Phase 1 model (defaults to current behaviour).
# ---------------------------------------------------------------------------


def test_phase1_batch_gate_defaults_off(monkeypatch):
    """Batching is opt-in so no existing invocation changes behaviour."""
    monkeypatch.delenv(audit_runner.AUDIT_PHASE1_BATCH_ENV, raising=False)
    assert audit_runner._phase1_batch_enabled() is False
    assert audit_runner._phase1_batch_enabled(True) is True
    assert audit_runner._phase1_batch_enabled(False) is False


def test_phase1_batch_gate_env_on(monkeypatch):
    monkeypatch.setenv(audit_runner.AUDIT_PHASE1_BATCH_ENV, "1")
    assert audit_runner._phase1_batch_enabled() is True


def test_phase1_model_env_surface(monkeypatch):
    """AUDIT_PHASE1_MODEL selects the Phase 1 model without touching Phase 2."""
    monkeypatch.setenv(audit_runner.AUDIT_PHASE1_MODEL_ENV, "fast-phase1")
    assert audit_runner._resolve_phase1_model(None) == "fast-phase1"
    # Explicit CLI wins over the env var.
    assert audit_runner._resolve_phase1_model("cli-model") == "cli-model"


def test_phase1_model_defaults_to_resolved_model(monkeypatch):
    """Absent any override, Phase 1 keeps using the resolved audit model."""
    monkeypatch.delenv(audit_runner.AUDIT_PHASE1_MODEL_ENV, raising=False)
    assert audit_runner._resolve_phase1_model(None) is None


def test_cli_exposes_batch_phase1_and_phase1_model():
    parser = audit_runner.build_parser()
    args = parser.parse_args([
        "issue", "SA-X", "--batch-phase1", "--phase1-model", "fast-model",
    ])
    assert args.batch_phase1 is True
    assert args.phase1_model == "fast-model"


def test_phase1_batch_parity_with_per_child_on_fixed_item_set(monkeypatch):
    """AC5: batched and per-child screening give equivalent verdicts.

    Fixed item set of 4 children x 2 ACs with a scripted model answer. The
    per-child path (N calls) and the batched path (1 call) must produce
    identical verdict/evidence per criterion — the batching-on/off comparison
    recorded for the frozen-semantics constraint.
    """
    children = [_make_child(i, acs=2) for i in range(4)]
    # Deterministic answers keyed by (child, index); reused by BOTH paths.
    answer = {
        (0, 0): ("met", "a.py:1"),
        (0, 1): ("unmet", "a.py:2"),
        (1, 0): ("partial", "b.py:1"),
        (1, 1): ("adjusted", "b.py:2"),
        (2, 0): ("met", "c.py:1"),
        (2, 1): ("met", "c.py:2"),
        (3, 0): ("unmet", "d.py:1"),
        (3, 1): ("met", "d.py:2"),
    }

    # --- per-child path: one call per child, indexes are per-child ---
    per_child_calls = 0

    def per_child_call(issue_id, context, prompt, **kwargs):
        nonlocal per_child_calls
        per_child_calls += 1
        child_idx = int(str(context).split(":")[-1].replace("SA-CHILD", ""))
        return {"extracted_text": json.dumps([
            {"index": j, "verdict": v, "evidence": e}
            for (ci, j), (v, e) in answer.items() if ci == child_idx
        ])}

    per_child: dict[int, list[dict]] = {}
    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=per_child_call
    ):
        for ci, child in enumerate(children):
            _ci, acs = audit_runner._phase1_review_child_acs(
                ci, child, "m", "m", "pi", None, None,
                _fake_runner(), lambda *a: None,
            )
            per_child[_ci] = acs

    # --- batched path: ONE call, answers carry the child index ---
    batch_calls = 0

    def batched_call(issue_id, context, prompt, **kwargs):
        nonlocal batch_calls
        batch_calls += 1
        return {"extracted_text": json.dumps([
            {"child": ci, "index": j, "verdict": v, "evidence": e}
            for (ci, j), (v, e) in answer.items()
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=batched_call
    ):
        batched = audit_runner._phase1_review_children_batched(
            list(enumerate(children)), "m", "m", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )

    assert per_child_calls == 4, "baseline is one call per child (plus parent)"
    assert batch_calls == 1, "batching must fold all children into one call"
    # Recorded equivalence: identical verdict + evidence per criterion.
    for ci in range(4):
        assert [r["verdict"] for r in batched[ci]] == [
            r["verdict"] for r in per_child[ci]
        ]
        assert [r["evidence"] for r in batched[ci]] == [
            r["evidence"] for r in per_child[ci]
        ]
        assert [r["text"] for r in batched[ci]] == [
            r["text"] for r in per_child[ci]
        ]


# ---------------------------------------------------------------------------
# SA-0MVI3608M0080QLW: right-size the child-screen budget and quarantine
# capped screens so a budget-exhausted screen is never indistinguishable from
# a model judgement.
# ---------------------------------------------------------------------------


def test_child_screen_budget_derived_from_measured_p95(monkeypatch):
    """The default budget is derived from measured data, not a bare constant.

    Recorded corpus (2026-10-21, ContextHub): completed child screens
    min 258 s / median 389 s / p95 ~506 s / max 506 s, i.e. the unconstrained
    p95 already sat just 15 % below the old 600 s cap. The budget must carry a
    documented safety factor over that p95 (proposal >= 2x) so host-load
    noise does not convert completions into timeouts.
    """
    measured_p95 = 506
    safety_factor = 2
    budget = audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT
    assert budget >= measured_p95 * safety_factor * 0.99, (
        f"child-screen budget {budget}s must cover {safety_factor}x the "
        f"measured p95 ({measured_p95}s)"
    )
    assert budget <= 1800, "must stay below the Phase-2 budget"


def test_parent_timeout_scaling_covers_the_child_budget():
    """AC: raising the child budget must not make the parent guard the new
    starvation point — the per-child term must cover one full child screen."""
    assert (
        audit_runner.PARENT_TIMEOUT_PER_CHILD
        >= audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT
    ), (
        f"parent per-child term {audit_runner.PARENT_TIMEOUT_PER_CHILD}s must "
        f"cover a {audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT}s child screen"
    )


def test_capped_child_screen_is_quarantined_not_plain_partial():
    """A child screen that hits its budget carries distinct, quarantinable
    evidence (timeout + phase) rather than an indistinguishable 'partial'."""
    children = [_make_child(0, acs=2)]
    pending = [(0, children[0])]

    def fake_call(issue_id, context, prompt, **kwargs):
        return {
            "_timeout": True,
            "evidence": "timed out after 600s",
            "extracted_text": "",
        }

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        results = audit_runner._phase1_review_children_batched(
            pending, "m", "m", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    verdicts = [r["verdict"] for r in results[0]]
    assert verdicts == [audit_runner.VERDICT_PARTIAL, audit_runner.VERDICT_PARTIAL]
    for r in results[0]:
        assert "budget" in r["evidence"].lower() or "timeout" in r["evidence"].lower()


def test_capped_child_screen_increments_reliability_counter():
    """child_screen_timeouts surfaces capped screens in the summary line."""
    audit_runner._rel_reset()
    children = [_make_child(0, acs=1)]
    pending = [(0, children[0])]

    def fake_call(issue_id, context, prompt, **kwargs):
        return {"_timeout": True, "evidence": "timed out", "extracted_text": ""}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        audit_runner._phase1_review_children_batched(
            pending, "m", "m", "pi", None, None,
            _fake_runner(), lambda *a: None,
        )
    assert audit_runner._rel_snapshot().get("child_screen_timeouts") == 1
    assert "child_screen_timeouts=1" in audit_runner.render_reliability_summary()


# ---------------------------------------------------------------------------
# SA-0MVJ3Q662006RDJR: right-size the parent Phase-1 screen budget, quarantine
# capped parent screens exactly like capped child screens, and never re-ask or
# retry after a capped screen so a single-item audit issues exactly ONE Phase 1
# call (the 1800 s parent cap + 1147 s re-ask of SA-0MVI6VVC00029XO4's run is
# the residual cost this child removes).
# ---------------------------------------------------------------------------


def _parent_screen_ctx(**overrides) -> audit_runner._AuditContext:
    """A minimal ``_AuditContext`` for direct ``_phase1_parent_screening``
    unit tests (mirrors ``test_audit_runner_phase1_model._make_ctx``)."""
    runner = mock.MagicMock()
    runner.side_effect = None
    defaults = {
        "issue_id": "TEST-1", "persist": False, "timeout": None,
        "parent_timeout": None,
        "pi_bin": "pi", "model": None, "model_source": "local",
        "runner": runner,
        "json_mode": False, "debug_log": None, "force": False,
        "worklog_dir": None, "batch_phase2": False, "green_run": None,
        "audit_children": False, "max_child_audits": None, "run_tests": False,
    }
    defaults.update(overrides)
    ctx = audit_runner._AuditContext(**defaults)
    ctx.acs = ["AC one", "AC two"]
    ctx.work_item = {
        "id": "TEST-1", "title": "T",
        "description": "## Acceptance Criteria\n1. AC one\n2. AC two\n",
    }
    ctx.ac_results = []
    ctx.owning_root = "."
    ctx.resolved_phase1_model = "fast-model"
    ctx.resolved_model = "full-model"
    return ctx


def test_parent_screen_budget_uses_right_sized_screening_default(monkeypatch):
    """AC1: the parent Phase-1 screen no longer keeps the 1800 s Phase-2
    budget — it uses the right-sized screening budget derived with the same
    documented discipline as the child budget (ceil(p95 x 2))."""
    monkeypatch.delenv(audit_runner.AUDIT_PARENT_SCREEN_TIMEOUT_ENV, raising=False)
    budget = audit_runner._PARENT_SCREEN_TIMEOUT_DEFAULT
    assert budget == audit_runner._CHILD_SCREEN_TIMEOUT_DEFAULT
    assert budget < audit_runner.CALL_PI_TIMEOUT, (
        "parent screen must be bounded below the 1800 s Phase-2 budget"
    )
    assert budget >= 1080  # documented screening budget (ceil(506 x 2) -> 1080)
    assert (
        audit_runner._resolve_call_timeout(None, parent_screen=True) == budget
    ), "parent screen resolves its own right-sized budget"
    # A Phase-2 call (no parent_screen) is unchanged at 1800 s.
    assert audit_runner._resolve_call_timeout(None) == audit_runner.CALL_PI_TIMEOUT
    # Explicit --timeout still wins, exactly like the child-screen budget.
    assert audit_runner._resolve_call_timeout(300, parent_screen=True) == 300


def test_parent_screen_timeout_env_and_cli_surface(monkeypatch):
    """AUDIT_PARENT_SCREEN_TIMEOUT / --parent-screen-timeout tune the parent
    screen independently of the child budget; an invalid env is ignored."""
    monkeypatch.setenv(audit_runner.AUDIT_PARENT_SCREEN_TIMEOUT_ENV, "900")
    assert audit_runner._resolve_call_timeout(None, parent_screen=True) == 900
    monkeypatch.setenv(audit_runner.AUDIT_PARENT_SCREEN_TIMEOUT_ENV, "nope")
    assert (
        audit_runner._resolve_call_timeout(None, parent_screen=True)
        == audit_runner._PARENT_SCREEN_TIMEOUT_DEFAULT
    )
    parser = audit_runner.build_parser()
    args = parser.parse_args(["issue", "SA-X", "--parent-screen-timeout", "840"])
    assert args.parent_screen_timeout == 840


def test_parent_screen_call_site_marks_parent_screen_budget():
    """The parent Phase-1 call site flags ``parent_screen`` so the call is
    bounded by the right-sized budget instead of the 1800 s Phase-2 budget."""
    seen: dict = {}

    def fake_call(issue_id, context, prompt, **kwargs):
        seen.update(kwargs)
        return {"extracted_text": json.dumps([
            {"index": 0, "verdict": "met", "evidence": "a.py:1"},
        ])}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ):
        audit_runner._call_phase1_screen(
            "SA-X", "parent", "p", "m", "pi", None, None, None,
            lambda *a: None, "parent screen", parent_screen=True,
        )
    assert seen.get("parent_screen") is True


def test_capped_parent_screen_issues_one_call_and_is_quarantined():
    """AC2/AC3: a parent screen that hits its budget issues exactly ONE
    Phase 1 call (no re-ask, no full-model retry), degrades to a quarantined
    ``partial`` whose evidence names the budget and phase, increments
    ``parent_screen_timeouts``, and never becomes ``met``."""
    audit_runner._rel_reset()
    ctx = _parent_screen_ctx()
    calls: list[str] = []

    def capped(issue_id, context, prompt, **kwargs):
        calls.append(context)
        return {
            "_timeout": True,
            "evidence": "Pi model call timed out after 1080s",
            "extracted_text": "",
        }

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=capped
    ), mock.patch.object(
        audit_runner, "_build_file_scope_manifest", return_value="manifest"
    ), mock.patch.object(
        audit_runner, "_validate_file_scope_manifest", return_value=None
    ), mock.patch.object(
        audit_runner, "_resolve_touched_files", return_value=[]
    ):
        audit_runner._phase1_parent_screening(ctx)

    assert calls == ["parent"], (
        f"capped parent screen issued calls {calls}; must be exactly ONE "
        "('parent') — no verdict_reask, no full-model retry"
    )
    assert [r["verdict"] for r in ctx.ac_results] == ["partial", "partial"]
    ev = ctx.ac_results[0]["evidence"].lower()
    assert "budget" in ev and "quarantined" in ev
    assert "audit_parent_screen_timeout" in ev, ev
    assert "phase 1" in ev, ev  # the phase is named (Phase 1 budget)
    # Partial never demotes/relaxes the ready-to-close gate: a gap remains.
    assert audit_runner._parent_has_gaps(ctx.ac_results) is True
    _snap = audit_runner._rel_snapshot()
    assert _snap.get("parent_screen_timeouts") == 1
    assert "parent_screen_timeouts=1" in audit_runner.render_reliability_summary()


def test_capped_parent_screen_with_fast_model_still_single_call():
    """AC2: even with a distinct fast Phase-1 model configured, a capped
    parent screen is NOT retried on the full model — one Phase 1 call total."""
    audit_runner._rel_reset()
    ctx = _parent_screen_ctx()
    calls: list[str] = []

    def capped(issue_id, context, prompt, **kwargs):
        calls.append(context)
        return {"_timeout": True, "evidence": "timed out", "extracted_text": ""}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=capped
    ), mock.patch.object(
        audit_runner, "_build_file_scope_manifest", return_value="manifest"
    ), mock.patch.object(
        audit_runner, "_validate_file_scope_manifest", return_value=None
    ), mock.patch.object(
        audit_runner, "_resolve_touched_files", return_value=[]
    ):
        audit_runner._phase1_parent_screening(ctx)

    assert calls == ["parent"]
    assert ctx.resolved_phase1_model != ctx.resolved_model  # fast != full here
    assert audit_runner._rel_snapshot().get("parent_screen_timeouts") == 1


def test_capped_screen_is_never_reasked_helper_level():
    """The bounded JSON re-ask fires only on genuine parse failures, never
    after a capped screen — a capped call is one Phase 1 call to completion
    (SA-0MVI6VVC00029XO4's 1800 s parent + 1147 s re-ask cannot recur)."""
    audit_runner._rel_reset()
    calls: list[str] = []

    def capped(issue_id, context, prompt, **kwargs):
        calls.append(context)
        return {"_timeout": True, "evidence": "timed out", "extracted_text": ""}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=capped
    ):
        _result, batch, _raw = audit_runner._call_phase1_screen(
            "SA-X", "parent", "screen these", "m", "pi", None, None, None,
            lambda *a: None, "parent screen",
        )
    assert calls == ["parent"], f"expected no re-ask after a cap, got {calls}"
    assert batch == []
    assert audit_runner._rel_snapshot().get("verdict_reasks") == 0


def test_single_item_audit_under_batching_issues_one_phase1_call(capsys):
    """AC2: a single-item (no children) audit under ``--batch-phase1`` issues
    exactly ONE Phase 1 call (the parent screen) with verdict semantics and
    the ready-to-close gate unchanged."""
    description = (
        "# Test\n\n## Acceptance Criteria\n\n- AC1: The first criterion\n"
    )
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
                returncode=0, stdout=json.dumps({"success": True}), stderr="",
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
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    mock_runner.side_effect = stateful_wl_side_effect(_side_effect)
    contexts: list[str] = []

    def fake_call(issue_id, context, prompt, **kwargs):
        contexts.append(context)
        if context == "parent":
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ])}
        return {"extracted_text": "[]"}

    mock_cq = mock.MagicMock(
        return_value={"success": True, "findings": [], "fixes_applied": 0}
    )
    with mock.patch.dict(
        audit_runner.os.environ,
        {audit_runner.AUDIT_PHASE1_BATCH_ENV: "1"},
        clear=False,
    ), mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ), mock.patch(
        "code_review.scripts.code_quality.run_code_quality", mock_cq
    ):
        rc = audit_runner.cmd_issue(
            "TEST-1", persist=False, force=True, runner=mock_runner,
        )

    assert rc == 0
    phase1 = [
        c for c in contexts
        if c in ("parent", "phase1_batch", "verdict_reask")
        or c.startswith("child:")
    ]
    assert phase1 == ["parent"], (
        f"single-item audit issued Phase 1 calls {phase1}; expected exactly "
        "ONE ('parent')"
    )
    out = capsys.readouterr().out
    assert "Ready to close: Yes" in out


# ----------------------------------------------------------------------
# Code-quality scan scope (SA-0MUI5ZYO6002W3KD)
#
# The Phase 1 code-quality scan is documented (SA-0MSKB6VWU000RT58 AC1) to
# lint ONLY the work item's changed files — never the whole repository.
# Previously the runner passed ``cq_scope_files or None`` to
# ``run_code_quality``, and ``None`` means a whole-project lint; for an item
# whose work is already committed (the normal post-merge audit state) the
# git changed-file list is EMPTY, so every audit linted the whole repo and
# unrelated critical findings blocked it.
#
# The contract these tests pin:
#   AC1 — a clean checkout resolves scope from the item's committed touched
#         files (and its Key Files), not the whole repo.
#   AC2 — an empty effective scope SKIPS the scan with a documented reason
#         (rendered through ``code_quality_skipped_reason``); it never runs
#         the whole-repository scan. ``files=[]`` is not a valid skip either
#         (linter_runner treats a falsy list as a whole-project scan).
#   AC3 — ``run_code_quality`` is never invoked with ``files=None`` for an
#         already-merged item.
#   AC4 — unrelated critical findings in the same repo are not attributed to
#         the item under audit.
# ----------------------------------------------------------------------

_PROJECT_FILE = "skill/audit/scripts/audit_runner.py"
_PROJECT_FILE_2 = "skill/audit/scripts/persist_audit.py"


def _cq_scope_ctx(runner, **overrides):
    """Build an ``_AuditContext`` suitable for a direct ``_phase_fetch_and_cq``
    call (mirrors the helper in ``test_audit_runner_core.py``)."""
    defaults = {
        "issue_id": "TEST-1", "persist": False, "timeout": None,
        "parent_timeout": None, "pi_bin": "pi", "model": None,
        "model_source": "default", "runner": runner,
        "json_mode": False, "debug_log": None, "force": False,
        "worklog_dir": None, "batch_phase2": False, "green_run": None,
        "audit_children": False, "max_child_audits": None,
        "run_tests": False,
    }
    defaults.update(overrides)
    return audit_runner._AuditContext(**defaults)


def _cq_scope_runner(description: str, *, committed=(),
                     changed=(), untracked=()):
    """Mock wl/git runner modelling an audit checkout.

    *committed* are the paths returned by the ``git log --grep=<item id>``
    touched-file source (the item's delivered commits); *changed* and
    *untracked* feed ``git diff --name-only HEAD`` and
    ``git status --porcelain=v1`` (both empty for an already-merged item on
    a clean checkout).
    """
    commands: list[list[str]] = []

    def _side(cmd):
        commands.append(list(cmd))
        cs = " ".join(cmd)
        if "--children" in cs:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "TEST-1", "description": description},
                    "children": [],
                    "comments": [],
                }),
                stderr="",
            )
        if cs.startswith("git log --all"):
            lines = [f"{audit_runner._TOUCHED_FILES_COMMIT_MARKER}abc1234"]
            lines.extend(committed)
            return SimpleNamespace(
                returncode=0, stdout="\n".join(lines) + "\n", stderr="",
            )
        if cs.startswith("git diff --name-only"):
            return SimpleNamespace(
                returncode=0,
                stdout="".join(f"{p}\n" for p in changed),
                stderr="",
            )
        if cs.startswith("git status --porcelain"):
            return SimpleNamespace(
                returncode=0,
                stdout="".join(f"?? {p}\n" for p in untracked),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True}), stderr="",
        )

    runner = mock.MagicMock()
    runner.side_effect = stateful_wl_side_effect(_side)
    runner.commands = commands
    return runner


class TestCodeQualityScanScope:
    """SA-0MUI5ZYO6002W3KD — the code-quality scan never silently widens to
    the whole repository."""

    def _run_fetch(self, description, *, committed=(), changed=(),
                   untracked=(), cq_return=None):
        runner = _cq_scope_runner(
            description, committed=committed, changed=changed,
            untracked=untracked,
        )
        ctx = _cq_scope_ctx(runner)
        cq = mock.MagicMock(
            return_value=cq_return
            or {"success": True, "findings": [], "fixes_applied": 0}
        )
        with mock.patch(
            "code_review.scripts.code_quality.run_code_quality", cq
        ):
            rc = audit_runner._phase_fetch_and_cq(ctx)
        assert rc is None
        return ctx, cq

    def test_committed_item_scopes_scan_to_its_delivered_files(self):
        """AC1/AC3: clean checkout ⇒ the scanned file list is the item's
        committed touched files, never ``None`` (whole repo)."""
        description = (
            "## Key Files\n\n"
            f"- `{_PROJECT_FILE_2}` — the touched helper\n"
        )
        ctx, cq = self._run_fetch(
            description, committed=(_PROJECT_FILE,),
        )

        cq.assert_called_once()
        _, kwargs = cq.call_args
        assert kwargs.get("files") == sorted([_PROJECT_FILE, _PROJECT_FILE_2])
        assert kwargs.get("files") is not None
        assert kwargs.get("fix") is False
        assert ctx.cq_findings == []
        assert ctx.cq_skipped_reason is None

    def test_empty_scope_skips_scan_instead_of_scanning_whole_repo(self):
        """AC2/AC3: with no changed, committed or Key Files evidence the scan
        is SKIPPED (documented reason), never run over the whole repo."""
        ctx, cq = self._run_fetch("No key files here.")

        cq.assert_not_called()
        assert ctx.cq_findings == []
        assert ctx.cq_skipped_reason
        reason = ctx.cq_skipped_reason.lower()
        assert "no in-scope files" in reason
        assert "whole-repository scan" in reason

    def test_skip_reason_is_rendered_in_the_report(self):
        """AC2: the skip is surfaced through the existing
        ``code_quality_skipped_reason`` report field."""
        ctx, _ = self._run_fetch("No key files here.")
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1", "title": "T"},
            [{"index": 1, "text": "AC1: x", "verdict": "met",
              "evidence": "x"}],
            [],
            code_quality_findings=ctx.cq_findings,
            code_quality_skipped_reason=ctx.cq_skipped_reason,
            model="test-model", model_source="remote",
        )
        assert "Code quality check skipped:" in report
        assert "whole-repository scan" in report

    def test_unrelated_critical_findings_are_not_attributed(self):
        """AC4: findings that a WHOLE-REPO scan would report (unrelated
        files) never reach this item's audit."""
        unrelated = {
            "severity": "critical", "file": "proxy/tests/unrelated.py",
            "line": 197, "message": "F841 message", "linter": "ruff",
            "code": "F841",
        }

        def _side_effect(*, files=None, **kwargs):
            # The pre-fix call passed files=None and linted everything.
            if files is None:
                return {"success": True, "findings": [unrelated],
                        "fixes_applied": 0}
            return {"success": True, "findings": [], "fixes_applied": 0}

        runner = _cq_scope_runner(
            f"## Key Files\n\n- `{_PROJECT_FILE}`\n",
            committed=(_PROJECT_FILE,),
        )
        ctx = _cq_scope_ctx(runner)
        with mock.patch(
            "code_review.scripts.code_quality.run_code_quality",
            side_effect=_side_effect,
        ) as cq:
            assert audit_runner._phase_fetch_and_cq(ctx) is None

        cq.assert_called_once()
        assert ctx.cq_findings == [], (
            "unrelated whole-repo findings must not be attributed to the item"
        )
