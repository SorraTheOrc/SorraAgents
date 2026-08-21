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
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from audit.scripts.persist_audit import PERSIST_CONTENT_INVALID
from audit.tests.wl_helpers import make_stateful_runner

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
        "shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return target, target_root, patcher


def _make_minimal_runner(recorded: list[list[str]] | None = None,
                         description: str = "",
                         git_cwd: Path | None = None):
    """Fake runner for the no-child, persist=False cmd_issue happy path.

    Handles the wl show/update/children calls plus git (best-effort) and
    defaults every other command to success. *recorded*, when given, receives
    every command list for assertions.

    *git_cwd*, when given, executes git commands for REAL against that
    directory (simulating a process launched from a worktree checkout)
    instead of faking them — used by the worktree-launch regression tests
    (SA-0MSRM7KIF003E0B2) to verify git resolves against the worktree.
    """
    recorded = [] if recorded is None else recorded

    def fake_runner(cmd):
        recorded.append(list(cmd))
        cmd_str = " ".join(cmd)

        if git_cwd is not None and cmd and cmd[0] == "git":
            proc = subprocess.run(
                list(cmd), cwd=str(git_cwd), check=False,
                capture_output=True, text=True,
            )
            return SimpleNamespace(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
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

    return make_stateful_runner(fake_runner)


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
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
            )

        assert rc == 0

    def test_undeterminable_ownership_aborts(self, tmp_path, capsys):
        """AC2 (SA-0MSLLGDW00098UCC): when no sibling project matches the
        item's prefix and no --worklog-dir is given, ownership is
        undeterminable — the run aborts with a clear error instead of
        falling back to the launch cwd's repository for git-derived content.
        """
        # Override the conftest autouse resolvable-ownership fixture: this
        # test exercises the abort path, so resolution must return None.
        with (
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root", return_value=None
            ),
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", tmp_path / "elsewhere"
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "ZZ-0001", persist=False, force=True,
                runner=_make_minimal_runner(description=""),
            )

        assert rc == 1
        err = capsys.readouterr().err
        assert "undeterminable project scope" in err.lower()


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
                "code_review.scripts.code_quality.run_code_quality",
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
                "code_review.scripts.code_quality.run_code_quality",
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
# SA-0MSUBX8PP0087OEA: FILE SCOPE manifest false positive for repos whose
# distinctive markers are all root-level files.
#
# _repo_index aggregates root-level files under "(root)/ (N files)", so a
# work item whose changes touch only framework-shared subdirectories never
# surfaces the owning repo's distinctive root-file markers in the manifest
# and the Phase 2 validation falsely aborts with an audit scope error.
# The fix exposes a bounded list of root file names in the (root) entry so
# root-file markers stay verifiable (AC1) while a manifest built from the
# wrong repo is still rejected (AC2). Fixture repos are used per the work
# item (never live dev-scripts state).
# ===========================================================================


class TestRootFileOnlyRepoManifest:
    """AC1/AC2: root-file-only-marker repos must not false-positive the
    FILE SCOPE validation, and mis-scoped manifests are still rejected.
    """

    ROOT_FILES: ClassVar[list[str]] = [
        "install.sh", "remote", "sshl", "update", "ai.home.conf"
    ]
    SHARED_SUBDIR = "tests"  # present in the framework repo too -> not distinctive

    @staticmethod
    def _init_repo(tmp_path: Path) -> Path:
        """Init a real git repo whose distinctive markers are all root files."""
        repo = tmp_path / "root-file-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "T"], check=True
        )
        for name in TestRootFileOnlyRepoManifest.ROOT_FILES:
            (repo / name).write_text("x\n", encoding="utf-8")
        shared = repo / TestRootFileOnlyRepoManifest.SHARED_SUBDIR
        shared.mkdir()
        (shared / "test_a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        return repo

    @staticmethod
    def _git_runner(repo: Path):
        """Runner resolving git commands against the fixture repo."""
        def runner(cmd):
            if cmd and cmd[0] == "git":
                cmd = ["git", "-C", str(repo)] + list(cmd[1:])
            return subprocess.run(cmd, capture_output=True, text=True,
                                  check=False)
        return runner

    def test_repo_index_exposes_root_file_names(self, tmp_path):
        """AC1: _repo_index lists root-level file names in the (root) entry so
        distinctive root-file markers appear in the manifest.
        """
        repo = self._init_repo(tmp_path)
        index = audit_runner._repo_index(self._git_runner(repo))
        joined = "\n".join(index)
        for name in self.ROOT_FILES:
            assert name in joined, f"root file {name!r} missing from index: {index}"
        assert any(line.startswith("(root)/ (5 files)") for line in index), (
            f"aggregate (root) count must be preserved: {index}"
        )

    def test_root_file_markers_are_distinctive(self, tmp_path):
        """AC1: the fixture repo's root files (not the shared subdir) are the
        distinctive markers the validation verifies against.
        """
        repo = self._init_repo(tmp_path)
        distinctive = audit_runner._distinctive_project_top_levels(repo)
        assert set(distinctive) == set(self.ROOT_FILES), (
            f"expected only root-file markers to be distinctive: {distinctive}"
        )

    def test_manifest_with_root_file_markers_passes_validation(self, tmp_path):
        """AC1: a manifest for a root-file-only-marker repo whose changes touch
        only a framework-shared subdir does NOT abort with a scope error.
        """
        repo = self._init_repo(tmp_path)
        index = audit_runner._repo_index(self._git_runner(repo))
        manifest = (
            "Changed files (git diff / status):\n"
            "- `tests/test_a.py`\n\n"
            "Repository index (top-level layout):\n"
            + "\n".join(f"- {line}" for line in index)
        )
        error = audit_runner._validate_file_scope_manifest(manifest, repo)
        assert error is None, f"false positive scope error: {error}"

    def test_mis_scoped_manifest_still_rejected_for_root_file_repo(self, tmp_path):
        """AC2: a manifest built from the wrong repo (no owning root-file
        markers) is still rejected — the guard is not disabled.
        """
        repo = self._init_repo(tmp_path)
        wrong_manifest = (
            "Repository index (top-level layout):\n"
            "- skill/ (42 files)\n- tests/ (11 files)\n- (root)/ (3 files)"
        )
        error = audit_runner._validate_file_scope_manifest(wrong_manifest, repo)
        assert error is not None
        assert "Audit scope error" in error

    def test_repo_index_root_file_list_is_bounded(self, tmp_path):
        """Risk mitigation: the root-file name list is bounded so a repo with
        many root files cannot bloat the manifest.
        """
        repo = self._init_repo(tmp_path)
        # (root) bucket has 5 files; cap the inline list at 2 and expect "..."
        index = audit_runner._repo_index(
            self._git_runner(repo), max_root_files=2
        )
        root_line = next(
            line for line in index if line.startswith("(root)/")
        )
        shown = root_line.split(":", 1)[1] if ":" in root_line else ""
        truncated = shown.rstrip().endswith(", ...")
        names = [
            p.strip()
            for p in shown.rstrip()[:-5].split(",") if p.strip()
        ] if truncated else [p.strip() for p in shown.split(",") if p.strip()]
        assert len(names) <= 2, (
            f"root-file list must be capped at max_root_files: {root_line}"
        )
        assert truncated, f"truncated list must be marked: {root_line}"


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

        return make_stateful_runner(fake_runner)

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
                "code_review.scripts.code_quality.run_code_quality",
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


# ===========================================================================
# SA-0MSLLGDW00098UCC: git file-scope/HEAD resolution must be
# cwd-independent — git commands target the owning project's repository.
# ===========================================================================


class TestGitResolutionFromNonOwningCwd:
    """Git-derived content resolves against the OWNING project, not the
    launch cwd (SA-0MSLLGDW00098UCC).

    The audit runner's git pieces (HEAD sha, working-tree hash, file-scope
    manifest, green-run evidence) execute git via the runner, which runs in
    the process cwd — the launch directory. A launch from an unrelated cwd
    with ``--worklog-dir`` pointing at the audited project would therefore
    scope git-derived content to the WRONG repository. The fix pins every
    runner git command to the owning project's root via ``git -C
    <owning_root>`` unless the launch cwd IS the owning root (or a worktree
    of it), where commands pass through byte-identical.
    """

    def test_git_commands_target_owning_project_root(self, tmp_path):
        """AC1: a simulated launch from an unrelated cwd with --worklog-dir
        produces git commands pinned to the owning project via `git -C`.
        """
        target, target_root, patcher = _make_sibling_projects(tmp_path)
        # The launch cwd's project root — unrelated to the owning project.
        launch_root = tmp_path / "skill-install-dir"
        launch_root.mkdir()
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", launch_root
            ),
            mock.patch.object(
                audit_runner, "_verify_launch_context", return_value=None
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(recorded),
                worklog_dir=str(target),
            )

        assert rc == 0
        git_cmds = [c for c in recorded if c and c[0] == "git"]
        assert git_cmds, "expected at least one git command to be recorded"
        for cmd in git_cmds:
            assert cmd[1] == "-C", f"git command lacks -C: {cmd}"
            assert cmd[2] == str(target_root), (
                f"git command must target the owning project root: {cmd}"
            )

    def test_undeterminable_ownership_aborts_before_git(self, tmp_path, capsys):
        """AC2: unknown prefix + no --worklog-dir + no sibling match aborts
        with a clear error before any git command runs (no fallback to the
        launch cwd's repository).
        """
        launch_root = tmp_path / "elsewhere"
        launch_root.mkdir()
        recorded: list[list[str]] = []

        with (
            mock.patch.object(
                audit_runner, "_resolve_owning_project_root", return_value=None
            ),
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", launch_root
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "ZZ-0001", persist=False, force=True,
                runner=_make_minimal_runner(recorded),
            )

        assert rc == 1
        err = capsys.readouterr().err
        assert "undeterminable project scope" in err.lower()
        git_cmds = [c for c in recorded if c and c[0] == "git"]
        assert git_cmds == [], (
            "undeterminable ownership must abort before any git command"
        )

    def test_owning_project_launch_git_commands_unchanged(self, tmp_path):
        """AC3: launching from the owning project root leaves git commands
        byte-identical — no `-C` injection, zero regression for the
        standard owning-project path.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        recorded: list[list[str]] = []

        with (
            patcher,
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", target_root
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(recorded),
            )

        assert rc == 0
        git_cmds = [c for c in recorded if c and c[0] == "git"]
        assert git_cmds, "expected at least one git command to be recorded"
        for cmd in git_cmds:
            assert cmd[1] != "-C", (
                f"owning-project launch must not inject -C: {cmd}"
            )


# ===========================================================================
# SA-0MSRM7KIF003E0B2: worktree-launch git regression — a launch from a
# worktree of the owning project keeps git resolving to the WORKTREE
# checkout (worktree branch HEAD + worktree-only files), never the
# --worklog-dir parent's main checkout.
# ===========================================================================


def _make_real_git_project_with_worktree(tmp_path: Path,
                                         prefix: str = "OSL"):
    """Create a real git repo (owning project) plus a real worktree checkout.

    Layout::

        <tmp>/projects/open_source_llm/.worklog/config.yaml   (prefix: OSL)
        <tmp>/projects/open_source_llm/.gitignore             (ignores .worklog/)
        <tmp>/projects/open_source_llm/src/main.py            (tracked marker)
        <tmp>/projects/open_source_llm/.worklog/worktrees/wl-OSL-1-test/
            wt_only/main.py            (worktree-only tracked file)
            wt_uncommitted.txt         (worktree-only untracked file)

    The worktree checks out a branch with a distinct commit (``wt_only/``)
    and an untracked file, so its HEAD and working tree differ from the
    main checkout's — a launch from the worktree must resolve git to the
    worktree checkout (SA-0MSRM7KIF003E0B2).

    Returns ``(worklog_dir, owning_root, worktree_path, main_head,
    worktree_head)``.
    """
    import subprocess as sp

    projects = tmp_path / "projects"
    owning_root = projects / "open_source_llm"
    (owning_root / "src").mkdir(parents=True)
    (owning_root / "src" / "main.py").write_text("print('hi')\n")
    (owning_root / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    worklog_dir = owning_root / ".worklog"
    worklog_dir.mkdir(parents=True)
    (worklog_dir / "config.yaml").write_text(
        f"projectName: Open Source LLM\nprefix: {prefix}\n", encoding="utf-8"
    )

    def _git(*args: str, cwd: Path) -> str:
        proc = sp.run(["git", *args], cwd=str(cwd), check=True,
                      capture_output=True, text=True)
        return proc.stdout.strip()

    _git("init", cwd=owning_root)
    _git("config", "user.email", "test@test.com", cwd=owning_root)
    _git("config", "user.name", "Test", cwd=owning_root)
    _git("add", "-A", cwd=owning_root)
    _git("commit", "-m", "main", cwd=owning_root)
    main_head = _git("rev-parse", "HEAD", cwd=owning_root)

    worktree_path = (worklog_dir / "worktrees" / "wl-OSL-1-test").resolve()
    _git("worktree", "add", "-b", "wl-OSL-1-test",
         str(worktree_path), cwd=owning_root)
    # worktree-only tracked file (committed on the worktree branch).
    (worktree_path / "wt_only").mkdir()
    (worktree_path / "wt_only" / "main.py").write_text("print('wt')\n")
    _git("add", "-A", cwd=worktree_path)
    _git("commit", "-m", "worktree-only", cwd=worktree_path)
    # worktree-only untracked file (uncommitted working-tree state).
    (worktree_path / "wt_uncommitted.txt").write_text("wt state\n")
    worktree_head = _git("rev-parse", "HEAD", cwd=worktree_path)
    return worklog_dir, owning_root, worktree_path, main_head, worktree_head


class TestWorktreeLaunchGitResolution:
    """A launch from a worktree of the owning project keeps git resolving to
    the worktree checkout (SA-0MSLLGDW00098UCC AC3) — never the
    --worklog-dir parent's main checkout.
    """

    def test_worktree_launch_git_commands_byte_identical(self, tmp_path):
        """AC3: launching from a worktree of the owning project leaves git
        commands byte-identical (no -C injection), so git resolves against
        the worktree checkout — not the --worklog-dir parent's main
        checkout.
        """
        worklog_dir, _owning_root, worktree_path, _mh, _wh = (
            _make_real_git_project_with_worktree(tmp_path)
        )
        recorded: list[list[str]] = []

        with (
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", worktree_path
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(recorded, git_cwd=worktree_path),
                worklog_dir=str(worklog_dir),
            )

        assert rc == 0
        git_cmds = [c for c in recorded if c and c[0] == "git"]
        assert git_cmds, "expected at least one git command to be recorded"
        for cmd in git_cmds:
            assert cmd[1] != "-C", (
                f"worktree launch must not inject -C: {cmd}"
            )

    def test_worktree_launch_head_and_manifest_reflect_worktree(self, tmp_path):
        """AC3: HEAD sha and file-scope manifest reflect the WORKTREE state,
        not the --worklog-dir parent's main checkout.
        """
        worklog_dir, _owning_root, worktree_path, main_head, worktree_head = (
            _make_real_git_project_with_worktree(tmp_path)
        )
        assert main_head != worktree_head, "fixture: worktree HEAD must differ"
        recorded: list[list[str]] = []
        runner = _make_minimal_runner(recorded, git_cwd=worktree_path)

        with (
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", worktree_path
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=runner,
                worklog_dir=str(worklog_dir),
            )

        assert rc == 0
        # HEAD resolves to the worktree checkout — the worktree branch HEAD.
        head = audit_runner._resolve_audited_head(runner)
        assert head == worktree_head, (
            f"audited HEAD must reflect the worktree checkout "
            f"({worktree_head}), not the main checkout ({main_head}): got {head}"
        )
        # File-scope manifest reflects the worktree state: worktree-only
        # tracked file in the repo index + worktree-only untracked file in
        # the changed-files list.
        manifest = audit_runner._build_file_scope_manifest(
            {}, [], runner=runner
        )
        assert "wt_only" in manifest, (
            f"manifest must reflect worktree-only files: {manifest!r}"
        )
        changed = audit_runner._git_changed_files(runner)
        assert "wt_uncommitted.txt" in changed, (
            f"changed files must reflect the worktree working tree: {changed}"
        )
