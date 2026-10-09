"""Tiered Phase 1 model tests (SA-0MSKB697P000T3HG) — re-scoped 2026-10-09.

Covers the surviving *programmatic* tiering. The original config-file /
``--phase1-model`` CLI mechanism this feature once shipped was removed as
dead code in SA-0MUQ0I0YY000JCB5 (commit 7660e441) because the
``.ralph.json`` config system it relied on never existed. Per the producer's
re-scope decision (option A), these tests cover:

- AC4: safe default — when a distinct fast Phase 1 model cannot produce
  reliable batched verdict JSON, the SAME Phase 1 screen retries once with
  the full model before degrading to ``partial``.
- AC2/AC5: per-phase model threading — Phase 1 parent + child AC screening
  use ``ctx.resolved_phase1_model``; Phase 2 deep analysis keeps the full
  ``resolved_model``.
- AC1: a programmatic ``phase1_model`` argument is threaded to Phase 1 while
  Phase 2 keeps the full model, end-to-end through ``cmd_issue``.
- AC2/AC3: the per-call timing line surfaces the serving ``model=<name>``.
"""

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
from audit.tests.wl_helpers import stateful_wl_side_effect


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore (same pattern as the other
    audit test modules — SA-0MSCDC4750019G9Y)."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


def _make_ctx(**overrides) -> audit_runner._AuditContext:
    """A minimal ``_AuditContext`` with defaults for Phase-1 unit tests."""
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
    ctx.acs = ["AC one"]
    ctx.work_item = {
        "id": "TEST-1", "title": "T",
        "description": "## Acceptance Criteria\n1. AC one\n",
    }
    ctx.ac_results = []
    ctx.owning_root = "."
    ctx.resolved_model = "full-model"
    ctx.resolved_phase1_model = "fast-model"
    return ctx


def _fake_screen_factory(calls, fast_batch, full_batch):
    """Build a ``_call_phase1_screen`` fake recording the model per call.

    *fast_batch* / *full_batch* are ``(result, batch, raw_text)`` tuples.
    """

    def _fake(issue_id, context, prompt, model, pi_bin, debug_log, timeout,
              ac_fallback_used, on_runtime_error, failure_label,
              child_screen=False, enable_tools=True, priority=None):
        calls.append({"context": context, "model": model})
        if model != "full-model":
            return fast_batch
        return full_batch

    return _fake


# ---------------------------------------------------------------------------
# AC4 — safe default: fast-model failure falls back to the full model
# ---------------------------------------------------------------------------


class TestPhase1FullModelFallback:
    """When the fast Phase 1 model cannot produce reliable batched verdict
    JSON, Phase 1 retries once with the full audit model (AC4)."""

    def test_parent_screen_retries_with_full_model_on_unparseable(self):
        ctx = _make_ctx()
        ctx.acs = ["AC one", "AC two"]
        calls: list[dict] = []
        full_batch = [
            {"index": 0, "verdict": "met", "evidence": "a.py:1"},
            {"index": 1, "verdict": "met", "evidence": "b.py:2"},
        ]
        fake = _fake_screen_factory(
            calls,
            fast_batch=(
                {"verdict": "unmet", "evidence": "", "extracted_text": "no json"},
                [], "no json",
            ),
            full_batch=(
                {"verdict": "met", "evidence": "", "extracted_text": json.dumps(full_batch)},
                full_batch, json.dumps(full_batch),
            ),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"), \
                mock.patch.object(audit_runner, "_validate_file_scope_manifest", return_value=None), \
                mock.patch.object(audit_runner, "_resolve_touched_files", return_value=[]):
            audit_runner._phase1_parent_screening(ctx)

        assert [c["model"] for c in calls] == ["fast-model", "full-model"]
        assert [r["verdict"] for r in ctx.ac_results] == ["met", "met"]

    def test_parent_screen_single_call_when_fast_model_ok(self):
        ctx = _make_ctx()
        calls: list[dict] = []
        batch = [{"index": 0, "verdict": "met", "evidence": "a.py:1"}]
        fake = _fake_screen_factory(
            calls,
            fast_batch=({"verdict": "met", "evidence": "", "extracted_text": json.dumps(batch)}, batch, "raw"),
            full_batch=(None, None, None),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"), \
                mock.patch.object(audit_runner, "_validate_file_scope_manifest", return_value=None), \
                mock.patch.object(audit_runner, "_resolve_touched_files", return_value=[]):
            audit_runner._phase1_parent_screening(ctx)

        assert [c["model"] for c in calls] == ["fast-model"]
        assert ctx.ac_results[0]["verdict"] == "met"

    def test_no_retry_when_phase1_equals_full_model(self):
        """No distinct phase-1 model → legacy single-call path preserved;
        unparseable output degrades to diagnostic 'partial'."""
        ctx = _make_ctx()
        ctx.resolved_phase1_model = "same-model"
        ctx.resolved_model = "same-model"
        calls: list[dict] = []
        fake = _fake_screen_factory(
            calls,
            fast_batch=({"verdict": "unmet", "evidence": "", "extracted_text": ""}, [], ""),
            full_batch=(None, None, None),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"), \
                mock.patch.object(audit_runner, "_validate_file_scope_manifest", return_value=None), \
                mock.patch.object(audit_runner, "_resolve_touched_files", return_value=[]):
            audit_runner._phase1_parent_screening(ctx)

        assert [c["model"] for c in calls] == ["same-model"]
        assert ctx.ac_results[0]["verdict"] == "partial"

    def test_child_screen_retries_with_full_model_on_unparseable(self):
        child = {
            "id": "CHILD-1", "title": "Child Issue",
            "description": "## Acceptance Criteria\n1. CAC one\n",
        }
        calls: list[dict] = []
        full_batch = [{"index": 0, "verdict": "met", "evidence": "a.py:1"}]
        fake = _fake_screen_factory(
            calls,
            fast_batch=({"verdict": "unmet", "evidence": "", "extracted_text": "x"}, [], "x"),
            full_batch=({"verdict": "met", "evidence": "", "extracted_text": json.dumps(full_batch)}, full_batch, "raw"),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"):
            ci, acs = audit_runner._phase1_review_child_acs(
                0, child, "fast-model", "full-model", "pi", None, None,
                mock.MagicMock(), lambda *a, **k: None,
            )

        assert ci == 0
        assert [c["model"] for c in calls] == ["fast-model", "full-model"]
        assert acs[0]["verdict"] == "met"

    def test_child_screen_no_retry_when_phase1_equals_full_model(self):
        """No distinct phase-1 model → child screen is a single call (no-op
        retry)."""
        child = {
            "id": "CHILD-1", "title": "Child Issue",
            "description": "## Acceptance Criteria\n1. CAC one\n",
        }
        calls: list[dict] = []
        fake = _fake_screen_factory(
            calls,
            fast_batch=({"verdict": "unmet", "evidence": "", "extracted_text": ""}, [], ""),
            full_batch=(None, None, None),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"):
            _, acs = audit_runner._phase1_review_child_acs(
                0, child, "same-model", "same-model", "pi", None, None,
                mock.MagicMock(), lambda *a, **k: None,
            )

        assert [c["model"] for c in calls] == ["same-model"]
        assert acs[0]["verdict"] == "partial"


# ---------------------------------------------------------------------------
# AC2/AC5 — per-phase model threading
# ---------------------------------------------------------------------------


class TestPerPhaseModelThreading:
    """Phase 1 screening uses the fast model; Phase 2 keeps the full model."""

    def test_parent_screening_passes_phase1_model_to_pi(self):
        ctx = _make_ctx()
        calls: list[dict] = []
        batch = [{"index": 0, "verdict": "met", "evidence": "a.py:1"}]
        fake = _fake_screen_factory(
            calls,
            fast_batch=({"verdict": "met", "evidence": "", "extracted_text": json.dumps(batch)}, batch, "raw"),
            full_batch=(None, None, None),
        )
        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"), \
                mock.patch.object(audit_runner, "_validate_file_scope_manifest", return_value=None), \
                mock.patch.object(audit_runner, "_resolve_touched_files", return_value=[]):
            audit_runner._phase1_parent_screening(ctx)

        assert [c["model"] for c in calls] == ["fast-model"]
        assert calls[0]["context"] == "parent"

    def test_child_screen_passes_phase1_model(self):
        child = {
            "id": "CHILD-1", "title": "Child Issue",
            "description": "## Acceptance Criteria\n1. CAC one\n",
        }
        calls: list[dict] = []
        prompts: list[str] = []
        batch = [{"index": 0, "verdict": "met", "evidence": "a.py:1"}]

        def _fake(issue_id, context, prompt, model, pi_bin, debug_log, timeout,
                  ac_fallback_used, on_runtime_error, failure_label,
                  child_screen=False, enable_tools=True, priority=None):
            calls.append(model)
            prompts.append(prompt)
            return ({"verdict": "met", "evidence": "", "extracted_text": json.dumps(batch)}, batch, "raw")

        with mock.patch.object(audit_runner, "_call_phase1_screen", side_effect=_fake), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"):
            audit_runner._phase1_review_child_acs(
                0, child, "fast-model", "full-model", "pi", None, None,
                mock.MagicMock(), lambda *a, **k: None,
            )

        assert calls == ["fast-model"]
        assert prompts and "Child Issue" in prompts[0]

    def test_phase2_deep_analysis_keeps_full_model(self):
        issue = {"id": "TEST-1", "description": "## Acceptance Criteria\n1. AC one\n"}
        ac_results = [{"text": "AC one", "verdict": "unmet", "evidence": ""}]
        seen_models: list[str] = []

        def _fake_call(issue_id, context, prompt,
                       model=audit_runner.DEFAULT_MODEL, **kwargs):
            seen_models.append((context, model))
            return {"extracted_text": "[]"}

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call), \
                mock.patch.object(audit_runner, "_build_file_scope_manifest", return_value="manifest"), \
                mock.patch.object(audit_runner, "_validate_file_scope_manifest", return_value=None), \
                mock.patch.object(audit_runner, "_resolve_touched_files", return_value=[]):
            audit_runner._run_phase2_deep_analysis(
                issue, ac_results, [], "full-model",
            )

        phase2 = [m for c, m in seen_models if c == "phase2_deep"]
        assert phase2 == ["full-model"]


# ---------------------------------------------------------------------------
# AC2/AC3 — timing observability
# ---------------------------------------------------------------------------


class TestTimingModelObservability:
    def test_timing_line_surfaces_serving_model(self, capsys):
        with mock.patch.object(audit_runner, "_call_pi") as mock_call:
            mock_call.return_value = {
                "verdict": "met", "evidence": "ok", "elapsed_seconds": 1.5,
            }
            audit_runner._call_pi_and_maybe_log(
                "SA-123", "parent", "prompt", model="fast-model",
            )
        captured = capsys.readouterr()
        assert "Per-call timing:" in captured.err
        assert "model=fast-model" in captured.err

    def test_timing_line_parsers_accept_model_field(self):
        line = (
            "Per-call timing: issue_id=SA-1 context=parent "
            "elapsed_seconds=1.00 input_tokens=410 model=fast-model"
        )
        pat = re.compile(
            r"Per-call timing: issue_id=(\S+) context=(\S+) "
            r"elapsed_seconds=([\d.]+)(?: input_tokens=(\d+))?"
        )
        m = pat.search(line)
        assert m is not None
        assert m.group(4) == "410"


# ---------------------------------------------------------------------------
# AC1 — programmatic phase1_model threading end-to-end through cmd_issue
# ---------------------------------------------------------------------------


class TestProgrammaticPhase1ModelEndToEnd:
    """A programmatic ``phase1_model`` reaches Phase 1 only; Phase 2 keeps the
    full resolved model."""

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
                    stdout=json.dumps({"success": True}), stderr="",
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
        return mock_runner

    def test_phase1_uses_phase1_model_phase2_uses_full_model(self):
        description = (
            "# Test\n\n## Acceptance Criteria\n\n- AC1: The first criterion\n"
        )
        mock_runner = self._make_mock_runner(description)
        batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )
        seen: list[tuple[str, str]] = []

        def _fake_call(issue_id, context, prompt, model=audit_runner.DEFAULT_MODEL, **kwargs):
            seen.append((context, model))
            return batch

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call), \
                mock.patch("code_review.scripts.code_quality.run_code_quality", mock_cq):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                model="full-model", phase1_model="fast-model",
            )

        assert rc == 0
        assert ("parent", "fast-model") in seen
        assert ("phase2_deep", "full-model") in seen

    def test_no_phase1_model_uses_full_model_for_both_phases(self):
        description = (
            "# Test\n\n## Acceptance Criteria\n\n- AC1: The first criterion\n"
        )
        mock_runner = self._make_mock_runner(description)
        batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "file.py:1"},
            ]),
        }
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )
        seen: list[tuple[str, str]] = []

        def _fake_call(issue_id, context, prompt, model=audit_runner.DEFAULT_MODEL, **kwargs):
            seen.append((context, model))
            return batch

        with mock.patch.object(audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_call), \
                mock.patch("code_review.scripts.code_quality.run_code_quality", mock_cq):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=mock_runner,
                model="full-model",
            )

        assert rc == 0
        assert ("parent", "full-model") in seen
        assert ("phase2_deep", "full-model") in seen
