"""SA-0MWO59IMN000RUHM: cross-repo (``--code-root``) FILE SCOPE validation.

The Phase 1/2 FILE SCOPE manifest is **built** from the per-audit target root
— the code root when ``--code-root`` / ``AUDIT_CODE_ROOT`` is set
(LP-0MVZ6R5H8007NYLR) — so it must be **validated** against that same root.
Validating against the worklog owning root made every genuine cross-repo audit
abort with an ``Audit scope error`` and (before this fix) still persist a
``Ready to close: No`` report whose acceptance-criteria table was empty.

Two contracts are covered here:

1. The validation root tracks the manifest's build root (the per-audit root),
   falling back to the owning root outside a run — byte-for-byte unchanged
   behaviour for same-repo audits (AC2).
2. A scope error is **fatal**: ``_phase1_parent_screening`` raises
   ``AuditScopeError`` (handled by ``cmd_issue`` → non-zero exit, no persisted
   report, pre-audit lifecycle restored) instead of printing and returning
   (AC4).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner
from audit.tests.test_audit_report_audit_scope import _persisted_report
from audit.tests.test_audit_runner_launch_context import (
    _fake_pi_met,
    _make_cross_repo_pair,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_CROSS_REPO_ACS_DESCRIPTION = (
    "## Acceptance Criteria\n"
    "- AC1: the codeb feature exists in the code repository\n"
    "\n"
    "## Key Files (predicted)\n"
    "- `src/codeb/feature.py`\n"
)


def _parent_screen_ctx(owning_root: str) -> audit_runner._AuditContext:
    """Minimal ``_AuditContext`` for a direct ``_phase1_parent_screening``."""
    runner = mock.MagicMock()
    runner.side_effect = None
    ctx = audit_runner._AuditContext(
        issue_id="OSL-1", persist=False, timeout=None, parent_timeout=None,
        pi_bin="pi", model=None, model_source="local", runner=runner,
        json_mode=False, debug_log=None, force=False, worklog_dir=None,
        batch_phase2=False, green_run=None, audit_children=False,
        max_child_audits=None, run_tests=False,
    )
    ctx.acs = ["AC1: the codeb feature exists"]
    ctx.work_item = {
        "id": "OSL-1",
        "title": "cross-repo item",
        "description": _CROSS_REPO_ACS_DESCRIPTION,
    }
    ctx.ac_results = []
    ctx.owning_root = owning_root
    ctx.resolved_phase1_model = "fast-model"
    ctx.resolved_model = "full-model"
    return ctx


# ---------------------------------------------------------------------------
# 1. The validation root tracks the per-audit (manifest build) root
# ---------------------------------------------------------------------------

class TestScopeValidationRoot:
    def test_uses_per_audit_root_during_a_run(self, tmp_path):
        """AC1: inside a run the per-audit root (the code root for a
        ``--code-root`` audit) is the validation root — never the worklog
        owner."""
        code_root = tmp_path / "code-repo"
        owning_root = tmp_path / "worklog-owner"
        with mock.patch.object(audit_runner, "_ACTIVE_AUDIT_ROOT", code_root):
            assert audit_runner._scope_validation_root(owning_root) == code_root

    def test_falls_back_to_owning_root_outside_a_run(self):
        """AC2: outside a run (no published per-audit root) the owning root is
        used unchanged, so a direct/unit caller behaves exactly as before."""
        owning_root = Path("/tmp/owning")
        with mock.patch.object(audit_runner, "_ACTIVE_AUDIT_ROOT", None):
            assert (
                audit_runner._scope_validation_root(owning_root) == owning_root
            )

    def test_keeps_none_when_unresolved(self):
        """A ``None`` owning root stays ``None`` (fail-open caller contract)."""
        with mock.patch.object(audit_runner, "_ACTIVE_AUDIT_ROOT", None):
            assert audit_runner._scope_validation_root(None) is None


# ---------------------------------------------------------------------------
# 2. Both screening phases validate against the code root
# ---------------------------------------------------------------------------

class TestScreeningUsesCodeRoot:
    def test_phase1_validates_manifest_against_code_root(self, tmp_path):
        """AC1: Phase 1 parent screening hands the validator the code root (the
        root the manifest was built from), not the worklog owning root."""
        code_root = tmp_path / "code-repo"
        owning_root = tmp_path / "worklog-owner"
        ctx = _parent_screen_ctx(str(owning_root))
        seen: dict = {}

        def fake_validate(manifest, root, touched_files=None):
            seen["root"] = root
            seen["touched"] = touched_files

        with (
            mock.patch.object(
                audit_runner, "_ACTIVE_AUDIT_ROOT", code_root,
            ),
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                return_value="manifest",
            ),
            mock.patch.object(
                audit_runner, "_resolve_touched_files", return_value=[],
            ),
            mock.patch.object(
                audit_runner, "_validate_file_scope_manifest",
                side_effect=fake_validate,
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi_met,
            ),
        ):
            audit_runner._phase1_parent_screening(ctx)

        assert seen["root"] == code_root, (
            "Phase 1 must validate the FILE SCOPE manifest against the code "
            f"root, got {seen['root']!r}"
        )
        assert [r["verdict"] for r in ctx.ac_results] == ["met"]

    def test_phase2_validates_manifest_against_code_root(self, tmp_path):
        """AC1: Phase 2 deep analysis uses the same validation root."""
        code_root = tmp_path / "code-repo"
        owning_root = tmp_path / "worklog-owner"
        seen: dict = {}

        def fake_validate(manifest, root, touched_files=None):
            seen["root"] = root

        issue = {
            "id": "OSL-1", "title": "cross-repo item", "status": "completed",
            "stage": "in_review", "description": _CROSS_REPO_ACS_DESCRIPTION,
        }
        acs = [{"index": 0, "text": "AC1: feature exists", "verdict": "met"}]

        with (
            mock.patch.object(
                audit_runner, "_ACTIVE_AUDIT_ROOT", code_root,
            ),
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root",
                return_value=owning_root,
            ),
            mock.patch.object(
                audit_runner, "_validate_file_scope_manifest",
                side_effect=fake_validate,
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={"extracted_text": "[]"},
            ),
        ):
            audit_runner._run_phase2_deep_analysis(issue, acs, [], "model")

        assert seen["root"] == code_root, (
            "Phase 2 must validate the FILE SCOPE manifest against the code "
            f"root, got {seen['root']!r}"
        )


# ---------------------------------------------------------------------------
# 3. A scope error is fatal (never a persisted AC-less verdict)
# ---------------------------------------------------------------------------

class TestScopeErrorIsFatal:
    def test_phase1_scope_error_raises_audit_scope_error(self):
        """AC4: a Phase 1 scope error aborts the run instead of returning —
        ``cmd_issue`` catches ``AuditScopeError``, prints it, exits non-zero
        and persists nothing (the pre-audit lifecycle is restored)."""
        ctx = _parent_screen_ctx("/owning/project")
        pi_call = mock.MagicMock()

        with (
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                return_value="manifest",
            ),
            mock.patch.object(
                audit_runner, "_resolve_touched_files", return_value=[],
            ),
            mock.patch.object(
                audit_runner, "_validate_file_scope_manifest",
                return_value="Audit scope error: wrong repository",
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", pi_call,
            ),pytest.raises(audit_runner.AuditScopeError)
        ):
            audit_runner._phase1_parent_screening(ctx)

        pi_call.assert_not_called()
        assert ctx.ac_results == [], (
            "a fatal scope error must not synthesise AC verdicts"
        )


# ---------------------------------------------------------------------------
# 4. End-to-end: a cross-repo item with ACs completes and is screened
# ---------------------------------------------------------------------------

class TestCrossRepoAuditScreensAcs:
    def test_cross_repo_audit_fills_the_ac_table(self, tmp_path):
        """AC1 regression: a cross-repo item with acceptance criteria is
        screened — the persisted report carries an AC row and no scope error.

        Pre-fix this run aborted its parent screen (empty AC table) because the
        manifest was built from repo B and validated against repo A.
        """
        worklog_a, _repo_a, repo_b = _make_cross_repo_pair(tmp_path)
        rc, persist_calls = _persisted_report(
            worklog_dir=worklog_a,
            git_root=repo_b,
            description=_CROSS_REPO_ACS_DESCRIPTION,
            code_root=str(repo_b),
        )

        assert rc == 0, "a cross-repo audit with --code-root must exit green"
        assert persist_calls, "the run must persist exactly one report"
        _issue_id, report, _kwargs = persist_calls[-1]
        assert "Audit scope error" not in report, report
        assert "| 1 |" in report, (
            "the persisted report must carry a row per acceptance criterion:\n"
            + report
        )

    def test_genuine_misscoped_manifest_still_fails_closed(self, tmp_path):
        """AC3: a manifest built from a foreign tree (the audit skill's own
        repository layout) is still rejected when the code repo has its own
        distinctive top-level entries."""
        code_root = tmp_path / "code-repo"
        (code_root / "src").mkdir(parents=True)
        foreign_manifest = (
            "Repository index (top-level layout):\n"
            "- skill/ (410 files)\n"
            "- docs/ (55 files)\n"
        )
        error = audit_runner._validate_file_scope_manifest(
            foreign_manifest, code_root,
        )
        assert error is not None and "Audit scope error" in error

        matched_manifest = foreign_manifest + "- src/ (3 files)\n"
        assert audit_runner._validate_file_scope_manifest(
            matched_manifest, code_root,
        ) is None
