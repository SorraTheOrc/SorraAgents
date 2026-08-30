from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


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

class TestRunPhase2DeepAnalysisEnableTools:
    """Tests for _run_phase2_deep_analysis() using enable_tools=True (AC1-AC5)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    stage: str = "in_progress",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_parent_deep_analysis_uses_enable_tools_true(self):
        """AC1: Parent deep analysis calls _call_pi_and_maybe_log with enable_tools=True."""
        issue = self._make_issue()
        acs = [self._make_ac(0), self._make_ac(1)]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        # Find the parent call (first argument is issue_id)
        parent_calls = [
            call for call in mock_call.call_args_list
            if call[0][0] == "TEST-1" and call[0][1] == "phase2_deep"
        ]
        assert len(parent_calls) >= 1
        _args, kwargs = parent_calls[0]
        assert kwargs.get("enable_tools") is True

    def test_child_deep_analysis_uses_enable_tools_true(self):
        """AC2: Child deep analysis calls _call_pi_and_maybe_log with enable_tools=True."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", ac_count=2)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        # Find the child call (first argument is child id)
        child_calls = [
            call for call in mock_call.call_args_list
            if call[0][0] == "CHILD-1"
        ]
        assert len(child_calls) >= 1
        _args, kwargs = child_calls[0]
        assert kwargs.get("enable_tools") is True

    def test_phase2_prompt_unchanged(self):
        """AC3: Phase 2 prompt remains appropriate for tools-enabled mode.

        The prompt already asks the model to read files, which is now feasible.
        Verify it still contains the file-reading instructions.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}

            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        mock_call.assert_called_once()
        prompt = mock_call.call_args[0][2]  # third positional arg is prompt
        assert "Read the actual implementation files" in prompt
        assert "file:line reference" in prompt

    def test_non_phase2_calls_unchanged(self):
        """AC5: Non-Phase-2 calls (Phase 1, project-level) remain unchanged.

        Verify that _call_pi() and _call_pi_and_maybe_log() default to
        enable_tools=False for non-Phase-2 callers. This is verified by
        the existing TestCallPiEnableTools and TestCallPiAndMaybeLogEnableTools
        tests which confirm the default is False.
        """
        # This is a documentation/coverage test - the actual behavior
        # is verified by the other test classes.
        # Confirm the default is False in both functions.
        with mock.patch.object(audit_runner.subprocess, "Popen") as mock_popen:
            mock_process = mock.MagicMock()
            mock_process.communicate.return_value = ("{}", "")
            mock_popen.return_value = mock_process

            # Phase 1-like call (no enable_tools argument)
            audit_runner._call_pi("test", model="test-model")
            args = mock_popen.call_args[0][0]
            assert "--tools" not in args

            # Phase 1-like call via _call_pi_and_maybe_log
            with mock.patch.object(audit_runner, "_call_pi") as mock_cp:
                mock_cp.return_value = {"verdict": "met", "evidence": ""}
                audit_runner._call_pi_and_maybe_log("PRJ", "project", "test")
                _args, kwargs = mock_cp.call_args
                assert kwargs.get("enable_tools") is False
    def test_parent_deep_analysis_dict_evidence_normalized(self):
        """SA-0MSKM2LSP006L0K8: Phase 2 batch items carrying structured
        dict evidence are normalized to a string (json-serialized, file
        refs salvageable) when merged into ac_results, instead of crashing
        downstream (e.g. _map_gaps_to_children)."""
        issue = self._make_issue()
        acs = [self._make_ac(0, verdict="met")]
        batch = [
            {"index": 0, "verdict": "met", "evidence": {
                "file": "src/gap.py", "line": 10, "note": "verified"}},
        ]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": json.dumps(batch)},
        ) as mock_call:
            updated_ac, _children, _ok = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )
        mock_call.assert_called_once()
        assert isinstance(updated_ac[0]["evidence"], str)
        assert "src/gap.py" in updated_ac[0]["evidence"]


class TestPhase2CitationCapPromptInjection:
    """F1-AC2/AC3/AC4: the max-citations-per-AC cap is injected into all
    three Phase-2 deep prompts (parent/child/batch) with the >=1 file:line
    floor, while verdicts/evidence pass through unchanged (prompt-level
    only; LP-0MSQ32WM5000NCB7)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1", ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def _cap_instruction(self, n: int) -> str:
        """The canonical cap instruction text the prompt must carry."""
        return f"AT MOST {n} specific file:line references"

    def test_parent_prompt_contains_cap_instruction(self):
        """AC2: the phase2_deep prompt carries the cap + >=1 floor."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
        prompt = mock_call.call_args.args[2]
        assert self._cap_instruction(audit_runner._DEFAULT_MAX_CITATIONS_PER_AC) in prompt
        assert "(minimum 1)" in prompt

    def test_child_prompt_contains_cap_instruction(self):
        """AC2: the phase2_child prompt carries the cap + >=1 floor."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1")
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [child], "test-model")
        child_calls = [
            c for c in mock_call.call_args_list
            if c.args[1] == "phase2_child:0"
        ]
        assert len(child_calls) == 1
        prompt = child_calls[0].args[2]
        assert self._cap_instruction(audit_runner._DEFAULT_MAX_CITATIONS_PER_AC) in prompt
        assert "(minimum 1)" in prompt

    def test_batch_prompt_contains_cap_instruction(self):
        """AC2: the phase2_batch prompt carries the cap + >=1 floor."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1")
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", batch_phase2=True,
            )
        prompt = mock_call.call_args.args[2]
        assert self._cap_instruction(audit_runner._DEFAULT_MAX_CITATIONS_PER_AC) in prompt
        assert "(minimum 1)" in prompt

    def test_configured_cap_value_reflected_in_prompt(self):
        """AC1/AC2: a configured cap value flows into the injected instruction."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call, mock.patch.object(
            audit_runner, "_load_config",
            return_value={"audit.max_citations_per_ac": 3},
        ):
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
        prompt = mock_call.call_args.args[2]
        assert self._cap_instruction(3) in prompt

    def test_legacy_prompt_structure_preserved(self):
        """AC3: the cap instruction is the only addition - legacy structure
        (header, FILE SCOPE, SCANNING block, criteria JSON, file:line
        guidance) survives verbatim, so no accidental restructuring."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC text")]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
        prompt = mock_call.call_args.args[2]
        assert "[READ-ONLY AUDIT] [PHASE 2 — DEEP CODE ANALYSIS] " in prompt
        assert "FILE SCOPE — Read ONLY the files listed in the manifest below" in prompt
        assert audit_runner._SCANNING_BLOCK in prompt
        assert "Provide a specific file:line reference" in prompt
        assert 'Criteria: [{"index": 0, "text": "Parent AC text"' in prompt

    def test_verdicts_and_evidence_never_mutated_by_cap(self):
        """AC4: the cap is prompt-level - parsed verdicts/evidence pass through
        unchanged (canonical report format preserved)."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "AC text")]
        deep_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:10"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=deep_result
        ):
            updated_acs, _children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "file.py:10"
        assert updated_acs[0]["text"] == "AC text"

class TestPhase2AcCountThreading:
    """F3-AC1/F4-AC1: the Phase-2 call sites pass the AC count so the
    per-call timing line can surface per-AC latency (LP-0MSQ32WM5000NCB7)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1", ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_parent_call_passes_ac_count(self):
        """AC1: the phase2_deep call passes ac_count=len(ac_results)."""
        issue = self._make_issue()
        acs = [self._make_ac(0), self._make_ac(1)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "test-model")
        parent_calls = [
            c for c in mock_call.call_args_list if c.args[1] == "phase2_deep"
        ]
        assert len(parent_calls) == 1
        assert parent_calls[0].kwargs.get("ac_count") == 2

    def test_child_call_passes_ac_count(self):
        """AC1: the phase2_child call passes ac_count=len(child_acs)."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", ac_count=3)
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(issue, acs, [child], "test-model")
        child_calls = [
            c for c in mock_call.call_args_list if c.args[1] == "phase2_child:0"
        ]
        assert len(child_calls) == 1
        assert child_calls[0].kwargs.get("ac_count") == 3

    def test_batch_call_passes_total_ac_count(self):
        """AC1: the phase2_batch call passes the total AC count (parent + children)."""
        issue = self._make_issue()
        acs = [self._make_ac(0), self._make_ac(1)]  # 2 parent ACs
        child = self._make_child("CHILD-1", ac_count=1)  # 1 child AC
        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p0"},
                {"index": 1, "verdict": "met", "evidence": "p1"},
                {"index": 2, "verdict": "met", "evidence": "c0"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result,
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", batch_phase2=True,
            )
        assert mock_call.call_count == 1
        context = mock_call.call_args.args[1]
        assert context == "phase2_batch"
        assert mock_call.call_args.kwargs.get("ac_count") == 3

class TestPhase2TimeoutHandling:
    """Tests for Phase 2 graceful timeout handling (AC1-AC3)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def test_timeout_marks_acs_as_partial(self):
        """AC1: When Phase 2 times out, all ACs are marked 'partial' with timeout evidence."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "AC1"), self._make_ac(1, "AC2")]

        timeout_result = {
            "verdict": "unmet",
            "evidence": "Pi model call timed out after 600s. Manual audit required.",
            "_timeout": True,
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=timeout_result,
        ):
            result = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        # The function now returns 3-tuple (acs, children, phase2_completed)
        updated_acs, _, phase2_completed = result

        assert phase2_completed is False
        for ac in updated_acs:
            assert ac["verdict"] == "partial"
            assert "timed out" in ac["evidence"].lower()

    def test_timeout_preserves_metadata(self):
        """AC2: On timeout, AC text is preserved and verdict is 'partial'."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Criterion 1"), self._make_ac(1, "Criterion 2", "unmet")]

        timeout_result = {
            "verdict": "unmet",
            "evidence": "Pi model call timed out after 600s.",
            "_timeout": True,
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=timeout_result,
        ):
            updated_acs, _, phase2_completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        assert phase2_completed is False
        for i, ac in enumerate(updated_acs):
            assert ac["text"] == acs[i]["text"]  # Original text preserved
            assert ac["verdict"] == "partial"

    def test_successful_return_still_works(self):
        """AC3: Successful Phase 2 still works correctly (backward compat).

        When no timeout occurs, the function should return phase2_completed=True
        and update ACs based on deep analysis results.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0, "AC1")]

        success_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
            "evidence": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value=success_result,
        ):
            updated_acs, _, phase2_completed = audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "file.py:10 works"

    def test_child_timeout_handled_gracefully(self):
        """AC4: Child deep analysis timeout marks child ACs as partial."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = {
            "id": "CHILD-1",
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC 1", "verdict": "met", "evidence": ""},
            ],
        }

        # First call (parent) succeeds, second call (child) times out
        parent_result = {
            "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
        }

        child_timeout = {
            "_timeout": True,
            "verdict": "unmet",
            "evidence": "timed out",
            "extracted_text": "",
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[parent_result, child_timeout],
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # Parent AC should still be updated (from the successful call)
        assert updated_acs[0]["verdict"] == "met"
        # Child AC should be marked partial due to timeout
        assert updated_children[0]["ac_results"][0]["verdict"] == "partial"
        assert "timed out" in updated_children[0]["ac_results"][0]["evidence"].lower()
        # Overall phase2_completed should be False
        assert phase2_completed is False

    def test_call_pi_timeout_constant_increased(self):
        """AC5: CALL_PI_TIMEOUT is increased to accommodate agent-mode Phase 2."""
        assert audit_runner.CALL_PI_TIMEOUT >= 1800, (
            f"CALL_PI_TIMEOUT ({audit_runner.CALL_PI_TIMEOUT}) should be at least 1800s "
            "for agent-mode Phase 2 deep analysis"
        )

class TestPhase2TimingInstrumentation:
    """Tests that Phase 2 parent and child calls emit per-call timings (AC4)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    stage: str = "in_progress",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_phase2_parent_and_child_emit_timing_lines(self, capsys):
        """AC4: phase2_deep and phase2_child calls emit per-call timing lines to stderr.

        Mocks ``_call_pi`` (so ``_call_pi_and_maybe_log`` runs for real) and
        runs ``_run_phase2_deep_analysis`` with one active child, then asserts
        the stderr output contains per-call timing lines for the parent
        (context ``phase2_deep``) and child (context ``phase2_child:0``) calls.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", ac_count=1)

        def _fake_call_pi(prompt, model="test-model", pi_bin="pi",
                          enable_tools=False, timeout=None, max_retries=None,
                          ac_fallback_used=None, child_screen=False,
                          issue_id="", context="", priority=None):
            return {
                "verdict": "met",
                "evidence": "file.py:10 works",
                "extracted_text": '[{"index": 0, "verdict": "met", "evidence": "file.py:10 works"}]',
                "elapsed_seconds": 3.75,
            }

        with mock.patch.object(audit_runner, "_call_pi", side_effect=_fake_call_pi):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(issue, acs, [child], "test-model")
            )

        captured = capsys.readouterr()
        assert "TEST-1" in captured.err
        assert "phase2_deep" in captured.err
        assert "CHILD-1" in captured.err
        assert "phase2_child:0" in captured.err
        assert "3.75" in captured.err
        # Behavior is unchanged: verdicts still flow through and phase2 completes.
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"

class TestPhase2FileScopeManifest:
    """Tests for the Phase 2 file-scope manifest (AC1-AC4).

    The Phase 2 prompt must include a file-scope manifest (Key Files + git
    changed files + repo index) and Phase 1 file:line evidence (P4) so the
    model verifies in-scope files instead of exploring the whole repo.
    """

    def _make_issue(self, issue_id: str = "TEST-1",
                    description: str = "") -> dict:
        return {"id": issue_id, "title": "Test Issue", "description": description}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met", evidence: str = "") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": evidence}

    def _make_git_runner(self, changed: list[str] | None = None,
                         index: list[str] | None = None):
        """Build a mock runner returning canned git outputs."""
        changed = changed or []
        index = index or ["skill/audit/scripts/audit_runner.py",
                          "skill/audit/tests/test_audit_runner.py",
                          "README.md"]
        mock_runner = mock.MagicMock()

        def _side_effect(cmd):
            cmd_str = " ".join(cmd)
            if "--name-only" in cmd_str:
                out = "\n".join(changed)
            elif "--porcelain=v1" in cmd_str:
                out = "\n".join(f" M {f}" for f in changed)
            elif "ls-files" in cmd_str:
                out = "\n".join(index)
            else:
                out = ""
            return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

        mock_runner.side_effect = _side_effect
        return mock_runner

    def _fake_call(self, captured):
        """Return a side_effect that matches _call_pi_and_maybe_log's signature."""
        def _side_effect(issue_id, context, prompt, model="m", pi_bin="pi",
                         debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                         ac_fallback_used=None, ac_count=None, priority=None):
            captured["prompt"] = prompt
            return {"extracted_text": "[]"}
        return _side_effect

    def test_prompt_includes_file_scope_manifest(self):
        """AC1/AC2: phase2_deep prompt includes a FILE SCOPE section."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        prompt = captured["prompt"]
        assert "FILE SCOPE" in prompt
        assert "in-scope" in prompt.lower() or "only the files" in prompt.lower()

    def test_prompt_includes_key_files_from_description(self):
        """AC1: Key Files extracted from the work item description appear in the prompt."""
        desc = (
            "## Summary\n\nThing.\n\n"
            "## Key Files (predicted)\n\n"
            "- `skill/audit/scripts/audit_runner.py` — primary\n"
            "- `skill/audit/tests/test_audit_runner.py` — tests\n"
        )
        issue = self._make_issue(description=desc)
        acs = [self._make_ac(0)]
        runner = self._make_git_runner()
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        prompt = captured["prompt"]
        assert "audit_runner.py" in prompt
        assert "test_audit_runner.py" in prompt

    def test_prompt_includes_changed_files_from_git(self):
        """AC1: git changed files appear in the Phase 2 prompt."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        assert "SKILL.md" in captured["prompt"]

    def test_prompt_includes_phase1_evidence_file_lines(self):
        """AC3 (P4): Phase 1 evidence file:line refs are fed forward."""
        issue = self._make_issue()
        acs = [self._make_ac(0, evidence="skill/audit/scripts/audit_runner.py:1608")]
        runner = self._make_git_runner()
        captured = {}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=self._fake_call(captured)):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model", runner=runner,
            )

        assert "audit_runner.py:1608" in captured["prompt"]

    def test_manifest_builder_returns_text(self):
        """AC1: _build_file_scope_manifest returns non-empty manifest text."""
        desc = "## Key Files\n\n- `skill/audit/scripts/audit_runner.py`\n"
        issue = self._make_issue(description=desc)
        acs = [self._make_ac(0, evidence="skill/audit/scripts/audit_runner.py:10")]
        runner = self._make_git_runner(changed=["skill/audit/SKILL.md"])

        manifest = audit_runner._build_file_scope_manifest(issue, acs, runner=runner)
        assert "audit_runner.py" in manifest
        assert "SKILL.md" in manifest
        assert "audit_runner.py:10" in manifest

    def test_manifest_builder_graceful_without_git(self):
        """AC4: manifest builder degrades gracefully when git fails."""
        issue = self._make_issue(description="")
        acs = [self._make_ac(0)]
        runner = mock.MagicMock()
        runner.side_effect = RuntimeError("git not available")

        manifest = audit_runner._build_file_scope_manifest(issue, acs, runner=runner)
        assert isinstance(manifest, str)
        assert manifest  # non-empty

    def test_child_prompt_includes_file_scope(self):
        """AC2: child deep-analysis prompts also carry the FILE SCOPE section."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = {
            "id": "CHILD-1",
            "title": "Child Issue",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met",
                 "evidence": "skill/audit/tests/test_audit_runner.py:1"},
            ],
        }
        runner = self._make_git_runner()
        prompts = []

        def _fake_call(issue_id, context, prompt, model="m", pi_bin="pi",
                       debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                       ac_fallback_used=None, ac_count=None, priority=None):
            prompts.append(prompt)
            return {"extracted_text": "[]"}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_fake_call):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model", runner=runner,
            )

        assert len(prompts) == 2
        assert "FILE SCOPE" in prompts[1]
        assert "test_audit_runner.py" in prompts[1]

class TestPhase2ChildVerdictReuse:
    """Tests for reusing fresh child audit verdicts in Phase 2 (AC1-AC4).

    When a child's own fresh audit produced a ready verdict
    (``child_audit_ready=True``), the parent Phase 2 must skip the duplicated
    child deep-analysis call and reuse the child's existing verdicts.
    """

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str = "CHILD-1",
                    child_audit_ready: bool = False,
                    stage: str = "plan_complete",
                    status: str = "open",
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": "Child Issue",
            "stage": stage,
            "status": status,
            "child_audit_ready": child_audit_ready,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_skips_deep_analysis_when_child_audit_ready(self):
        """AC1: no phase2_child call is made for a child_audit_ready=True child."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=True)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model",
                )
            )

        # Only the parent phase2_deep call should be made
        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert child_calls == []
        # Parent ACs still processed
        assert updated_acs[0]["verdict"] == "met"
        # Child AC results are preserved (reused), unchanged
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_runs_deep_analysis_when_child_audit_not_ready(self):
        """AC2: a child_audit_ready=False child still gets parent deep analysis."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1", child_audit_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) == 1

    def test_runs_deep_analysis_when_child_audit_ready_missing(self):
        """AC2 (guard): children without child_audit_ready default to analysis.

        Backward compatibility: existing callers that do not populate
        ``child_audit_ready`` must see unchanged behavior.
        """
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("CHILD-1")
        child.pop("child_audit_ready")  # Simulate pre-P2 callers

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        child_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "CHILD-1"
        ]
        assert len(child_calls) == 1

    def test_mixed_children_skip_only_ready_ones(self):
        """AC3: only child_audit_ready=True children are skipped in a mixed set."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        ready_child = self._make_child("READY-1", child_audit_ready=True)
        not_ready_child = self._make_child("PENDING-1", child_audit_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [ready_child, not_ready_child], "test-model",
            )

        ready_calls = [c for c in mock_call.call_args_list if c[0][0] == "READY-1"]
        pending_calls = [c for c in mock_call.call_args_list if c[0][0] == "PENDING-1"]
        assert ready_calls == []
        assert len(pending_calls) == 1

    def test_completed_done_child_still_skipped(self):
        """AC3 (guard): completed/done children remain exempt regardless of flag."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        child = self._make_child("DONE-1", child_audit_ready=False,
                                 stage="done", status="completed")

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        done_calls = [c for c in mock_call.call_args_list if c[0][0] == "DONE-1"]
        assert done_calls == []

    def test_stale_child_audit_maps_to_not_ready(self):
        """AC5 (SA-0MSGAU5RZ00137JZ): a stale child audit is detected as
        stale by _get_child_audit_verdict (audit older than updatedAt +
        freshness buffer)."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        stale_audit_time = (now - timedelta(seconds=600)).isoformat().replace(
            "+00:00", "Z"
        )
        updated_time = now.isoformat().replace("+00:00", "Z")

        def _fake_run_wl(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "auditedAt": stale_audit_time,
                        "rawOutput": "Ready to close: Yes\n...",
                    },
                }
            if "wl show" in cmd_str:
                return {
                    "success": True,
                    "workItem": {"id": "STALE-1", "updatedAt": updated_time},
                }
            raise AssertionError(f"unexpected wl command: {cmd_str}")

        with mock.patch.object(
            audit_runner, "_run_wl", side_effect=_fake_run_wl
        ) as mock_wl:
            verdict, reason, audited_at = audit_runner._get_child_audit_verdict(
                mock.MagicMock(), "STALE-1"
            )

        assert verdict is None
        assert reason == "stale"
        assert audited_at == stale_audit_time
        # The freshness check queried both the audit and the work item
        assert mock_wl.call_count == 2

    def test_stale_child_receives_phase2_analysis(self):
        """AC5 (SA-0MSGAU5RZ00137JZ): a child whose audit is stale maps to
        child_audit_ready=False (same code path as not-ready), so the parent
        Phase 2 loop still deep-analyzes it (phase2_child call runs)."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        # Stale audits resolve to child_audit_ready=False in cmd_issue
        # (audit_runner.py:3429) — exercise that mapping here.
        child = self._make_child("STALE-1", child_audit_ready=False)

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )

        stale_calls = [
            c for c in mock_call.call_args_list if c[0][0] == "STALE-1"
        ]
        assert len(stale_calls) == 1

class TestPhase2ParallelChildCalls:
    """Tests for bounded-concurrency parallel child deep analysis (AC1-AC4)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                 verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str, child_audit_ready: bool = False,
                    ac_count: int = 1) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "plan_complete",
            "status": "open",
            "child_audit_ready": child_audit_ready,
            "ac_results": [
                {"index": i, "text": f"Child AC {i}", "verdict": "met", "evidence": ""}
                for i in range(ac_count)
            ],
        }

    def test_parallel_children_processed_concurrently(self):
        """AC1: multiple child calls run concurrently (not sequentially).

        Uses a real ThreadPoolExecutor with a mock Pi call that blocks on a
        barrier; if calls were sequential, a 2-child run with a 2-worker pool
        would serialize and take ~2x the per-call time.
        """
        import threading
        import time as _time

        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [
            self._make_child("C-1"),
            self._make_child("C-2"),
        ]

        started = threading.Barrier(2)  # both child calls must be in-flight to pass

        def _slow_call(issue_id, context, prompt, model="m", pi_bin="pi",
                       debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                       ac_fallback_used=None, ac_count=None, priority=None):
            if context.startswith("phase2_child"):
                started.wait(timeout=5)  # raises BrokenBarrierError if not concurrent
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_slow_call):
            _t0 = _time.monotonic()
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )
            _elapsed = _time.monotonic() - _t0

        # Both calls completed without deadlock/timeout
        assert phase2_completed is True
        assert len(updated_children) == 2
        # If sequential, elapsed >= 2x barrier overhead; concurrency proves
        # the two calls ran in parallel (barrier would have thrown otherwise).
        assert _elapsed < 10

    def test_sequential_when_parallelism_disabled(self):
        """AC2/fallback: parallelism=1 runs child calls sequentially."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [self._make_child("C-1"), self._make_child("C-2")]
        call_order: list[str] = []

        def _ordered_call(issue_id, context, prompt, model="m", pi_bin="pi",
                          debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                          ac_fallback_used=None, ac_count=None, priority=None):
            if context.startswith("phase2_child"):
                call_order.append(issue_id)
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "1"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_ordered_call):
            _updated_acs, _updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert call_order == ["C-1", "C-2"]  # strictly sequential order
        assert phase2_completed is True

    def test_default_parallelism_cap(self):
        """AC1: a sensible default bounded concurrency cap exists."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            cap = audit_runner._resolve_parallelism()
        assert isinstance(cap, int)
        assert 1 <= cap <= 4

    def test_parallelism_env_var_respected(self):
        """AC1: env var sets the concurrency cap."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "3"},
            clear=False,
        ):
            assert audit_runner._resolve_parallelism() == 3

    def test_invalid_parallelism_env_falls_back(self):
        """AC2: invalid env value falls back to the default cap."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "banana"},
            clear=False,
        ):
            cap = audit_runner._resolve_parallelism()
        assert isinstance(cap, int)
        assert 1 <= cap <= 4

    def test_legacy_alias_fallback(self):
        """Legacy AUDIT_PHASE2_PARALLELISM is honored when AUDIT_PARALLELISM is unset."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_PARALLELISM_ENV_LEGACY: "3"},
            clear=False,
        ):
            # Ensure the new name is not set
            assert audit_runner.AUDIT_PARALLELISM_ENV not in audit_runner.os.environ or audit_runner.os.environ.get(audit_runner.AUDIT_PARALLELISM_ENV) is None
            cap = audit_runner._resolve_parallelism()
        assert cap == 3

    def test_new_name_takes_precedence_over_legacy(self):
        """AUDIT_PARALLELISM wins when both are set."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {
                audit_runner.AUDIT_PARALLELISM_ENV: "4",
                audit_runner.AUDIT_PHASE2_PARALLELISM_ENV_LEGACY: "3",
            },
            clear=False,
        ):
            cap = audit_runner._resolve_parallelism()
        assert cap == 4

    def test_ready_children_skipped_in_parallel_run(self):
        """AC3: child_audit_ready children are skipped even when parallelism > 1."""
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [
            self._make_child("READY-1", child_audit_ready=True),
            self._make_child("PENDING-1"),
        ]
        call_ids: list[str] = []

        def _recording_call(issue_id, context, prompt, model="m", pi_bin="pi",
                            debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                            ac_fallback_used=None, ac_count=None, priority=None):
            call_ids.append(issue_id)
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_recording_call):
            audit_runner._run_phase2_deep_analysis(
                issue, acs, children, "test-model",
            )

        assert "READY-1" not in call_ids
        assert "PENDING-1" in call_ids

    def test_timeout_child_marks_partial_in_parallel_run(self):
        """AC4: a child timeout still marks partial ACs and phase2_completed=False."""

        issue = self._make_issue()
        acs = [self._make_ac(0)]
        children = [self._make_child("C-1"), self._make_child("C-2")]

        def _call_with_timeout(issue_id, context, prompt, model="m", pi_bin="pi",
                               debug_log=None, enable_tools=False, timeout=None, max_retries=None,
                               ac_fallback_used=None, ac_count=None, priority=None):
            if issue_id == "C-1":
                return {"_timeout": True, "verdict": "unmet",
                        "evidence": "timed out", "extracted_text": ""}
            return {"extracted_text": "[]"}

        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PARALLELISM_ENV: "2"},
            clear=False,
        ), mock.patch.object(audit_runner, "_call_pi_and_maybe_log",
                               side_effect=_call_with_timeout):
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert phase2_completed is False
        timeout_child = next(c for c in updated_children if c["id"] == "C-1")
        assert timeout_child["ac_results"][0]["verdict"] == "partial"

class TestPhase2RetryTuning:
    """Tests for bounded provider-error retries on long Phase 2 calls (AC1-AC4).

    Long agent-mode Phase 2 calls (phase2_deep / phase2_child) must NOT
    restart the entire call on provider error beyond a bounded retry cap
    (1, per the performance evaluation). Short Phase 1 bare calls keep the
    existing ``_PI_MAX_RETRIES`` behavior.
    """

    def _make_provider_error_stream(self) -> str:
        return json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "stopReason": "error", "errorMessage": "boom"}],
        })

    def _make_valid_stream(self) -> str:
        return json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": '{"verdict": "met", "evidence": "file.py:1"}'}],
        })

    def test_phase2_deep_uses_reduced_retry_cap(self):
        """AC1: phase2_deep calls pass a reduced retry cap (1)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-1", "phase2_deep", "prompt",
                model="m", enable_tools=True, max_retries=1,
            )
        _args, kwargs = mock_call.call_args
        assert kwargs.get("max_retries") == 1

    def test_phase2_child_uses_reduced_retry_cap(self):
        """AC1: phase2_child calls pass a reduced retry cap (1)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-2", "phase2_child:0", "prompt",
                model="m", enable_tools=True, max_retries=1,
            )
        _args, kwargs = mock_call.call_args
        assert kwargs.get("max_retries") == 1

    def test_default_retries_unchanged_for_phase1(self):
        """AC3: Phase 1 bare calls keep the default _PI_MAX_RETRIES (2)."""
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._call_pi_and_maybe_log(
                "SA-3", "parent", "prompt", model="m",
            )
        _args, kwargs = mock_call.call_args
        # max_retries not passed → _call_pi uses its default
        assert kwargs.get("max_retries") is None

    def test_call_pi_retries_bounded_by_max_retries(self):
        """AC1/AC2: _call_pi with max_retries=1 makes at most 2 attempts on provider error."""
        provider_stream = self._make_provider_error_stream()
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (provider_stream, "")

        with mock.patch.object(audit_runner.subprocess, "Popen",
                               return_value=mock_process) as mock_popen:
            result = audit_runner._call_pi(
                "prompt", model="m", max_retries=1,
            )

        # 1 initial attempt + 1 retry = 2 attempts, then provider error surfaced
        assert mock_popen.call_count == 2
        assert result.get("_provider_error") is True

    def test_call_pi_default_retries_full_budget(self):
        """AC3: default (no max_retries) keeps _PI_MAX_RETRIES=2 extra attempts."""
        provider_stream = self._make_provider_error_stream()
        mock_process = mock.MagicMock()
        mock_process.communicate.return_value = (provider_stream, "")

        with mock.patch.object(audit_runner.subprocess, "Popen",
                               return_value=mock_process) as mock_popen:
            result = audit_runner._call_pi("prompt", model="m")

        # 1 initial + 2 retries = 3 attempts
        assert mock_popen.call_count == 3
        assert result.get("_provider_error") is True

    def test_phase2_deep_analysis_forwards_reduced_retries(self):
        """AC1 (integration): _run_phase2_deep_analysis forwards max_retries=1."""
        issue = {"id": "TEST-1", "title": "Test"}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            return_value={"extracted_text": "[]"},
        ) as mock_call:
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [], "test-model",
            )

        parent_calls = [
            c for c in mock_call.call_args_list
            if c[0][1] == "phase2_deep"
        ]
        assert len(parent_calls) == 1
        _args, kwargs = parent_calls[0]
        assert kwargs.get("max_retries") == 1

    def test_deep_analyze_child_forwards_reduced_retries(self):
        """AC4 (SA-0MSGAUD3H007P8Y4): the production phase2_child path
        (_deep_analyze_child) forwards max_retries=_PHASE2_MAX_RETRIES to
        _call_pi_and_maybe_log."""
        child = {
            "id": "C-1",
            "title": "Child",
            "ac_results": [
                {"index": 0, "text": "AC", "verdict": "met", "evidence": ""}
            ],
        }
        result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"}
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=result
        ) as mock_call:
            _ci, _updated_child, _timeout = audit_runner._deep_analyze_child(
                0, child, "test-model", "pi", None, None, mock.MagicMock()
            )

        _args, kwargs = mock_call.call_args
        assert kwargs.get("max_retries") == audit_runner._PHASE2_MAX_RETRIES
        assert audit_runner._PHASE2_MAX_RETRIES == 1

    def test_call_pi_timeout_default_exactly_1800(self):
        """AC5 (SA-0MSGAUD3H007P8Y4): the default CALL_PI_TIMEOUT remains
        exactly 1800s (operator overrides via --timeout/AUDIT_PI_TIMEOUT are
        intentional and documented in SKILL.md)."""
        assert audit_runner.CALL_PI_TIMEOUT == 1800

    def test_child_provider_error_degrades_to_partial(self):
        """AC2 (child path): a provider error in phase2_child degrades ACs.

        Mirrors the parent phase2_deep path: on _provider_error the child's
        ACs must be marked partial (not left at Phase 1 verdicts), and
        phase2_completed must be False so the audit is not reported as fully
        deep-verified.
        """
        issue = {"id": "TEST-1", "title": "Test"}
        acs = [{"index": 0, "text": "AC", "verdict": "met", "evidence": ""}]
        children = [{
            "id": "CHILD-1",
            "title": "Child",
            "ac_results": [
                {"index": 0, "text": "AC1", "verdict": "met",
                 "evidence": "phase1"}
            ],
        }]

        def _provider_error_call(issue_id, context, prompt, model="m",
                                 pi_bin="pi", debug_log=None,
                                 enable_tools=False, timeout=None,
                                 max_retries=None, ac_fallback_used=None, ac_count=None, priority=None):
            if context.startswith("phase2_child"):
                return {
                    "verdict": "unmet",
                    "evidence": "Pi provider error: finish_reason: error",
                    "extracted_text": "",
                    "_provider_error": True,
                    "_provider_error_message": "finish_reason: error",
                }
            return {"extracted_text": "[]"}

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=_provider_error_call,
        ):
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model",
                )
            )

        assert phase2_completed is False
        child = updated_children[0]
        assert child["id"] == "CHILD-1"
        assert child["ac_results"][0]["verdict"] == "partial"
        assert "provider error" in child["ac_results"][0]["evidence"].lower()

class TestPhase2ScanningGuidance:
    """Phase 2 prompts contain scanning guidance (scan.py + no unbounded grep)."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _capture_prompt(self, children: list[dict] | None = None,
                        ac_count: int = 1) -> str:
        """Run _run_phase2_deep_analysis and return the parent prompt text."""
        issue = self._make_issue()
        acs = [self._make_ac(i) for i in range(ac_count)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, children or [], "test-model",
            )
        parent_call = [
            call for call in mock_call.call_args_list
            if call[0][1] == "phase2_deep"
        ]
        assert parent_call
        return parent_call[0][0][2]  # prompt is the 3rd positional arg

    def test_parent_prompt_references_scan_helper(self) -> None:
        """The parent phase2_deep prompt references scan.py."""
        prompt = self._capture_prompt()
        assert "scan.py" in prompt

    def test_parent_prompt_forbids_unbounded_recursive_grep(self) -> None:
        """The parent prompt forbids unbounded grep -r over repo root."""
        prompt = self._capture_prompt()
        assert "grep -r" in prompt
        assert "unbounded" in prompt

    def test_child_prompt_references_scan_helper(self) -> None:
        """The child phase2_child prompt references scan.py."""
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )
        child_call = [
            call for call in mock_call.call_args_list
            if call[0][1].startswith("phase2_child")
        ]
        assert child_call
        prompt = child_call[0][0][2]
        assert "scan.py" in prompt

    def test_child_prompt_forbids_repo_root_scan(self) -> None:
        """The child prompt forbids unbounded repo-root exploration."""
        child = {
            "id": "CHILD-1", "title": "Child", "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": ""}
            ],
        }
        issue = self._make_issue()
        acs = [self._make_ac(0)]
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log"
        ) as mock_call:
            mock_call.return_value = {"extracted_text": "[]"}
            audit_runner._run_phase2_deep_analysis(
                issue, acs, [child], "test-model",
            )
        child_call = [
            call for call in mock_call.call_args_list
            if call[0][1].startswith("phase2_child")
        ]
        prompt = child_call[0][0][2]
        assert "grep" in prompt
        assert "unbounded" in prompt or "explore the whole repository" in prompt

class TestPhase2BatchResolution:
    """Tests for batch-mode enablement (env var / default)."""

    def test_env_constant_defined(self):
        """AC1: The AUDIT_PHASE2_BATCH env var constant is defined."""
        assert audit_runner.AUDIT_PHASE2_BATCH_ENV == "AUDIT_PHASE2_BATCH"

    def test_default_disabled(self):
        """AC1/AC5: Batching is off by default (existing N+1 path preserved)."""
        with mock.patch.dict(audit_runner.os.environ, {}, clear=True):
            assert audit_runner._phase2_batch_enabled(None) is False

    def test_env_enables(self):
        """AC1: AUDIT_PHASE2_BATCH=1 enables batching."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_BATCH_ENV: "1"},
            clear=False,
        ):
            assert audit_runner._phase2_batch_enabled(None) is True

    def test_cli_flag_wins_over_env(self):
        """AC1: Explicit --batch-phase2 flag overrides a disabled env value."""
        with mock.patch.dict(
            audit_runner.os.environ,
            {audit_runner.AUDIT_PHASE2_BATCH_ENV: "0"},
            clear=False,
        ):
            assert audit_runner._phase2_batch_enabled(True) is True

class TestCanonicalScanningBlock:
    """SA-0MSL1Z6M7002EDS0: a single canonical SCANNING block constant is
    reused in every audit prompt — no inline SCANNING strings remain.

    The fuller version (with ``list-files`` and the single-file ``.worklog``
    grep note) is defined once and interpolated at every prompt site:
    Phase 1 parent, Phase 1 child, Phase 2 parent, Phase 2 child, Phase 2
    batch.
    """

    def test_no_inline_scanning_literals_outside_constant(self):
        """AC1: the SCANNING text appears in the runner only as the constant
        definition (once); every prompt site interpolates it."""
        src = Path(audit_runner.__file__).read_text()
        inline = re.findall(r'"SCANNING — When you need to look something up', src)
        assert len(inline) == 1, \
            f"inline SCANNING string literals: {inline}"

    def test_canonical_block_used_at_every_prompt_site(self):
        """AC1: the constant is referenced at all 5 prompt sites."""
        src = Path(audit_runner.__file__).read_text()
        refs = re.findall(r'\{_SCANNING_BLOCK\}', src)
        assert len(refs) == 5, f"prompt-site references: {refs}"

    def test_canonical_block_is_the_full_version(self):
        """The canonical block carries list-files and the .worklog grep note."""
        block = audit_runner._SCANNING_BLOCK
        assert "list-files" in block
        assert "never `grep -r` over .worklog/" in block


class TestPhase2BatchRouting:
    """AC1/AC2/AC4: Batch mode folds parent + pending child ACs into one
    indexed call and routes results back to the correct lists."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text",
                  verdict: str = "met") -> dict:
        return {"index": index, "text": text, "verdict": verdict, "evidence": ""}

    def _make_child(self, child_id: str, ac_text: str = "Child AC") -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": ac_text, "verdict": "met", "evidence": "phase1"},
            ],
        }

    def test_single_batch_call_covers_parent_and_child(self):
        """AC1: One phase2_batch call replaces the parent + child calls."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1", "Child AC 1")

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "parent file.py:1"},
                {"index": 1, "verdict": "unmet", "evidence": "child file.py:2"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        # Exactly one call, batched
        assert mock_call.call_count == 1
        context = mock_call.call_args.args[1]
        assert context == "phase2_batch"
        prompt = mock_call.call_args.args[2]
        assert "Parent AC" in prompt
        assert "Child AC 1" in prompt

        # Routing: parent AC got its verdict, child AC got its own
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "parent file.py:1"
        assert updated_children[0]["ac_results"][0]["verdict"] == "unmet"
        assert "Phase 1" in updated_children[0]["ac_results"][0]["evidence"]
        assert "child file.py:2" in updated_children[0]["ac_results"][0]["evidence"]

    def test_index_routing_multiple_children(self):
        """AC2: Results route per-child by index offset."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC"), self._make_ac(1, "Parent AC 2")]
        children = [
            self._make_child("C-1", "C1 AC"),
            self._make_child("C-2", "C2 AC"),
        ]

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p0"},
                {"index": 1, "verdict": "adjusted", "evidence": "p1"},
                {"index": 2, "verdict": "unmet", "evidence": "c1"},
                {"index": 3, "verdict": "met", "evidence": "c2"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, children, "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[1]["verdict"] == "adjusted"
        assert updated_children[0]["ac_results"][0]["verdict"] == "unmet"
        assert updated_children[1]["ac_results"][0]["verdict"] == "met"

    def test_batch_skips_done_and_ready_children(self):
        """AC1: completed/done and child_audit_ready children are not batched."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        done_child = self._make_child("DONE-1", "done AC")
        done_child["status"] = "completed"
        done_child["stage"] = "done"
        ready_child = self._make_child("READY-1", "ready AC")
        ready_child["child_audit_ready"] = True
        pending_child = self._make_child("PEND-1", "pending AC")

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "p"},
                {"index": 1, "verdict": "met", "evidence": "c"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ) as mock_call:
            _updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs,
                    [done_child, ready_child, pending_child],
                    "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        prompt = mock_call.call_args.args[2]
        assert "done AC" not in prompt
        assert "ready AC" not in prompt
        assert "pending AC" in prompt
        # Skipped children keep their Phase 1 results unchanged
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert updated_children[1]["ac_results"][0]["verdict"] == "met"
        assert updated_children[2]["ac_results"][0]["verdict"] == "met"

    def test_batch_verdict_semantics_unchanged(self):
        """AC4: Phase 1 met + Phase 2 met -> met; Phase 1 met + Phase 2 disagree -> downgrade."""
        issue = self._make_issue()
        acs = [
            {"index": 0, "text": "AC1", "verdict": "met", "evidence": "p1"},
            {"index": 1, "text": "AC2", "verdict": "met", "evidence": "p1"},
        ]

        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "deep ok"},
                {"index": 1, "verdict": "unmet", "evidence": "deep disagree"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, _, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [], "test-model", batch_phase2=True,
                )
            )

        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == "met"
        assert updated_acs[0]["evidence"] == "deep ok"
        assert updated_acs[1]["verdict"] == "unmet"
        assert "Phase 1" in updated_acs[1]["evidence"]
        assert "deep disagree" in updated_acs[1]["evidence"]

class TestPhase2BatchFallback:
    """AC3: Batch failure/timeout falls back to per-child calls."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text") -> dict:
        return {"index": index, "text": text, "verdict": "met", "evidence": ""}

    def _make_child(self, child_id: str) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": "phase1"},
            ],
        }

    def _run_with_side_effects(self, side_effects):
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        return audit_runner._run_phase2_deep_analysis(
            issue, acs, [child], "test-model", batch_phase2=True,
        ), audit_runner._call_pi_and_maybe_log

    def test_batch_timeout_falls_back_to_per_child(self):
        """AC3: A batch timeout falls back to the existing per-child path."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        timeout_result = {
            "_timeout": True,
            "verdict": "unmet",
            "evidence": "timed out",
            "extracted_text": "",
        }
        success_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        # Batch call (timeout) + fallback parent call + fallback child call
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[timeout_result, success_result, success_result],
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        # Batch call + fallback per-child call both happened
        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert any(ctx == "phase2_child:0" for ctx in contexts[1:])
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_runtime_error_falls_back(self):
        """AC3: A batch RuntimeError falls back to the per-child path."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        def _side_effect(issue_id, context, prompt, **kwargs):
            if context == "phase2_batch":
                raise RuntimeError("batch failed")
            return {
                "extracted_text": json.dumps([
                    {"index": 0, "verdict": "met", "evidence": "file.py:1"},
                ]),
            }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", side_effect=_side_effect
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert "phase2_deep" in contexts
        assert "phase2_child:0" in contexts
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_disabled_uses_existing_path(self):
        """AC5: With batching disabled the existing parent + child calls run."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        success = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=success
        ) as mock_call:
            updated_acs, _updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=False,
                )
            )

        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert "phase2_batch" not in contexts
        assert "phase2_deep" in contexts
        assert "phase2_child:0" in contexts
        assert updated_acs[0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_unparseable_output_falls_back(self):
        """AC3 (SA-0MSGATYJR006XMIJ): Unparseable batch output must fall
        back to the per-child path instead of silently succeeding with an
        empty reviewed map."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        unparseable_result = {
            "extracted_text": "I could not produce JSON: the model output was garbled.",
            "evidence": "",
            "text": "",
        }
        success_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        # Batch call (unparseable) + fallback parent call + fallback child call
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[unparseable_result, success_result, success_result],
        ) as mock_call:
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        # The batch call was attempted first, then the per-child fallback ran
        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert "phase2_deep" in contexts[1:]
        assert "phase2_child:0" in contexts[1:]
        # Verdicts come from the fallback deep analysis, not a silent empty map
        assert updated_acs[0]["verdict"] == "met"
        assert updated_children[0]["ac_results"][0]["verdict"] == "met"
        assert phase2_completed is True

    def test_batch_empty_array_falls_back(self):
        """AC3 (SA-0MSGATYJR006XMIJ): An empty batch array (no usable items)
        must also fall back to the per-child path."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")

        empty_result = {
            "extracted_text": json.dumps([]),
            "evidence": "",
            "text": "",
        }
        success_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }

        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[empty_result, success_result, success_result],
        ) as mock_call:
            updated_acs, _updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )

        contexts = [c.args[1] for c in mock_call.call_args_list]
        assert contexts[0] == "phase2_batch"
        assert "phase2_deep" in contexts[1:]
        assert "phase2_child:0" in contexts[1:]
        assert updated_acs[0]["verdict"] == "met"
        assert phase2_completed is True

class TestPhase2NormalizesDeepVerdicts:
    """AC3: Phase 2 deep-analysis verdicts are normalized before merge."""

    def _make_issue(self, issue_id: str = "TEST-1") -> dict:
        return {"id": issue_id, "title": "Test Issue"}

    def _make_ac(self, index: int, text: str = "AC text") -> dict:
        return {"index": index, "text": text, "verdict": "met", "evidence": ""}

    def _make_child(self, child_id: str) -> dict:
        return {
            "id": child_id,
            "title": f"Child {child_id}",
            "stage": "in_progress",
            "status": "open",
            "ac_results": [
                {"index": 0, "text": "Child AC", "verdict": "met", "evidence": "phase1"},
            ],
        }

    def test_batch_path_normalizes_pass_to_met(self):
        """A batch deep verdict of 'pass' merges as 'met' (met+pass -> met)."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "parent file.py:1"},
                {"index": 1, "verdict": "pass", "evidence": "child file.py:2"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=True,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_children[0]["ac_results"][0]["verdict"] == audit_runner.VERDICT_MET

    def test_per_child_path_normalizes_pass_to_met(self):
        """The per-child deep path (phase2_child) normalizes 'pass' to 'met'."""
        issue = self._make_issue()
        acs = [self._make_ac(0, "Parent AC")]
        child = self._make_child("C-1")
        success = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }
        child_pass = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "child file.py:2"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log",
            side_effect=[success, child_pass],
        ):
            updated_acs, updated_children, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [child], "test-model", batch_phase2=False,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_children[0]["ac_results"][0]["verdict"] == audit_runner.VERDICT_MET

    def test_batch_pass_downgrade_still_applies(self):
        """AC3: Phase 1 met + deep 'pass' stays met, but deep 'unmet' still downgrades."""
        issue = self._make_issue()
        acs = [
            {"index": 0, "text": "AC1", "verdict": "met", "evidence": "p1"},
            {"index": 1, "text": "AC2", "verdict": "met", "evidence": "p1"},
        ]
        batch_result = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "pass", "evidence": "deep ok"},
                {"index": 1, "verdict": "unmet", "evidence": "deep fail"},
            ]),
        }
        with mock.patch.object(
            audit_runner, "_call_pi_and_maybe_log", return_value=batch_result
        ):
            updated_acs, _, phase2_completed = (
                audit_runner._run_phase2_deep_analysis(
                    issue, acs, [], "test-model", batch_phase2=True,
                )
            )
        assert phase2_completed is True
        assert updated_acs[0]["verdict"] == audit_runner.VERDICT_MET
        assert updated_acs[1]["verdict"] == audit_runner.VERDICT_UNMET

