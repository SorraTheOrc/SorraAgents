"""Real-worklog integration tests for plan/intake tree iteration and coverage.

Unlike ``test_tree_coverage.py`` (which injects a fake ``wl`` runner), these
tests exercise the production helpers against a real, ephemeral worklog store
created with the ``wl`` CLI in a temporary project (``tmp_path``). The store is
resolved from the current working directory, so no production internals are
patched — only the process cwd is redirected into the temp project.

Coverage:

  - AC1: multi-level tree fetch and dependency-respecting ordering using real
    ``wl dep`` edges, plus stage advancement via ``apply_coverage_review``.
  - AC2: per-child intake (via the real intake script) plus coverage
    computation across children.
  - AC3: auto-close of an unambiguous gap creates a covering child and advances
    the stage.
  - AC4: an unresolvable conflict leaves the item open with a comment and does
    not advance the stage.
  - AC5: re-running the review/apply step is idempotent.

Related work item: SA-0MTLJUK60000RTK1
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = _REPO_ROOT / "skill"
for _path in (_REPO_ROOT, _SKILLS_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from shared.tree_coverage import (
    apply_coverage_review,
    fetch_descendant_tree,
    order_by_dependencies,
    run_coverage_review,
)

_AGENT = "integration-probe"
_INTAKE_SCRIPT = _SKILLS_ROOT / "intake" / "scripts" / "intake.py"


# ---------------------------------------------------------------------------
# wl helpers (real CLI against the temp store)
# ---------------------------------------------------------------------------


def _run(store: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["wl", "--worklog-dir", str(store), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _json(store: Path, args: list[str]) -> dict:
    proc = _run(store, args)
    assert proc.returncode == 0, (
        f"wl {' '.join(args)} failed: {proc.stdout} {proc.stderr}"
    )
    return json.loads(proc.stdout)


def _create(store, title, acs, parent=None, issue_type="task") -> str:
    description = "## Acceptance Criteria\n" + "".join(f"- {ac}\n" for ac in acs)
    args = [
        "create",
        "--title", title,
        "--description", description,
        "--issue-type", issue_type,
        "--priority", "medium",
        "--json",
    ]
    if parent:
        args.extend(["--parent", parent])
    return _json(store, args)["workItem"]["id"]


def _show(store, item_id) -> dict:
    return _json(store, ["show", item_id, "--json"])["workItem"]


def _children(store, item_id) -> list[dict]:
    # ``wl list --parent`` returns *direct* children (``wl show --children``
    # returns the full descendant set).
    return _json(store, ["list", "--parent", item_id, "--json"])["workItems"]


def _comment_bodies(store, item_id) -> list[str]:
    data = _json(store, ["comment", "list", item_id, "--json"])
    return [c.get("comment", "") for c in data.get("comments", [])]


def _run_intake(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_INTAKE_SCRIPT), *args],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def eco_project(tmp_path, monkeypatch):
    """A temporary, initialized worklog project; cwd is set inside it."""
    project = tmp_path / "eco-project"
    project.mkdir()
    store = project / ".worklog"
    proc = subprocess.run(
        [
            "wl", "init",
            "--worklog-dir", str(store),
            "--project-name", "ECO-INTEGRATION",
            "--prefix", "ECO",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--json",
        ],
        input="\n",  # satisfy any residual interactive prompt
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"wl init failed: {proc.stdout} {proc.stderr}"
    # Production helpers resolve the store from cwd; redirect into the project.
    monkeypatch.chdir(project)
    return project, store


# ---------------------------------------------------------------------------
# AC1 — plan tree iteration
# ---------------------------------------------------------------------------


class TestTreeIterationIntegration:
    def test_multi_level_tree_and_dependency_order(self, eco_project):
        _, store = eco_project
        parent = _create(store, "Parent", ["Parent AC one", "Parent AC two"])
        child_a = _create(store, "Child A", ["AC A"], parent=parent)
        child_b = _create(store, "Child B", ["AC B"], parent=parent)
        grandchild = _create(store, "Grandchild", ["AC G"], parent=child_a)
        # child_a depends on child_b, so dependency order must place child_b
        # first even though child_a was created (and listed) first.
        _run(store, ["dep", "add", child_a, child_b, "--json"])

        tree = fetch_descendant_tree(parent)
        assert set(tree) == {parent, child_a, child_b, grandchild}
        assert set(tree[parent]["children"]) == {child_a, child_b}
        assert tree[child_a]["children"] == [grandchild]
        assert tree[grandchild]["children"] == []

        ordered = [
            c["id"]
            for c in order_by_dependencies(_children(store, parent), parent)
        ]
        assert set(ordered) == {child_a, child_b}
        assert ordered.index(child_b) < ordered.index(child_a), (
            "real wl dependency edge must order child_b before child_a"
        )

    def test_apply_advances_stage_when_covered(self, eco_project):
        _, store = eco_project
        parent = _create(store, "Parent", ["Feature A login"])
        _create(store, "Child", ["Feature A login"], parent=parent)

        result = apply_coverage_review(parent, target_stage="plan_complete")

        assert result["action"] == "proceed"
        assert result["advanced"] is True
        wi = _show(store, parent)
        assert wi["status"] == "open"
        assert wi["stage"] == "plan_complete"


# ---------------------------------------------------------------------------
# AC2 — intake per-child + coverage
# ---------------------------------------------------------------------------


class TestIntakeIntegration:
    def test_per_child_intake_and_coverage(self, eco_project):
        project, store = eco_project
        parent = _create(store, "Parent", ["AC one alpha", "AC two beta"])
        child_a = _create(store, "Child A", ["AC one alpha"], parent=parent)
        child_b = _create(store, "Child B", ["AC two beta"], parent=parent)

        for child_id in (child_a, child_b):
            start = _run_intake(project, "start", child_id, "--assignee", _AGENT)
            assert start.returncode == 0, start.stdout + start.stderr
            complete = _run_intake(project, "auto-complete", child_id)
            assert complete.returncode == 0, complete.stdout + complete.stderr
            wi = _show(store, child_id)
            assert wi["status"] == "open"
            assert wi["stage"] == "intake_complete"

        review = run_coverage_review(parent)
        assert {c["id"] for c in review["child_summary"]} == {child_a, child_b}
        assert review["coverage"]["fully_covered"] is True
        assert review["recommendation"] == "proceed"


# ---------------------------------------------------------------------------
# AC3 / AC4 — coverage mutation
# ---------------------------------------------------------------------------


class TestCoverageMutationIntegration:
    def test_auto_close_creates_child_and_advances(self, eco_project):
        _, store = eco_project
        parent = _create(
            store, "Parent", ["Support user authentication via OAuth login"]
        )
        _create(store, "Child", ["OAuth support"], parent=parent)

        review = run_coverage_review(parent)
        assert review["recommendation"] == "auto_close"
        assert review["resolved_gaps"]

        result = apply_coverage_review(parent, target_stage="plan_complete")

        assert result["action"] == "auto_close"
        assert result["advanced"] is True
        assert len(result["created_children"]) == 1

        # The new child covers the parent AC and the stage advanced.
        assert run_coverage_review(parent)["coverage"]["fully_covered"] is True
        wi = _show(store, parent)
        assert wi["status"] == "open"
        assert wi["stage"] == "plan_complete"
        comments = _comment_bodies(store, parent)
        assert any(
            "[tree-coverage-review]" in c and "auto-closed" in c
            for c in comments
        )

    def test_unresolvable_conflict_blocks_advance(self, eco_project):
        _, store = eco_project
        parent = _create(
            store, "Parent", ["System must encrypt all data at rest"]
        )
        _create(store, "Child", ["User can view profile"], parent=parent)

        assert run_coverage_review(parent)["recommendation"] == "stop"

        result = apply_coverage_review(parent, target_stage="plan_complete")

        assert result["action"] == "stop"
        assert result["advanced"] is False
        assert result["created_children"] == []

        wi = _show(store, parent)
        assert wi["status"] == "open"
        assert wi["stage"] != "plan_complete"
        comments = _comment_bodies(store, parent)
        assert any(
            "[tree-coverage-review]" in c and "stopped" in c for c in comments
        )
        # Only the original child exists — no coverage child was created.
        assert len(_children(store, parent)) == 1


# ---------------------------------------------------------------------------
# AC5 — idempotence
# ---------------------------------------------------------------------------


class TestCoverageReviewIdempotence:
    def test_stop_review_is_idempotent(self, eco_project):
        _, store = eco_project
        parent = _create(
            store, "Parent", ["System must encrypt all data at rest"]
        )
        _create(store, "Child", ["User can view profile"], parent=parent)

        first = apply_coverage_review(parent, target_stage="plan_complete")
        comments_after_first = _comment_bodies(store, parent)
        second = apply_coverage_review(parent, target_stage="plan_complete")
        comments_after_second = _comment_bodies(store, parent)

        assert first["action"] == second["action"] == "stop"
        assert comments_after_second == comments_after_first
        assert len(_children(store, parent)) == 1

    def test_auto_close_rerun_creates_no_duplicates(self, eco_project):
        _, store = eco_project
        parent = _create(
            store, "Parent", ["Support user authentication via OAuth login"]
        )
        _create(store, "Child", ["OAuth support"], parent=parent)

        first = apply_coverage_review(parent, target_stage="plan_complete")
        assert first["action"] == "auto_close"
        children_after_first = {c["id"] for c in _children(store, parent)}

        second = apply_coverage_review(parent, target_stage="plan_complete")

        assert second["action"] == "proceed"
        assert second["created_children"] == []
        assert {c["id"] for c in _children(store, parent)} == children_after_first
