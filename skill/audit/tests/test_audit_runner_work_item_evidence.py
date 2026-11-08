"""Work-item comments as read-only evidence in the audit prompts.

Regression guard for SA-0MW742YZS002TZ9F (discovered-from AI_Hell
AH-0MW4W2CAJ000NUJZ): the Phase 1 parent screen and the Phase 2 parent deep
analysis previously carried only the FILE SCOPE manifest + the AC list, so an
acceptance criterion whose required evidence is recorded in a comment (e.g. a
verification-log AC) could never be verified and was graded a false-negative
``unmet``.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests."""
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield


COMMENTS = [
    {
        "author": "implement",
        "createdAt": "2026-11-06T16:28:37.777Z",
        "comment": "AC2 evidence: `wl update --audit-text` returned rc=0 on a "
        "drifted sandbox copy (schemaVersion=8, fingerprint column dropped).",
    },
]


def _make_issue() -> dict:
    return {"id": "TEST-1", "title": "Test Issue"}


def _make_ac(index: int = 0) -> dict:
    return {"index": index, "text": "AC text", "verdict": "met", "evidence": ""}


def _parent_screen_ctx(**overrides) -> audit_runner._AuditContext:
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
    ctx.comments = list(COMMENTS)
    ctx.ac_results = []
    ctx.owning_root = "."
    ctx.resolved_phase1_model = "fast-model"
    ctx.resolved_model = "full-model"
    return ctx


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def test_evidence_block_renders_comments():
    block = audit_runner._build_work_item_evidence_block(COMMENTS)
    assert "WORK ITEM EVIDENCE" in block
    assert "comment by implement" in block
    assert "rc=0 on a" in block
    # Explicitly instructs the model not to grade comment-backed evidence unmet.
    assert "do NOT mark the criterion 'unmet'" in block


def test_evidence_block_empty_when_no_comments():
    assert audit_runner._build_work_item_evidence_block(None) == ""
    assert audit_runner._build_work_item_evidence_block([]) == ""


def test_evidence_block_bounded_and_truncated():
    long_comments = [
        {"author": "a", "comment": "x" * 5000},
        {"author": "b", "comment": "y" * 5000},
    ]
    block = audit_runner._build_work_item_evidence_block(long_comments, max_chars=400)
    assert "…[truncated]" in block
    # The block is bounded by the cap (plus the fixed header/footer prose).
    assert len(block) < 400 + 700


# ---------------------------------------------------------------------------
# Prompt wiring
# ---------------------------------------------------------------------------

def test_phase2_parent_prompt_includes_comment_evidence():
    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log",
        return_value={"extracted_text": "[]"},
    ) as mock_call:
        audit_runner._run_phase2_deep_analysis(
            _make_issue(), [_make_ac()], [], "test-model", comments=COMMENTS,
        )
    prompt = mock_call.call_args.args[2]
    assert "WORK ITEM EVIDENCE" in prompt
    assert "rc=0 on a" in prompt


def test_phase2_parent_prompt_unchanged_without_comments():
    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log",
        return_value={"extracted_text": "[]"},
    ) as mock_call:
        audit_runner._run_phase2_deep_analysis(
            _make_issue(), [_make_ac()], [], "test-model",
        )
    prompt = mock_call.call_args.args[2]
    assert "WORK ITEM EVIDENCE" not in prompt


def test_phase1_parent_prompt_includes_comment_evidence():
    ctx = _parent_screen_ctx()
    captured: dict[str, str] = {}

    def fake_call(issue_id, context, prompt, **kwargs):
        captured["prompt"] = prompt
        return {"extracted_text": "[]"}

    with mock.patch.object(
        audit_runner, "_call_pi_and_maybe_log", side_effect=fake_call
    ), mock.patch.object(
        audit_runner, "_build_file_scope_manifest", return_value="manifest"
    ), mock.patch.object(
        audit_runner, "_validate_file_scope_manifest", return_value=None
    ), mock.patch.object(
        audit_runner, "_resolve_touched_files", return_value=[]
    ):
        audit_runner._phase1_parent_screening(ctx)

    assert "WORK ITEM EVIDENCE" in captured["prompt"]
    assert "rc=0 on a" in captured["prompt"]
