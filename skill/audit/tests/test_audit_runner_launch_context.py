#!/usr/bin/env python3
"""Tests for the audit runner launch-context guard (LP-0MSQ32HNR007AI6B).

Covers the fail-fast launch contract so a mis-scoped audit aborts loudly and
early instead of producing a misleading full run (incident: an audit of an
LP item launched from the SorraAgents cwd ran Phase 2 against the audit
skill's own tree — ~124 min model time wasted):

  - AC1/AC4(a): a launch from a non-owning project dir fails fast with a
    clear error and non-zero exit, with ZERO pi calls.
  - AC2/AC4(b): a Phase 2 FILE SCOPE manifest that lacks the item repository
    aborts with a scope error before Phase 2 (no 'unmet' verdicts emitted).
  - AC3/AC4(c): a child audit persistence failure aborts the run instead of
    being swallowed as a warning that leads to a misleading parent report.
  - AC5: a correctly-configured launch (from the owning project root) still
    runs unchanged (guard is zero-cost for correct launches).
"""  # noqa: EXE001
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts import audit_runner
from skill.audit.scripts.persist_audit import PERSIST_CONTENT_INVALID

# ===========================================================================
# Helpers
# ===========================================================================


def _make_wl_success_proc() -> SimpleNamespace:
    """Build a canned success response for a wl subcommand."""
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"success": True}),
        stderr="",
    )


def _make_sibling_projects(tmp_path: Path, prefix: str = "OSL") -> tuple[Path, Path, mock.patch]:
    """Create a tmp projects dir with a sibling target project.

    Layout::

        <tmp>/projects/
            SorraAgents/.worklog/config.yaml      (prefix: SA)
            open_source_llm/.worklog/config.yaml  (prefix: OSL)
            open_source_llm/src/                  (distinctive repo marker)

    Returns ``(target_worklog_dir, target_project_root, patcher)`` where
    *target_project_root* is the owning project root (the parent of its
    ``.worklog``) and *patcher* is a ``mock.patch`` on the shared
    ``skill.shared.status_lifecycle.SIBLING_SCAN_ROOT`` constant.
    """
    projects = tmp_path / "projects"
    framework = projects / "SorraAgents" / ".worklog"
    framework.mkdir(parents=True)
    (framework / "config.yaml").write_text(
        "projectName: Sorra Agents\nprefix: SA\n", encoding="utf-8"
    )
    target_root = projects / "open_source_llm"
    target = target_root / ".worklog"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text(
        f"projectName: Open Source LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    # Distinctive top-level marker of the owning project's repository (used
    # by the file-scope manifest validation; the framework repo has no src/).
    (target_root / "src").mkdir()
    patcher = mock.patch(
        "skill.shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return target, target_root, patcher


def _make_minimal_runner(recorded: list[list[str]] | None = None,
                         description: str = ""):
    """Fake runner for the no-child, persist=False cmd_issue happy path.

    Handles the wl show/update/children calls plus git (best-effort) and
    defaults every other command to success. *recorded*, when given, receives
    every command list for assertions.
    """
    recorded = [] if recorded is None else recorded

    def fake_runner(cmd):
        recorded.append(list(cmd))
        cmd_str = " ".join(cmd)

        if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {"id": "OSL-1", "status": "open"},
                }),
                stderr="",
            )
        if "--children" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "success": True,
                    "workItem": {
                        "id": "OSL-1",
                        "description": description,
                        "status": "in_progress",
                    },
                    "children": [],
                }),
                stderr="",
            )
        if "update" in cmd_str:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"success": True}),
                stderr="",
            )
        return _make_wl_success_proc()

    return fake_runner


# ===========================================================================
# AC1 / AC4(a): launch-context guard — non-owning cwd fails fast
# ===========================================================================


class TestLaunchContextGuard:
    """A launch from a non-owning project dir must abort before any pi call."""

    def test_non_owning_cwd_aborts_with_zero_pi_calls(self, tmp_path):
        """AC4(a): launching from a non-owning project dir returns non-zero
        and never invokes the pi/model path (zero pi calls).
        """
        _target, _target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log"
            ) as pi_mock,
            mock.patch("builtins.print"),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
            )

        assert rc == 1
        pi_mock.assert_not_called()

    def test_non_owning_cwd_error_names_resolved_vs_expected(self, tmp_path, capsys):
        """AC1: the abort message states the resolved (launch) and expected
        (owning) project so operators can re-launch from the right directory.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log"
            ) as pi_mock,
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
            )

        assert rc == 1
        pi_mock.assert_not_called()
        err = capsys.readouterr().err
        assert "Audit launch-context error" in err
        assert str(wrong_root) in err
        assert str(target_root) in err
        assert "OSL-1" in err

    def test_guard_runs_before_any_wl_status_transition(self, tmp_path):
        """AC1: the guard fires before the status lifecycle — a mis-scoped
        launch never flips the item to in_progress (no wasted state changes).
        """
        _target, _target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
            mock.patch("builtins.print"),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(recorded),
            )

        assert rc == 1
        status_updates = [
            c for c in recorded if "update" in c and "--status" in c
        ]
        assert status_updates == [], (
            "a mis-scoped launch must not touch the item status"
        )

    def test_explicit_worklog_dir_does_not_bypass_wrong_cwd(self, tmp_path):
        """AC1: passing --worklog-dir does NOT change the project scope — a
        launch from a non-owning cwd still aborts (the expected project is
        derived from the explicit dir's parent per resolution precedence).
        """
        target, _target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log"
            ) as pi_mock,
            mock.patch("builtins.print"),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
                worklog_dir=str(target),
            )

        assert rc == 1
        pi_mock.assert_not_called()

    def test_owning_cwd_passes_guard_and_runs_unchanged(self, tmp_path):
        """AC5 (regression): a correctly-configured launch from the owning
        project root passes the guard and the run completes normally.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", target_root),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
            )

        assert rc == 0

    def test_fail_open_when_ownership_cannot_be_determined(self, tmp_path):
        """AC1: when no sibling project matches the item's prefix, ownership
        cannot be determined and the guard fails open (never blocks).
        """
        # No patcher → the real sibling scan has no project with prefix ZZZ.
        with (
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", tmp_path / "elsewhere"
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "ZZZ-1", persist=False, force=True,
                runner=_make_minimal_runner(description=""),
            )

        assert rc == 0


# ===========================================================================
# AC2 / AC4(b): FILE SCOPE manifest validation
# ===========================================================================


class TestFileScopeManifestValidation:
    """The Phase 2 FILE SCOPE manifest must contain the item repository."""

    def test_wrong_scope_manifest_is_rejected(self, tmp_path):
        """AC2: a manifest built from the audit skill's own tree (no item
        repo files) yields a scope error.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        skill_tree_manifest = (
            "Key Files (from the work item):\n- `skill/audit/scripts/audit_runner.py`\n\n"
            "Repository index (top-level layout):\n"
            "- tests/ (11 files)\n- scripts/ (5 files)\n- (root)/ (3 files)"
        )
        with patcher:
            error = audit_runner._validate_file_scope_manifest(
                skill_tree_manifest, target_root
            )
        assert error is not None
        assert "Audit scope error" in error
        assert str(target_root) in error

    def test_correct_scope_manifest_is_accepted(self, tmp_path):
        """AC2: a manifest referencing the item repository's top-level files
        passes validation (inclusion-based — need not equal the repo).
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        correct_manifest = (
            "Repository index (top-level layout):\n"
            "- src/ (42 files)\n- docs/ (5 files)"
        )
        with patcher:
            error = audit_runner._validate_file_scope_manifest(
                correct_manifest, target_root
            )
        assert error is None

    def test_fail_open_when_owning_root_unknown(self):
        """AC2: with no owning project the manifest cannot be verified —
        fail open rather than blocking legitimately-launched audits.
        """
        assert audit_runner._validate_file_scope_manifest(
            "anything at all", None
        ) is None

    def test_fail_open_when_no_distinctive_marker(self, tmp_path):
        """AC2: mono-repo items whose files live in the framework repo have
        no distinctive top-level marker — validation fails open (no false
        flag, per the risk mitigation).
        """
        with mock.patch.object(
            audit_runner, "_project_top_levels", return_value=[]
        ), mock.patch.object(
            audit_runner, "_distinctive_project_top_levels", return_value=[]
        ):
            assert audit_runner._validate_file_scope_manifest(
                "tests/ (11 files)", tmp_path
            ) is None

    def test_missing_item_repo_aborts_before_phase2(self, tmp_path):
        """AC4(b): a scope manifest missing the item repo aborts the run
        before Phase 2 — zero pi calls, non-zero exit, no 'unmet' verdicts.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        desc = "## Acceptance Criteria\n- AC1: the thing works.\n- AC2: docs updated."
        skill_tree_manifest = (
            "Key Files (from the work item):\n"
            "- `proxy/proxy/provider.py`\n\n"
            "Repository index (top-level layout):\n"
            "- tests/ (11 files)\n- scripts/ (5 files)\n- (root)/ (3 files)"
        )

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", target_root),
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                return_value=skill_tree_manifest,
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log"
            ) as pi_mock,
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
            mock.patch("builtins.print"),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(description=desc),
            )

        assert rc == 1
        pi_mock.assert_not_called()

    def test_correct_scope_proceeds_to_phase1(self, tmp_path):
        """AC4(b) regression: with a valid manifest the run proceeds to the
        Phase 1 pi call (the guard is not over-eager).
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        desc = "## Acceptance Criteria\n- AC1: the thing works."
        correct_manifest = (
            "Repository index (top-level layout):\n- src/ (42 files)"
        )
        met_batch = {
            "extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "src/main.py:1"},
            ]),
        }

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", target_root),
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                return_value=correct_manifest,
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", return_value=met_batch
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(description=desc),
            )

        assert rc == 0


# ===========================================================================
# AC3 / AC4(c): child persistence failures are fatal
# ===========================================================================


class TestChildPersistFailureFatal:
    """A child audit persistence failure aborts the run instead of warning."""

    def _make_parent_with_child_runner(self, child_id: str = "OSL-2"):
        """Fake runner returning a parent OSL-1 with one child OSL-2."""
        def fake_runner(cmd):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                # Parent readback (after persist) must return a stored audit
                # referencing OSL-1; the child verdict check returns no audit
                # so the child is audited and persisted by this run.
                if "OSL-1" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "rawOutput": (
                                    "Ready to close: Yes\n-- OSL-1 --\n"
                                ),
                                "auditedAt": "2026-01-01T00:00:00.000Z",
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "audit": None}),
                    stderr="",
                )
            if "show" in cmd_str and "--children" not in cmd_str and "--json" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {"id": "OSL-1", "status": "open"},
                    }),
                    stderr="",
                )
            if "--children" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({
                        "success": True,
                        "workItem": {
                            "id": "OSL-1",
                            "description": "## Acceptance Criteria\n- AC1: parent works.",
                            "status": "in_progress",
                        },
                        "children": [
                            {
                                "id": child_id,
                                "title": "Child",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": "## Acceptance Criteria\n- AC1: child works.",
                            },
                        ],
                    }),
                    stderr="",
                )
            if "audit-show" in cmd_str:
                # Parent readback (after persist) must return a stored audit
                # referencing OSL-1; the child verdict check returns no audit
                # so the child is audited and persisted by this run.
                if "OSL-1" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "rawOutput": (
                                    "Ready to close: Yes\n-- OSL-1 --\n"
                                ),
                                "auditedAt": "2026-01-01T00:00:00.000Z",
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True, "audit": None}),
                    stderr="",
                )
            if "update" in cmd_str:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"success": True}),
                    stderr="",
                )
            return _make_wl_success_proc()

        return fake_runner

    def _run_with_child(self, persist_rc: int, tmp_path):
        """Run cmd_issue with one child and a canned persist_audit return."""
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        pi_calls: list[str] = []

        def _fake_pi(issue_id, context, prompt, **kwargs):
            pi_calls.append(context)
            return {"extracted_text": json.dumps([
                {"index": 0, "verdict": "met", "evidence": "src/main.py:1"},
            ])}

        def _passthrough_phase2(work_item, ac_results, child_results, **kwargs):
            return (ac_results, child_results, True)

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", target_root),
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                return_value=(
                    "Repository index (top-level layout):\n- src/ (42 files)"
                ),
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_fake_pi
            ),
            mock.patch.object(
                audit_runner, "_run_phase2_deep_analysis",
                side_effect=_passthrough_phase2,
            ),
            mock.patch.object(
                audit_runner, "persist_audit", return_value=persist_rc
            ),
            mock.patch(
                "skill.code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=True, force=True,
                runner=self._make_parent_with_child_runner(),
                audit_children=True,
            )
        return rc, pi_calls

    def test_child_persist_not_found_aborts_run(self, tmp_path, capsys):
        """AC4(c): a child 'Work item not found' persist failure (rc=1) aborts
        the run with a non-zero exit and a clear error.
        """
        rc, _pi_calls = self._run_with_child(persist_rc=1, tmp_path=tmp_path)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Failed to persist audit for child OSL-2" in err
        assert "Aborting" in err

    def test_child_persist_content_invalid_is_not_fatal(self, tmp_path):
        """AC3 scoping: PERSIST_CONTENT_INVALID (4) means a usable fallback
        notice WAS persisted — the run completes (rc==0), not aborted.
        """
        rc, _pi_calls = self._run_with_child(
            persist_rc=PERSIST_CONTENT_INVALID, tmp_path=tmp_path
        )
        assert rc == 0
