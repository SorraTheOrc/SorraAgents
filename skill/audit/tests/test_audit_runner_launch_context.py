#!/usr/bin/env python3
"""Tests for the audit runner launch-context guard (LP-0MSQ32HNR007AI6B).

Covers the launch-context contract. SA-0MSRLECW2001AA15 inverted the
historical fail-fast guard: the per-audit target root is now resolved from
the worklog-derived owner, so a determinable non-owning launch proceeds
against the owning project with a one-line stderr warning; only
undeterminable ownership still aborts (incident context: an audit of an LP
item launched from the SorraAgents cwd once ran Phase 2 against the audit
skill's own tree — ~124 min model time wasted):

  - AC1/AC4(a): a determinable non-owning launch proceeds with a stderr
    warning naming the launch and owning roots (no abort).
  - AC2/AC4(b): a Phase 2 FILE SCOPE manifest that lacks the item repository
    aborts with a scope error before Phase 2 (no 'unmet' verdicts emitted).
  - AC3/AC4(c): a child audit persistence failure aborts the run instead of
    being swallowed as a warning that leads to a misleading parent report.
  - AC4: a correctly-configured launch (from the owning project root) runs
    silently unchanged.
  - Undeterminable ownership still fails fast with a clear error.
"""  # noqa: EXE001
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so the audit_runner module is importable.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner
from audit.scripts.persist_audit import PERSIST_CONTENT_INVALID
from audit.tests.wl_helpers import make_stateful_runner
from shared.git_sandbox import (
    GitSandboxError,
    add_worktree,
    commit_all,
    fixture_git_env,
    init_repo,
    run_git,
)

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
            # Hermetic env: the finish gate exports a temporary GIT_INDEX_FILE
            # for the suite it runs, and a fixture command that inherited it
            # would stage into the gate's index (SA-0MVWS6G0Z0050PD2).
            proc = subprocess.run(
                list(cmd), cwd=str(git_cwd), check=False,
                capture_output=True, text=True, env=fixture_git_env(),
            )
            return SimpleNamespace(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )

        # Fake git against an empty repository: success with no output. This
        # keeps touched-file resolution fail-open (``_resolve_touched_files``
        # returns None) instead of treating the generic wl JSON sentinel as a
        # repository path (SA-0MUKCOW1I001MJ7O).
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")

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
    """A launch from a determinable non-owning cwd proceeds with a warning.

    SA-0MSRLECW2001AA15 inverted the historical fail-fast contract: the
    per-audit target root is now resolved from the worklog-derived owner, so
    a foreign-cwd launch audits the owning project instead of aborting. Only
    **undeterminable** ownership still fails fast.
    """

    def test_non_owning_cwd_proceeds_with_warning(self, tmp_path, capsys):
        """AC1: a determinable non-owning launch proceeds (rc == 0) and emits
        a one-line stderr warning naming the launch and owning roots.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
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
        captured = capsys.readouterr()
        assert str(wrong_root) in captured.err
        assert str(target_root) in captured.err
        # The warning is a stderr diagnostic only — it never enters the
        # report/JSON output (AC3).
        assert "Warning: audit launched from" not in captured.out

    def test_explicit_worklog_dir_foreign_cwd_proceeds(self, tmp_path, capsys):
        """AC1: with an explicit --worklog-dir the owning root is the
        resolved dir's parent, so a foreign-cwd launch still proceeds against
        that owner with a warning.
        """
        target, target_root, patcher = _make_sibling_projects(tmp_path)
        wrong_root = tmp_path / "wrong-project"
        wrong_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", wrong_root),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
                worklog_dir=str(target),
            )

        assert rc == 0
        err = capsys.readouterr().err
        assert str(target_root) in err

    def test_owning_cwd_passes_guard_silently(self, tmp_path, capsys):
        """AC4: a launch from the owning project root proceeds silently —
        no warning is emitted.
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
        err = capsys.readouterr().err
        assert "Warning: audit launched from" not in err

    def test_undeterminable_ownership_aborts(self, tmp_path, capsys):
        """AC2 (SA-0MSRLECW2001AA15): when no sibling project matches the
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

    def test_manifest_omitting_touched_files_is_rejected(self, tmp_path):
        """SA-0MUKCOW1I001MJ7O: a manifest that references the item repo but
        omits every touched file cannot verify the item's changes.
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        manifest = (
            "Repository index (top-level layout):\n- src/ (42 files)"
        )
        with patcher:
            error = audit_runner._validate_file_scope_manifest(
                manifest, target_root,
                touched_files=["src/implementation.py"],
            )
        assert error is not None
        assert "Audit scope error" in error

    def test_manifest_containing_touched_file_is_accepted(self, tmp_path):
        """SA-0MUKCOW1I001MJ7O: when the touched set appears, validation passes."""
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        manifest = (
            "Touched files (from the item's commits):\n"
            "- `src/implementation.py`\n\n"
            "Repository index (top-level layout):\n- src/ (42 files)"
        )
        with patcher:
            error = audit_runner._validate_file_scope_manifest(
                manifest, target_root,
                touched_files=["src/implementation.py"],
            )
        assert error is None

    def test_touched_files_none_and_empty_fail_open(self, tmp_path):
        """SA-0MUKCOW1I001MJ7O: an unresolvable/empty touched set never blocks."""
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        manifest = (
            "Repository index (top-level layout):\n- src/ (42 files)"
        )
        with patcher:
            assert audit_runner._validate_file_scope_manifest(
                manifest, target_root, touched_files=None,
            ) is None
            assert audit_runner._validate_file_scope_manifest(
                manifest, target_root, touched_files=[],
            ) is None

    def test_built_manifest_always_covers_committed_touched_files(self, tmp_path):
        """SA-0MUKCOW1I001MJ7O: the builder + validator compose — a manifest
        built for an item with resolvable touched files always passes the
        touched-files coverage check (the omission is impossible to build).
        """
        _target, target_root, patcher = _make_sibling_projects(tmp_path)
        committed = ["src/implementation.py", "src/helpers/util.py"]
        issue = {
            "id": "OSL-1",
            "title": "T",
            "description": "## Key Files\n\n- `src/implementation.py`\n",
        }

        def _runner(cmd):
            cmd = [str(c) for c in cmd]
            joined = " ".join(cmd)
            marker = audit_runner._TOUCHED_FILES_COMMIT_MARKER
            if cmd[:2] == ["git", "log"] and "--name-only" in joined:
                out = marker + "abc1234\n" + "\n".join(committed)
            elif cmd[:2] == ["git", "ls-files"]:
                out = "\n".join(committed)
            else:
                out = ""
            return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

        manifest = audit_runner._build_file_scope_manifest(
            issue, [], runner=_runner,
        )
        with patcher:
            error = audit_runner._validate_file_scope_manifest(
                manifest, target_root, touched_files=committed,
            )
        assert error is None

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

    SAFETY CONTEXT (SA-0MU8EKJYY007PT42): On 2026-09-19, the _init_repo
    fixture committed against the live repo when pytest's tmp_path resolved
    to the project root (author T <t@t.com>, commit 2c30dbfe). The fixture
    now asserts that tmp_path is not inside any git repository — neither the
    repository root itself nor any path nested within a checkout — before
    proceeding.
    Root cause: pytest's tmp_path / --basetemp can be overridden by TMPDIR
    environment variable or by the test-skill runner's working directory.
    When run_tests.py invoked pytest from the project root without an
    explicit --basetemp, tmp_path fell back to the live repo.
    """

    ROOT_FILES: ClassVar[list[str]] = [
        "install.sh", "remote", "sshl", "update", "ai.home.conf"
    ]
    SHARED_SUBDIR = "tests"  # present in the framework repo too -> not distinctive

    @staticmethod
    def _init_repo(tmp_path: Path) -> Path:
        """Init a real git repo whose distinctive markers are all root files.

        SAFETY GUARD: refuses to run if *tmp_path* sits inside any git work
        tree (the sandbox helper rejects both the live repo root and any path
        nested inside an enclosing repository). This preserves the historical
        incident guard (SA-0MU8EKJYY007PT42): the fixture's ``git add`` /
        ``git commit`` must never touch the live checkout.
        """
        repo = init_repo(tmp_path / "root-file-repo")
        run_git(repo, ["config", "user.email", "t@t.com"], check=True)
        run_git(repo, ["config", "user.name", "T"], check=True)
        for name in TestRootFileOnlyRepoManifest.ROOT_FILES:
            (repo / name).write_text("x\n", encoding="utf-8")
        shared = repo / TestRootFileOnlyRepoManifest.SHARED_SUBDIR
        shared.mkdir()
        (shared / "test_a.py").write_text("x\n", encoding="utf-8")
        commit_all(repo, "init")
        return repo

    @staticmethod
    def _git_runner(repo: Path):
        """Runner resolving git commands against the fixture repo.

        Hermetic environment (``fixture_git_env``): the finish gate exports a
        temporary ``GIT_INDEX_FILE`` that a fixture command must never inherit
        (SA-0MVWS6G0Z0050PD2).
        """
        def runner(cmd):
            if cmd and cmd[0] == "git":
                cmd = ["git", "-C", str(repo)] + list(cmd[1:])
            return subprocess.run(cmd, capture_output=True, text=True,
                                  check=False, env=fixture_git_env())
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

    def test_foreign_gate_index_does_not_replace_the_repo_index(
        self, tmp_path, monkeypatch
    ):
        """An inherited foreign ``GIT_INDEX_FILE`` must not mask the markers.

        ``implement.py finish`` exports a temporary would-be-commit index for
        the suite it runs (SA-0MVQHVGFR006W8LG), and ``git ls-files`` applies
        an inherited index to whatever repository ``cwd`` names. Without a
        per-repository scrub ``_project_top_levels`` reported the *gate's*
        top-level paths for every repository, so the distinctive markers
        collapsed to ``[]`` and the scope validation silently validated a
        manifest against the wrong tree — the two root-file-marker tests then
        failed only inside the finish gate (SA-0MVWJ6U3J003TLRZ).
        """
        repo = self._init_repo(tmp_path)
        foreign = init_repo(tmp_path / "foreign-repo")
        (foreign / "skill").mkdir()
        (foreign / "skill" / "unrelated.py").write_text("x\n", encoding="utf-8")
        commit_all(foreign, "foreign")

        monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))

        assert audit_runner._project_top_levels(repo) == sorted(
            self.ROOT_FILES + [self.SHARED_SUBDIR]
        ), "the inspected repo's own index must decide its top-level paths"
        assert set(audit_runner._distinctive_project_top_levels(repo)) == set(
            self.ROOT_FILES
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

    def test_init_repo_refuses_live_repo_tmp_path(self):
        """AC1 regression: _init_repo refuses any tmp_path inside a git repo.

        Reproduces the guard for the SA-0MU8EKJYY007PT42 incident where
        tmp_path resolved to the live project root and the fixture committed
        into the repo, deleting 472 files. The guard rejects both the repo
        root itself and any path nested inside an enclosing repository, since
        a nested path would still let the fixture create and commit a
        repository inside the live checkout.
        """
        for unsafe_path in (REPO_ROOT, REPO_ROOT / "skill"):
            with pytest.raises(GitSandboxError) as exc_info:
                self._init_repo(unsafe_path)
            assert "inside git work tree" in str(exc_info.value)

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
            # The scope guards are out of scope for these child-persist tests:
            # a Phase 1 scope error is now FATAL (SA-0MWO59IMN000RUHM) and would
            # abort before the child is ever audited, hiding the behaviour under
            # test. An empty touched set keeps both guards fail-open.
            mock.patch.object(
                audit_runner, "_resolve_touched_files", return_value=[]
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
            # Mock the child-audit subprocess spawn so the test does not
            # execute a real audit_runner.py issue subprocess (which would
            # trigger F3 auto-execution and potentially recurse into the
            # full suite under a cold cache — SA-0MUG47DYG006TV40 AC3).
            mock.patch.object(audit_runner.subprocess, "run",
                              return_value=mock.MagicMock(returncode=0)),
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


class TestPerAuditRootConsumers:
    """The per-audit target root is consumed by every residual
    ``TARGET_PROJECT_ROOT`` reader (SA-0MULGDABC001T11L).

    A foreign-cwd launch with a determinable owner resolves the per-audit
    root to the owning project, so the code-quality scan, the repo-index
    fallback, the debug-log slug, and the F3 suite-execution cwd all target
    the audited project — never the launch cwd. A worktree launch keeps
    resolving to the worktree checkout (same git repository).
    """

    def test_foreign_cwd_code_quality_targets_owning_root(self, tmp_path):
        """The code-quality scan receives the owning project as
        ``project_root``, not the launch cwd.
        """
        target, target_root, patcher = _make_sibling_projects(tmp_path)
        launch_root = tmp_path / "skill-install-dir"
        launch_root.mkdir()
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", launch_root),
            # A non-empty code-quality scope is required for the scan to run
            # at all: an empty scope is skipped by design
            # (SA-0MUI5ZYO6002W3KD).
            mock.patch.object(audit_runner, "_git_changed_files",
                              return_value=["src/x.py"]),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality", mock_cq
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
                worklog_dir=str(target),
            )

        assert rc == 0
        mock_cq.assert_called_once()
        assert mock_cq.call_args.kwargs["project_root"] == target_root

    def test_foreign_cwd_suite_execution_cwd_targets_owning_root(
        self, tmp_path
    ):
        """The F3 auto-green / test-skill suite-execution cwd is the owning
        project, not the launch cwd.
        """
        target, target_root, patcher = _make_sibling_projects(tmp_path)
        launch_root = tmp_path / "skill-install-dir"
        launch_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", launch_root),
            mock.patch.object(
                audit_runner, "_auto_green_run_outcome",
                return_value=(None, None, audit_runner._FULL_SUITE_CACHE_MISS),
            ) as auto_mock,
            mock.patch.object(
                audit_runner, "_run_tests_via_test_skill",
                return_value={"success": False},
            ) as run_mock,
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
                worklog_dir=str(target),
                run_tests=True,
            )

        assert rc == 0
        auto_mock.assert_called_once()
        assert auto_mock.call_args.kwargs["cwd"] == str(target_root)
        run_mock.assert_called_once()
        assert run_mock.call_args.kwargs["cwd"] == target_root

    def test_foreign_cwd_debug_log_dir_targets_owning_root(self, tmp_path):
        """The debug-log directory slug is derived from the owning project,
        so debug artefacts are attributable to the audited project.
        """
        target, target_root, patcher = _make_sibling_projects(tmp_path)
        launch_root = tmp_path / "skill-install-dir"
        launch_root.mkdir()

        with (
            patcher,
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", launch_root),
            mock.patch.object(
                audit_runner, "_debug_log_dir",
                wraps=audit_runner._debug_log_dir,
            ) as debug_dir_spy,
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(),
                worklog_dir=str(target),
            )

        assert rc == 0
        assert debug_dir_spy.called
        roots = [
            (call.args[0] if call.args else call.kwargs.get("project_root"))
            for call in debug_dir_spy.call_args_list
        ]
        assert any(root == target_root for root in roots), roots
        assert all(root != launch_root for root in roots), roots

    def test_repo_index_fallback_uses_per_audit_root(self, tmp_path):
        """When ``git ls-files`` fails, ``_repo_index`` lists the per-audit
        target root's top-level entries (not the launch cwd's).
        """
        _target, target_root, _patcher = _make_sibling_projects(tmp_path)
        launch_root = tmp_path / "skill-install-dir"
        launch_root.mkdir()
        (launch_root / "launch-only").mkdir()

        def failing_git_runner(cmd):
            if cmd and cmd[0] == "git":
                return SimpleNamespace(returncode=1, stdout="", stderr="fatal")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", launch_root):
            index = audit_runner._repo_index(
                failing_git_runner, project_root=target_root
            )

        assert any("src" in entry for entry in index), index
        assert not any("launch-only" in entry for entry in index), index

    def test_worktree_launch_per_audit_root_is_worktree(self, tmp_path):
        """A worktree launch keeps the per-audit root at the worktree
        checkout (same git repository), not the --worklog-dir parent.
        """
        (worklog_dir, _owning_root, worktree_path, _main_head,
         _worktree_head) = _make_real_git_project_with_worktree(tmp_path)
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        with (
            mock.patch.object(
                audit_runner, "TARGET_PROJECT_ROOT", worktree_path
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality", mock_cq
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(git_cwd=worktree_path),
                worklog_dir=str(worklog_dir),
            )

        assert rc == 0
        assert mock_cq.call_args.kwargs["project_root"] == worktree_path


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

    init_repo(owning_root, default_branch="main", sandbox_root=tmp_path)
    commit_all(owning_root, "main")
    main_head = run_git(owning_root, ["rev-parse", "HEAD"], check=True).stdout.strip()

    worktree_path = (worklog_dir / "worktrees" / "wl-OSL-1-test").resolve()
    add_worktree(owning_root, worktree_path, "wl-OSL-1-test")
    # worktree-only tracked file (committed on the worktree branch).
    (worktree_path / "wt_only").mkdir()
    (worktree_path / "wt_only" / "main.py").write_text("print('wt')\n")
    commit_all(worktree_path, "worktree-only")
    # worktree-only untracked file (uncommitted working-tree state).
    (worktree_path / "wt_uncommitted.txt").write_text("wt state\n")
    worktree_head = run_git(
        worktree_path, ["rev-parse", "HEAD"], check=True
    ).stdout.strip()
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
        # the changed-files list.  (Manifest is built inside the patch
        # context so TARGET_PROJECT_ROOT is correct for the existence
        # filter in _git_changed_files — SA-0MSXVXVUL0011JKX.)
        manifest = audit_runner._build_file_scope_manifest(
            {}, [], runner=runner
        )
        assert "wt_only" in manifest, (
            f"manifest must reflect worktree-only files: {manifest!r}"
        )
        # Direct _git_changed_files call must also be inside the patch
        # context for the TARGET_PROJECT_ROOT-based existence filter to
        # resolve paths correctly.
        with mock.patch.object(
            audit_runner, "TARGET_PROJECT_ROOT", worktree_path
        ):
            changed = audit_runner._git_changed_files(runner)
        assert "wt_uncommitted.txt" in changed, (
            f"changed files must reflect the worktree working tree: {changed}"
        )


# ===========================================================================
# Stale-sibling isolation (SA-0MUIMLPLH006TKY1)
# ===========================================================================


class TestStaleSiblingIsolation:
    """Flow tests must not resolve synthetic ids through the real scan root.

    Regression for SA-0MUIMLPLH006TKY1:
    ``TestLifecycleVerification.test_swallowed_no_update_fails_nonzero``
    audits the synthetic id ``TEST-1``. The real prefix-to-sibling scan root
    is ``REPO_ROOT.parent``; a stale ``TEST``-prefixed worklog left behind on
    the host (e.g. ``/tmp/wlfields-test`` from an earlier suite) matched that
    prefix and made the launch-context guard reject the item, so the lifecycle
    test failed on otherwise-unrelated hosts. The autouse ownership fixture now
    redirects the scan to an empty isolated directory, keeping flow tests
    hermetic.
    """

    def test_scan_root_is_isolated_from_real_siblings(self):
        """The active sibling scan root must not be the real repo parent, and
        must expose no prefix-matching worklog to synthetic-id flow tests."""
        from shared import status_lifecycle as shared

        assert shared.SIBLING_SCAN_ROOT != shared.REPO_ROOT.parent, (
            "flow tests must not scan the real sibling root where stale "
            "prefix-matching worklogs can hijack synthetic ids"
        )
        assert shared._find_worklog_dir_by_prefix("TEST") is None

    def test_synthetic_id_resolves_to_launch_root(self):
        """With the scan isolated, ``TEST-1`` resolves to the launch project
        root, so the launch-context guard does not abort the flow test."""
        assert (
            audit_runner._resolve_owning_project_root("TEST-1")
            == audit_runner.TARGET_PROJECT_ROOT
        )


# ===========================================================================
# AC1 (LP-0MWDDRTHE00981PK): cross-repo code-root — a work item tracked in
# worklog repo A whose code lives in repo B is audited with the explicit
# ``--code-root <repo-B>`` so every git-derived surface (merge gate,
# file-scope manifest, changed files, green-run evidence, code-quality
# scope) resolves against B while all wl reads/writes stay on A's worklog.
#
# TEST-FIRST CONTRACT — expected red until the implementation items land
# (LP-0MWDDRVQ7005KOHN ``--code-root``/per-audit-root wiring, LP-0MWDDRWBG0013CS1
# cross-repo detection). The markers were removed once the production
# change landed (LP-0MWDDRVQ7005KOHN --code-root / AUDIT_CODE_ROOT wiring).
# ===========================================================================

#: Item description used by the cross-repo fixture: Key Files live in code
#: repo B (``src/codeb/feature.py``); no Acceptance Criteria heading, so the
#: Phase 1 parent screen is skipped (no pi invocation — the flow stays
#: hermetic while the git-derived scope is still exercised).
_CROSS_REPO_DESCRIPTION = (
    "## Key Files (predicted)\n"
    "- `src/codeb/feature.py`\n"
)


def _make_cross_repo_pair(tmp_path: Path, prefix: str = "OSL"):
    """Create TWO real temp git repos: worklog repo A + code repo B.

    Layout::

        <tmp>/projects/llm/                  (repo A — worklog owner)
            .worklog/config.yaml             (prefix: OSL)
            .gitignore                       (ignores .worklog/)
            admin/registry.txt               (A-only distinctive marker)
        <tmp>/projects/code/                 (repo B — code repo)
            src/codeb/feature.py             (Key File; commit msg: OSL-1)
            src/codeb/wip.py                 (uncommitted working-tree change)

    Creates real git repositories for BOTH A and B (the AC1 contract demands
    two real temp git repos). The ``wl-OSL-1-feature`` branch in B gives the
    merge gate resolvable integration evidence inside B; B has no ``dev``
    baseline, so the gate fails open and never attempts a push.

    Returns ``(worklog_dir, repo_a_root, repo_b_root)``.
    """
    projects = tmp_path / "projects"
    # Repo A — the worklog-owning project (real git repo).
    repo_a = projects / "llm"
    (repo_a / "admin").mkdir(parents=True)
    (repo_a / "admin" / "registry.txt").write_text("a-registry\n")
    (repo_a / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    worklog_dir = repo_a / ".worklog"
    worklog_dir.mkdir()
    (worklog_dir / "config.yaml").write_text(
        f"projectName: LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    init_repo(repo_a, default_branch="main", sandbox_root=tmp_path)
    commit_all(repo_a, "worklog repo initial")

    # Repo B — the code repository the item's delivered files live in.
    repo_b = projects / "code"
    (repo_b / "src" / "codeb").mkdir(parents=True)
    (repo_b / "src" / "codeb" / "feature.py").write_text("x = 1\n")
    init_repo(repo_b, default_branch="main", sandbox_root=tmp_path)
    commit_all(repo_b, "OSL-1: add codeb feature")
    # Merge-gate integration evidence lives in B (a wl-<id>-* branch).
    run_git(repo_b, ["branch", "wl-OSL-1-feature"], check=True)
    # Uncommitted working-tree change so changed-files resolution is exercised.
    (repo_b / "src" / "codeb" / "wip.py").write_text("y = 2\n")

    return worklog_dir, repo_a, repo_b


class TestCrossRepoCodeRoot:
    """AC1 (LP-0MWDDRTHE00981PK): ``--code-root`` decouples the git-derived
    audit scope from the worklog-owning repository.

    Work item ``OSL-1`` is tracked in worklog repo A; its delivered code
    lives in repo B. Audited with the explicit code root, every git-derived
    surface (merge gate, file-scope manifest, changed files, green-run
    evidence, code-quality scope) must resolve against B while all wl
    reads/writes still target A's worklog — the two roots are independent.
    """

    def test_cross_repo_code_root_resolves_git_scope_to_code_repo(
        self, tmp_path,
    ):
        """AC1: with ``--code-root <B>`` the merge gate, file-scope manifest,
        changed files, green-run evidence and code-quality scope all resolve
        against repo B, while every wl read/write stays on worklog repo A.
        """
        worklog_a, repo_a, repo_b = _make_cross_repo_pair(tmp_path)
        repo_b_head = run_git(
            repo_b, ["rev-parse", "HEAD"], check=True
        ).stdout.strip()
        recorded: list[list[str]] = []
        runner = _make_minimal_runner(
            recorded, description=_CROSS_REPO_DESCRIPTION, git_cwd=repo_b,
        )
        mock_cq = mock.MagicMock(
            return_value={"success": True, "findings": [], "fixes_applied": 0}
        )

        with (
            # Launch from the worklog owner (repo A); code root is repo B.
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", repo_a),
            mock.patch.object(
                audit_runner, "_auto_green_run_outcome",
                wraps=audit_runner._auto_green_run_outcome,
            ) as auto_spy,
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality", mock_cq
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True, runner=runner,
                worklog_dir=str(worklog_a), code_root=str(repo_b),
            )

        assert rc == 0, "cross-repo audit with --code-root must exit green"

        # -- Merge gate, changed files, manifest, HEAD: every git command
        # issued by the runner resolves against code repo B (never A / the
        # launch root).
        git_cmds = [c for c in recorded if c and c[0] == "git"]
        assert git_cmds, "expected the cross-repo audit to run git commands"
        for git_cmd in git_cmds:
            assert git_cmd[1] == "-C", f"git command lacks -C: {git_cmd}"
            assert Path(git_cmd[2]).resolve() == repo_b.resolve(), (
                f"git command must resolve against code repo B: {git_cmd}"
            )
        # The merge gate's ref scan runs against B.
        assert any(
            "for-each-ref" in " ".join(cmd) for cmd in git_cmds
        ), "merge gate must run for-each-ref against code repo B"
        # Audited HEAD is code repo B's HEAD.
        assert audit_runner._resolve_audited_head(runner) == repo_b_head

        # -- Green-run evidence resolves the test-cache cwd to B.
        assert auto_spy.called, "expected the automatic green-run path to run"
        assert Path(
            auto_spy.call_args.kwargs["cwd"]
        ).resolve() == repo_b.resolve(), (
            "green-run evidence must resolve against code repo B"
        )

        # -- Code-quality scope resolves to B and references B's files
        # (either the changed-files path or the touched/Key-Files path —
        # both must stay inside code repo B).
        mock_cq.assert_called_once()
        assert Path(
            mock_cq.call_args.kwargs["project_root"]
        ).resolve() == repo_b.resolve(), (
            "code-quality scope must target code repo B"
        )
        assert any(
            "src/codeb/" in str(f)
            for f in mock_cq.call_args.kwargs.get("files", [])
        ), "code-quality scope must reference code repo B's files"

        # -- File-scope manifest resolves to B: B's Key File and B's repo
        # index appear; worklog repo A's marker never does.
        with mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", repo_b):
            manifest = audit_runner._build_file_scope_manifest(
                {"id": "OSL-1", "description": _CROSS_REPO_DESCRIPTION},
                [], runner=runner, project_root=repo_b,
            )
        assert "src/codeb/feature.py" in manifest, manifest
        assert "src/" in manifest, f"manifest lacks code repo B's index: {manifest}"
        assert "admin" not in manifest, (
            f"manifest must not leak worklog repo A's marker: {manifest}"
        )

        # -- Changed files resolve to B's working tree.
        with mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", repo_b):
            changed = audit_runner._git_changed_files(runner)
        assert "src/codeb/wip.py" in changed, (
            f"changed files must reflect code repo B: {changed}"
        )

        # -- wl reads/writes all target worklog repo A — never repo B.
        wl_cmds = [c for c in recorded if c and c[0] == "wl"]
        assert wl_cmds, "expected wl commands from the cross-repo audit"
        for wl_cmd in wl_cmds:
            assert "--worklog-dir" in wl_cmd, wl_cmd
            assert str(worklog_a) in wl_cmd, (
                f"wl command must target worklog repo A: {wl_cmd}"
            )
            assert str(repo_b) not in " ".join(wl_cmd), (
                f"wl command must never reference the code repo: {wl_cmd}"
            )

    def test_cli_exposes_code_root_flag(self, tmp_path):
        """The ``issue`` subcommand parses ``--code-root <dir>`` into a
        ``code_root`` value (the CLI surface AC1 invokes).
        """
        _worklog_a, _repo_a, repo_b = _make_cross_repo_pair(tmp_path)
        args = audit_runner.build_parser().parse_args([
            "issue", "OSL-1", "--code-root", str(repo_b),
        ])
        assert args.code_root == str(repo_b)


# ===========================================================================
# AC2 (LP-0MWDDRU1P0067SUV): cross-repo DETECTION — without an explicit code
# root, a work item whose delivered files live only in a SIBLING project's
# repository must fail CLOSED with a scope error that names the sibling
# repo, before any Phase 1 model call (zero pi invocations), with a non-zero
# exit and no persisted report.
#
# This is the sibling of the AC1 cross-repo ``--code-root`` test above: AC1
# proves the explicit code root works; AC2 proves the *absence* of one is
# refused loudly rather than silently mis-scoped.
#
# TEST-FIRST CONTRACT — implemented by LP-0MWDDRWBG0013CS1 "Implement
# cross-repo detection: fail closed naming sibling candidates". The markers
# were removed when the production change landed (precedent: LP-0MWDDRTHE00981PK).
# ===========================================================================


#: Item description for the detection fixture: the Key Files exist ONLY in
#: the sibling code repo. An ``## Acceptance Criteria`` section keeps the
#: Phase 1 parent screen live (so "zero pi invocations" is a real assertion —
#: pre-fix the run *would* reach Phase 1 and call pi).
_CROSS_REPO_DETECTION_DESCRIPTION = (
    "## Acceptance Criteria\n"
    "- AC1: the delivered feature behaves as specified.\n"
    "\n"
    "## Key Files (predicted)\n"
    "- `src/codeb/feature.py`\n"
)

#: Same-repo control description: the Key File lives in the OWNING repo, so
#: the cross-repo guard must not fire (no false abort).
_SAME_REPO_DESCRIPTION = (
    "## Acceptance Criteria\n"
    "- AC1: the delivered feature behaves as specified.\n"
    "\n"
    "## Key Files (predicted)\n"
    "- `src/app/main.py`\n"
)


def _make_cross_repo_detection_fixture(tmp_path: Path, prefix: str = "OSL"):
    """Two real temp git repos plus a discoverable sibling scan root.

    Layout::

        <tmp>/projects/
            llm/                               (repo A — worklog owner)
                .worklog/config.yaml           (prefix: OSL)
                .gitignore                     (ignores .worklog/)
                src/app/main.py                (A's OWN marker; distinctive)
            code/                              (repo B — sibling code repo)
                .worklog/config.yaml           (prefix: CODE — scanable)
                src/codeb/feature.py           (the item's Key File)

    Both are real git repositories on ``main`` (no ``dev`` baseline ⇒ the
    Phase 1 merge gate fails open, never attempting integration). Repo A
    carries a ``src/`` marker so its FILE SCOPE manifest is *valid* — the
    fail-closed assertion therefore cannot be satisfied by the pre-existing
    manifest scope error; it must come from the new cross-repo detection.

    Returns ``(worklog_a, repo_a, repo_b, patcher)``.
    """
    projects = tmp_path / "projects"

    # Repo A — the worklog-owning project (real git repo).
    repo_a = projects / "llm"
    (repo_a / "src" / "app").mkdir(parents=True)
    (repo_a / "src" / "app" / "main.py").write_text("app = 1\n")
    (repo_a / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    worklog_a = repo_a / ".worklog"
    worklog_a.mkdir()
    (worklog_a / "config.yaml").write_text(
        f"projectName: LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    init_repo(repo_a, default_branch="main", sandbox_root=tmp_path)
    commit_all(repo_a, "worklog repo initial")

    # Repo B — a SIBLING project whose repository holds the item's files.
    # Its own .worklog/config.yaml makes it part of the scan population.
    repo_b = projects / "code"
    (repo_b / "src" / "codeb").mkdir(parents=True)
    (repo_b / "src" / "codeb" / "feature.py").write_text("x = 1\n")
    sibling_worklog = repo_b / ".worklog"
    sibling_worklog.mkdir()
    (sibling_worklog / "config.yaml").write_text(
        "projectName: Code\nprefix: CODE\n", encoding="utf-8"
    )
    init_repo(repo_b, default_branch="main", sandbox_root=tmp_path)
    commit_all(repo_b, "add codeb feature")

    patcher = mock.patch(
        "shared.status_lifecycle.SIBLING_SCAN_ROOT", projects
    )
    return worklog_a, repo_a, repo_b, patcher


def _fake_pi_met(issue_id, context, prompt, **kwargs):
    """Canned Phase 1/2 pi response (met) so a pre-fix run completes."""
    return {
        "extracted_text": json.dumps([
            {"index": 0, "verdict": "met", "evidence": "src/codeb/feature.py:1"},
        ]),
    }


class TestCrossRepoDetectionFailsClosed:
    """AC2 (LP-0MWDDRU1P0067SUV): absent an explicit code root, a cross-repo
    item is refused loudly — naming the sibling repo — before any model call.
    """

    def _run(self, tmp_path, *, description, recorded=None):
        worklog_a, repo_a, repo_b, patcher = (
            _make_cross_repo_detection_fixture(tmp_path)
        )
        recorded = [] if recorded is None else recorded
        pi_mock = mock.MagicMock(side_effect=_fake_pi_met)
        persist_mock = mock.MagicMock(return_value=0)
        with (
            patcher,
            # Launch from the owning project (repo A), no --code-root.
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", repo_a),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", pi_mock,
            ),
            mock.patch.object(audit_runner, "persist_audit", persist_mock),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue(
                "OSL-1", persist=False, force=True,
                runner=_make_minimal_runner(
                    recorded=recorded, description=description, git_cwd=repo_a,
                ),
                worklog_dir=str(worklog_a),
            )
        return rc, pi_mock, persist_mock, repo_b

    def test_cross_repo_item_without_code_root_fails_closed(self, tmp_path,
                                                            capsys):
        """AC2/AC3/AC5: no ``--code-root`` on a cross-repo item aborts with a
        non-zero exit and a scope error that NAMES the sibling repo holding
        the item's touched files, and no report is emitted.
        """
        rc, _pi, persist_mock, repo_b = self._run(
            tmp_path, description=_CROSS_REPO_DETECTION_DESCRIPTION,
        )
        captured = capsys.readouterr()
        err = captured.err

        assert rc == 1, "a cross-repo item without --code-root must fail closed"
        # Same style as the existing "Audit scope error" paths.
        assert "Audit scope error" in err, err
        # The error names the SIBLING repository that holds the files — the
        # discriminating signal that this is the cross-repo detection, not the
        # pre-existing manifest scope error (which names the owning root).
        assert str(repo_b) in err, (
            f"scope error must name the sibling code repo {repo_b}: {err}"
        )
        # No persisted report, no report text at all (the abort happens
        # before report assembly).
        persist_mock.assert_not_called()
        assert "Ready to close" not in captured.out + err, captured.out

    def test_fail_closed_before_phase1_zero_pi_invocations(self, tmp_path):
        """AC4: the abort happens BEFORE any Phase 1 model call — zero pi
        invocations (fail closed, fail fast).
        """
        _rc, pi_mock, _persist, _repo_b = self._run(
            tmp_path, description=_CROSS_REPO_DETECTION_DESCRIPTION,
        )
        pi_mock.assert_not_called()

    # -- Controls (must stay green before AND after the change) -------------

    def test_same_repo_item_is_not_rejected(self, tmp_path, capsys):
        """Guard is not over-eager: an item whose Key Files live in the owning
        repo proceeds to Phase 1 (pi is called) with no cross-repo abort.
        """
        rc, pi_mock, _persist, _repo_b = self._run(
            tmp_path, description=_SAME_REPO_DESCRIPTION,
        )
        err = capsys.readouterr().err
        assert rc == 0, err
        assert pi_mock.called, "same-repo item must still reach Phase 1"
        assert "cross-repo" not in err, err

    def test_undeterminable_touched_set_fails_open(self, tmp_path, capsys):
        """An undeterminable touched set (no Key Files, no item commits) skips
        the detection (fail open) — never a false abort.
        """
        rc, pi_mock, _persist, _repo_b = self._run(
            tmp_path,
            description="## Acceptance Criteria\n- AC1: the thing works.\n",
        )
        err = capsys.readouterr().err
        assert rc == 0, err
        assert pi_mock.called, "undeterminable scope must not fail closed"


# ===========================================================================
# AC3 (LP-0MWDDRUMH002LRZR): same-repo byte-for-byte contract — a same-repo
# item (worklog owner IS the code repo) audited WITH an explicit
# ``--code-root`` pointing at the owning repo must behave byte-for-byte
# identically to one audited WITHOUT it (identical file-scope manifest, HEAD
# attestation and verdict); with the flag absent, the resolved code root is
# the owning project root (default unchanged).
#
# This is the control for the AC1/AC2 cross-repo contract: the new option
# must be a strict no-op when its value equals the default resolution.
#
# TEST-FIRST CONTRACT — expected red until the implementation item lands
# (LP-0MWDDRVQ7005KOHN ``--code-root``/per-audit-root wiring). Marked
# ``xfail(strict=False)`` so the suite stays green while the contract is
# documented; the markers were removed once the production change landed
# (LP-0MWDDRVQ7005KOHN --code-root / AUDIT_CODE_ROOT).
# ===========================================================================


def _make_same_repo_fixture(tmp_path: Path, prefix: str = "OSL"):
    """One real git repo where the worklog owner IS the code repo.

    Layout::

        <tmp>/projects/llm/                  (repo — worklog owner + code)
            .worklog/config.yaml             (prefix: OSL)
            .gitignore                       (ignores .worklog/)
            src/app/main.py                  (Key File; commit msg: OSL-1)
            src/app/wip.py                   (uncommitted working-tree change)

    Returns ``(worklog_dir, repo_root)``. ``main`` has no ``dev`` baseline,
    so the merge gate fails open and never attempts an integration push.
    """
    projects = tmp_path / "projects"
    repo = projects / "llm"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "app" / "main.py").write_text("app = 1\n")
    (repo / ".gitignore").write_text(".worklog/\n", encoding="utf-8")
    worklog_dir = repo / ".worklog"
    worklog_dir.mkdir()
    (worklog_dir / "config.yaml").write_text(
        f"projectName: LLM\nprefix: {prefix}\n", encoding="utf-8"
    )
    init_repo(repo, default_branch="main", sandbox_root=tmp_path)
    commit_all(repo, "OSL-1: add app main")
    # Merge-gate integration evidence lives in the owning repo itself.
    run_git(repo, ["branch", "wl-OSL-1-feature"], check=True)
    # Uncommitted working-tree change so changed-files + code-quality scope
    # resolution are exercised (deterministic across both runs).
    (repo / "src" / "app" / "wip.py").write_text("wip = 2\n")
    return worklog_dir, repo


class TestSameRepoCodeRootByteForByte:
    """AC3 (LP-0MWDDRUMH002LRZR): a same-repo item's behaviour is
    byte-for-byte unchanged with and without the new ``--code-root`` option.
    """

    def _capture_run(
        self, worklog_dir: Path, repo: Path, *, code_root: str | None,
    ) -> dict:
        """Run the same-repo item once, capturing the run's artefacts.

        Returns ``{"rc", "manifests", "heads", "pi_calls", "git_cmds"}``:
        the exit code, every file-scope manifest built, every HEAD sha
        resolved, every pi ``(context, prompt)`` pair sent (the deterministic
        verdict inputs), and the git command stream the runner executed.
        """
        manifests: list[str] = []
        heads: list[str] = []
        pi_calls: list[tuple[str, str]] = []
        recorded: list[list[str]] = []
        real_build = audit_runner._build_file_scope_manifest
        real_head = audit_runner._resolve_audited_head

        def recording_build(*args, **kwargs):
            manifest = real_build(*args, **kwargs)
            manifests.append(manifest)
            return manifest

        def recording_head(runner):
            head = real_head(runner)
            heads.append(head)
            return head

        def recording_pi(issue_id, context, prompt, **kwargs):
            pi_calls.append((context, prompt))
            return _fake_pi_met(issue_id, context, prompt, **kwargs)

        cmd_kwargs = {
            "persist": False, "force": True,
            "runner": _make_minimal_runner(
                recorded, description=_SAME_REPO_DESCRIPTION,
                git_cwd=repo,
            ),
            "worklog_dir": str(worklog_dir),
        }
        if code_root is not None:
            cmd_kwargs["code_root"] = code_root
        with (
            # Launch from the owning project root; the item's Key Files live
            # in this same repo (the same-repo case).
            mock.patch.object(audit_runner, "TARGET_PROJECT_ROOT", repo),
            mock.patch.object(
                audit_runner, "_build_file_scope_manifest",
                side_effect=recording_build,
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head",
                side_effect=recording_head,
            ),
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                side_effect=recording_pi,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [],
                              "fixes_applied": 0},
            ),
        ):
            rc = audit_runner.cmd_issue("OSL-1", **cmd_kwargs)
        return {
            "rc": rc,
            "manifests": manifests,
            "heads": heads,
            "pi_calls": pi_calls,
            "git_cmds": [c for c in recorded if c and c[0] == "git"],
        }

    def test_same_repo_byte_for_byte_with_and_without_code_root(self, tmp_path):
        """AC3: the SAME same-repo item run twice — once with an explicit
        ``--code-root <owning-root>``, once without — yields byte-for-byte
        identical file-scope manifests, HEAD attestations, verdict inputs
        (the pi context/prompt stream) and git command streams.
        """
        worklog_dir, repo = _make_same_repo_fixture(tmp_path)
        with_flag = self._capture_run(
            worklog_dir, repo, code_root=str(repo),
        )
        without_flag = self._capture_run(worklog_dir, repo, code_root=None)

        assert with_flag["rc"] == 0, (
            "same-repo audit with --code-root must exit green"
        )
        assert without_flag["rc"] == 0, (
            "same-repo audit without --code-root must exit green"
        )
        # Byte-for-byte: identical file-scope manifest(s), HEAD attestation(s)
        # and pi (context, prompt) stream — identical verdict inputs produce
        # the identical verdict.
        assert with_flag["manifests"] == without_flag["manifests"], (
            "file-scope manifest must be byte-for-byte identical"
        )
        assert with_flag["heads"] == without_flag["heads"], (
            "HEAD attestation must be byte-for-byte identical"
        )
        assert with_flag["pi_calls"] == without_flag["pi_calls"], (
            "verdict inputs (pi context/prompt stream) must be identical"
        )
        # The git command stream is unchanged too — the option is a strict
        # no-op when it equals the default resolution.
        assert with_flag["git_cmds"] == without_flag["git_cmds"], (
            "git command stream must be byte-for-byte identical"
        )

    def test_default_code_root_is_owning_project_root(self, tmp_path):
        """AC3: with the flag ABSENT the resolved code root is the owning
        project root (default unchanged) — every HEAD resolved during the
        run attests the owning repo, never a foreign repository.
        """
        worklog_dir, repo = _make_same_repo_fixture(tmp_path)
        repo_head = run_git(
            repo, ["rev-parse", "HEAD"], check=True
        ).stdout.strip()
        result = self._capture_run(worklog_dir, repo, code_root=None)

        assert result["rc"] == 0
        assert result["heads"], (
            "expected at least one HEAD attestation during the run"
        )
        assert all(head == repo_head for head in result["heads"]), (
            f"default code root must be the owning project root "
            f"({repo_head}): {result['heads']}"
        )
