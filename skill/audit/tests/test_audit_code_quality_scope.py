"""Code-quality scope must stay inside the project and out of nested repos.

The audit's code-quality scan lints the paths a work item touched. Before
SA-0MVPDTSRV0049KW2 that set was not bounded to the audited project: a work
item's ``Key Files`` (or a commit touching a submodule) could drag in files
from a git submodule or from an entirely different repository. The scan then
filed *critical* work items against that foreign code via
``create_quality_epics.py``, which blocked the owning project's release at the
ship skill's critical-items gate.

These tests pin the scope filter (SA-0MVPDTSRV0049KW2):

* AC1 — paths outside the audited project root are dropped.
* AC2 — paths inside a nested git repository (git submodule) are dropped.
* AC3 — BOTH scope sources (git changed files and the touched-files
  resolver) are filtered.
* AC4 — a scope that filters down to nothing skips the scan rather than
  widening to a whole-repository lint.
* AC5 — in-scope paths are returned unchanged and in order (never widened,
  never re-ordered).
* AC6 — resolution is symlink/``..``-safe and never raises; an unresolvable
  path is dropped, and a missing project root falls back to the launch root.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


def _proc(returncode: int = 0, stdout: str = ""):
    """Build a canned CompletedProcess for git subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="",
    )


def _make_project(root: Path) -> Path:
    """Create a minimal project tree with an in-project module."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return root / "src" / "app.py"


def _make_submodule(root: Path, name: str = "packages/vendor") -> Path:
    """Create a nested git repository (a submodule-shaped dir) with a file."""
    submodule = root / name
    (submodule / "scripts").mkdir(parents=True)
    # A real submodule's ``.git`` is a FILE (gitlink), not a directory.
    (submodule / ".git").write_text("gitdir: ../../.git/modules/vendor\n",
                                    encoding="utf-8")
    target = submodule / "scripts" / "tool.py"
    target.write_text("y = 2\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# AC1 — outside-project paths are dropped
# ---------------------------------------------------------------------------


def test_absolute_path_outside_project_root_is_dropped(tmp_path):
    project = tmp_path / "project"
    in_scope = _make_project(project)

    sibling = tmp_path / "sibling-repo"
    (sibling / "skill").mkdir(parents=True)
    foreign = sibling / "skill" / "impl.py"
    foreign.write_text("z = 3\n", encoding="utf-8")

    kept = audit_runner._filter_code_quality_scope(
        [str(in_scope), str(foreign)], project_root=project,
    )

    assert kept == [str(in_scope)]


def test_parent_relative_path_escaping_project_is_dropped(tmp_path):
    """AC6 — ``..`` segments are resolved before the containment test."""
    project = tmp_path / "project"
    _make_project(project)

    sibling = tmp_path / "sibling-repo"
    (sibling / "skill").mkdir(parents=True)
    (sibling / "skill" / "impl.py").write_text("z = 3\n", encoding="utf-8")

    kept = audit_runner._filter_code_quality_scope(
        ["../sibling-repo/skill/impl.py"], project_root=project,
    )

    assert kept == []


# ---------------------------------------------------------------------------
# AC2 — nested repositories (submodules) are dropped
# ---------------------------------------------------------------------------


def test_submodule_file_is_dropped_and_project_file_kept(tmp_path):
    project = tmp_path / "project"
    in_scope = _make_project(project)
    submodule_file = _make_submodule(project)

    kept = audit_runner._filter_code_quality_scope(
        [str(in_scope), str(submodule_file)], project_root=project,
    )

    assert kept == [str(in_scope)]


def test_relative_submodule_path_is_dropped(tmp_path):
    project = tmp_path / "project"
    in_scope = _make_project(project)
    _make_submodule(project)

    kept = audit_runner._filter_code_quality_scope(
        ["packages/vendor/scripts/tool.py", "src/app.py"],
        project_root=project,
    )

    assert kept == ["src/app.py"]
    assert str(in_scope)  # in-project path survives through the relative form


def test_nested_git_directory_marker_also_marks_a_nested_repo(tmp_path):
    """Some nested checkouts carry a ``.git`` DIRECTORY, not a gitlink file."""
    project = tmp_path / "project"
    _make_project(project)
    nested = project / "vendor" / "lib"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    (nested / "mod.py").write_text("w = 4\n", encoding="utf-8")

    kept = audit_runner._filter_code_quality_scope(
        ["vendor/lib/mod.py"], project_root=project,
    )

    assert kept == []


def test_vendored_directory_without_git_marker_is_kept(tmp_path):
    """AC5 — a plain vendored directory is project code: never filtered."""
    project = tmp_path / "project"
    _make_project(project)
    vendored = project / "vendor" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "mod.py").write_text("w = 4\n", encoding="utf-8")

    kept = audit_runner._filter_code_quality_scope(
        ["vendor/lib/mod.py"], project_root=project,
    )

    assert kept == ["vendor/lib/mod.py"]


# ---------------------------------------------------------------------------
# AC5 — order preserved, paths unchanged
# ---------------------------------------------------------------------------


def test_in_scope_paths_keep_their_order_and_form(tmp_path):
    project = tmp_path / "project"
    _make_project(project)
    (project / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")

    paths = [
        "src/b.py",
        str(project / "src" / "app.py"),
        "src/b.py",
    ]

    kept = audit_runner._filter_code_quality_scope(paths, project_root=project)

    assert kept == paths


# ---------------------------------------------------------------------------
# AC6 — symlinks and unresolvable paths
# ---------------------------------------------------------------------------


def test_symlink_pointing_outside_project_is_dropped(tmp_path):
    project = tmp_path / "project"
    _make_project(project)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("v = 5\n", encoding="utf-8")
    (project / "src" / "leak.py").symlink_to(outside / "leak.py")

    kept = audit_runner._filter_code_quality_scope(
        ["src/leak.py"], project_root=project,
    )

    assert kept == []


def test_unresolvable_path_is_dropped_without_raising(tmp_path):
    """A NUL byte (or any resolve failure) must drop the entry, not abort."""
    project = tmp_path / "project"
    _make_project(project)

    kept = audit_runner._filter_code_quality_scope(
        ["src/\x00bad.py", "src/app.py"], project_root=project,
    )

    assert kept == ["src/app.py"]


def test_missing_project_root_falls_back_to_launch_root(tmp_path, monkeypatch):
    """AC6 — no explicit root keeps the historical launch-root behaviour."""
    monkeypatch.setattr(audit_runner, "TARGET_PROJECT_ROOT", tmp_path)
    _make_project(tmp_path)

    kept = audit_runner._filter_code_quality_scope(["src/app.py"])

    assert kept == ["src/app.py"]


# ---------------------------------------------------------------------------
# AC3 — both scope sources are filtered
# ---------------------------------------------------------------------------


def test_scope_filters_the_git_changed_file_source(tmp_path):
    project = tmp_path / "project"
    in_scope = _make_project(project)
    submodule_file = _make_submodule(project)

    def runner(cmd):
        if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            # Absolute paths so the pre-existing existence filter (which is
            # rooted at the launch root in unit tests) does not interfere:
            # this test isolates the containment/nested-repo bound.
            return _proc(0, f"{in_scope}\n{submodule_file}\n")
        if cmd[:2] == ["git", "status"]:
            return _proc(0, "")
        raise AssertionError(f"unexpected git call: {cmd}")

    scope = audit_runner._resolve_code_quality_scope(
        runner, "SA-TEST-CHANGED", work_item={"description": ""},
        project_root=project,
    )

    assert scope == [str(in_scope)]
    assert str(submodule_file) not in scope


def test_scope_filters_the_touched_files_source(tmp_path):
    """Key Files outside the project (or in a submodule) never reach the scan."""
    project = tmp_path / "project"
    _make_project(project)
    _make_submodule(project)

    foreign = tmp_path / "other-repo" / "skill" / "impl.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("q = 6\n", encoding="utf-8")

    description = (
        "## Key Files\n\n"
        f"- {foreign}\n"
        "- packages/vendor/scripts/tool.py\n"
        "- src/app.py\n"
    )

    def runner(cmd):
        if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            return _proc(0, "")
        if cmd[:2] == ["git", "status"]:
            return _proc(0, "")
        if cmd[:2] == ["git", "log"]:
            return _proc(0, "")  # no commits reference the id
        raise AssertionError(f"unexpected git call: {cmd}")

    scope = audit_runner._resolve_code_quality_scope(
        runner, "SA-TEST-TOUCHED", work_item={"description": description},
        project_root=project,
    )

    assert scope == ["src/app.py"]


# ---------------------------------------------------------------------------
# AC4 — empty filtered scope skips the scan (no whole-repo fallback)
# ---------------------------------------------------------------------------


def test_scope_is_empty_when_every_path_is_out_of_bounds(tmp_path):
    project = tmp_path / "project"
    _make_project(project)
    submodule_file = _make_submodule(project)

    def runner(cmd):
        if cmd[:4] == ["git", "diff", "--name-only", "HEAD"]:
            return _proc(0, "")
        if cmd[:2] == ["git", "status"]:
            return _proc(0, "")
        if cmd[:2] == ["git", "log"]:
            return _proc(0, "")
        raise AssertionError(f"unexpected git call: {cmd}")

    scope = audit_runner._resolve_code_quality_scope(
        runner, "SA-TEST-EMPTY",
        work_item={"description": f"## Key Files\n\n- {submodule_file}\n"},
        project_root=project,
    )

    # ``[]`` is the documented "lint nothing" signal: the callers skip the
    # scan with ``_CODE_QUALITY_SCOPE_EMPTY_REASON`` rather than linting the
    # whole repository.
    assert scope == []
    assert audit_runner._CODE_QUALITY_SCOPE_EMPTY_REASON


def test_dropped_out_of_scope_paths_are_reported_on_stderr(tmp_path, capsys):
    """Observability: a narrowed scope says so, so a silent skip is visible."""
    project = tmp_path / "project"
    _make_project(project)

    foreign = tmp_path / "other-repo" / "impl.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("q = 6\n", encoding="utf-8")

    audit_runner._filter_code_quality_scope(
        ["src/app.py", str(foreign)], project_root=project,
    )

    err = capsys.readouterr().err
    assert "code-quality scope" in err
    assert "1" in err
