"""Scratch-repo fixtures for self-referential upstream detection (SA-0MVX1M6HH004YPX2 AC7).

A long-lived branch (`dev`/`main`) whose git tracking config resolves to the
**same repository** makes a plain ``git pull`` a silent no-op: it fetches from
``.``, prints ``From .`` / ``Already up to date``, exits 0, and no remote data
arrives. ``branch.<name>.remote`` is one key per **branch name** stored in the
shared ``.git/config``, so the defect is machine-wide.

This module is the deterministic, network-free test harness. Every scenario
builds its own throwaway repository under ``tmp_path`` and exercises real git
plumbing — no mocks, no ambient config.

Contract under test
-------------------
``shared.self_referential_upstream.detect_self_referential_upstreams(repo)``
returns a list of findings. Each finding is a mapping with at least::

    {"branch": str, "type": "defect" | "expected",
     "remote": str, "merge": str, "message": str}

The detection module itself lands in the sibling work item
**SA-0MVX359B7008R9F3** (child 2 of the epic); until it exists the detection
scenarios ``pytest.importorskip`` cleanly — the same tracer convention as
``test_code_freeze.py`` and ``test_work_item_lock.py``. The worktree-tracking
scenario (AC1) needs no detection module and runs immediately.

Classification rules pinned here
--------------------------------
* ``dev``, ``main`` and any branch named in the repository's release config are
  **long-lived**: a self-referential upstream is a **defect**.
* ``wl-*`` branches are **worktree branches**: ``remote = .`` is the legitimate
  worktree-ownership signal and is **expected** — never a defect.

Reconciling "self-referential merge" with the healthy shape
----------------------------------------------------------
A healthy ``origin``-tracked ``dev`` *always* has
``branch.dev.merge = refs/heads/dev`` (it merges ``origin/dev`` into ``dev``),
so "``merge`` names the same branch" alone is **not** a defect — flagging it
would contradict AC4 (a healthy ``origin``-tracked branch produces no
findings). The real defect is a **self-referential upstream**: ``remote ==
"."`` (the empirically observed, machine-wide breakage) so that the ``merge``
ref resolves *inside the same repository* instead of at ``origin``. AC2 is
therefore exercised via the ``git push -u . dev:dev`` shape, which sets
``remote = .`` *and* ``merge = refs/heads/dev`` — the merge ref resolves to
itself because the remote is the repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

#: Detection module implemented by the sibling child (SA-0MVX359B7008R9F3).
DETECTION_MODULE = "shared.self_referential_upstream"


def _detection():
    """Import the detection module, skipping cleanly until sibling child lands.

    Called from inside detection tests (not at module scope) so the
    worktree-tracking scenario still runs while the module is absent.
    """
    return pytest.importorskip(DETECTION_MODULE)


def _defects(findings: list[dict]) -> list[dict]:
    """Only the ``type == "defect"`` findings (never ``expected``)."""
    return [f for f in findings if f.get("type") == "defect"]


def _tracking(repo: Path, branch: str) -> tuple[str | None, str | None]:
    """Read ``(remote, merge)`` for *branch* from the repository's config."""
    remote = gs.run_git(repo, ["config", "--get", f"branch.{branch}.remote"]).stdout
    merge = gs.run_git(repo, ["config", "--get", f"branch.{branch}.merge"]).stdout
    return (remote.strip() or None, merge.strip() or None)


# ---------------------------------------------------------------------------
# Scratch-repo fixtures (network-free, hermetic)
# ---------------------------------------------------------------------------


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> Path:
    """A ``dev`` checkout whose ``dev`` and ``main`` track ``origin``.

    Creates a bare local ``origin`` (a real remote name, never a path form)
    and pushes both long-lived branches with ``-u``, so the healthy shape is
    ``remote = origin`` / ``merge = refs/heads/<branch>``.
    """
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    gs.run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    origin = gs.init_bare_remote(tmp_path / f"{name}-origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["branch", "main"], check=True)
    gs.run_git(repo, ["push", "-q", "-u", "origin", "main"], check=True)
    return repo


# ---------------------------------------------------------------------------
# AC1 — worktree registration keeps --track for wl-* but never corrupts the
#        parent branch
# ---------------------------------------------------------------------------


class TestWorktreeTrackingDoesNotCorruptParent:
    """``git worktree add --track -b wl-* <path> dev`` writes ``.`` for the
    ``wl-*`` branch only; the parent branch's tracking config is untouched.

    This is the write path ``implement.py`` uses (``--track`` produces the
    worktree-ownership signal ``remote = .`` that sandbox cleanup relies on).
    The regression it guards: if a worktree registration ever wrote ``.``
    onto a **long-lived** branch name, a plain ``git pull`` would silently
    stop fetching from ``origin``.
    """

    def _add_tracked_worktree(self, repo: Path, tmp_path: Path) -> None:
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-tracked",
                str(tmp_path / "wt"),
                "dev",
            ],
            check=True,
        )

    def test_tracked_wl_branch_gets_dot_remote(self, tmp_path):
        """The new ``wl-*`` branch records ``remote = .`` (the ownership signal)."""
        repo = _repo_with_origin(tmp_path)
        self._add_tracked_worktree(repo, tmp_path)

        remote, merge = _tracking(repo, "wl-SA-TEST-tracked")
        assert remote == ".", "the worktree-ownership signal must be preserved"
        assert merge == "refs/heads/dev"

    def test_parent_dev_tracking_is_untouched(self, tmp_path):
        """``dev`` keeps ``remote = origin`` after the worktree is registered."""
        repo = _repo_with_origin(tmp_path)
        self._add_tracked_worktree(repo, tmp_path)

        remote, merge = _tracking(repo, "dev")
        assert remote == "origin", "worktree registration corrupted dev's upstream"
        assert merge == "refs/heads/dev"

    def test_parent_main_tracking_is_untouched(self, tmp_path):
        """``main`` keeps ``remote = origin`` after the worktree is registered."""
        repo = _repo_with_origin(tmp_path)
        self._add_tracked_worktree(repo, tmp_path)

        remote, merge = _tracking(repo, "main")
        assert remote == "origin", "worktree registration corrupted main's upstream"
        assert merge == "refs/heads/main"

    def test_worktree_registration_is_not_a_self_referential_defect(self, tmp_path):
        """Registering a ``--track`` worktree yields no long-lived-branch defect."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        self._add_tracked_worktree(repo, tmp_path)

        findings = detection.detect_self_referential_upstreams(repo)
        assert _defects(findings) == [], (
            "worktree registration must never flag a long-lived branch"
        )


# ---------------------------------------------------------------------------
# AC2/AC3 — defect detection (remote = ".", self-referential merge)
# ---------------------------------------------------------------------------


class TestDefectDetection:
    """Self-referential upstreams on long-lived branches are defects.

    AC2's "self-referential merge" is exercised through the
    ``git push -u . dev:dev`` shape: the command records ``remote = .`` *and*
    ``merge = refs/heads/dev``, so the merge ref resolves inside the same
    repository. A healthy ``remote = origin`` branch legitimately carries
    ``merge = refs/heads/<branch>`` and is covered by AC4 instead.
    """

    def test_push_to_local_path_records_self_upstream(self, tmp_path):
        """Fixture sanity: ``git push -u . dev:dev`` really writes ``remote = .``."""
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        remote, merge = _tracking(repo, "dev")
        assert remote == ".", "git did not record the self-referential upstream"
        assert merge == "refs/heads/dev"

    def test_self_referential_remote_is_a_defect_on_dev(self, tmp_path):
        """AC2: ``dev`` with ``remote = .`` is reported as a defect."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        findings = detection.detect_self_referential_upstreams(repo)
        defects = _defects(findings)
        assert [f["branch"] for f in defects] == ["dev"]
        assert defects[0]["remote"] == "."
        assert defects[0]["merge"] == "refs/heads/dev"

    def test_self_referential_remote_is_a_defect_on_main(self, tmp_path):
        """AC1/AC2: ``main`` with ``remote = .`` is reported as a defect."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["checkout", "-q", "main"], check=True)
        gs.run_git(repo, ["push", "-q", "-u", ".", "main:main"], check=True)

        findings = detection.detect_self_referential_upstreams(repo)
        defects = _defects(findings)
        assert [f["branch"] for f in defects] == ["main"]
        assert defects[0]["remote"] == "."

    def test_defect_finding_is_machine_readable(self, tmp_path):
        """AC4 (child 2): each finding carries the documented fields."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        findings = detection.detect_self_referential_upstreams(repo)
        [defect] = _defects(findings)
        for key in ("branch", "type", "remote", "merge", "message"):
            assert key in defect, f"finding is missing the {key!r} field"
        assert defect["type"] == "defect"
        assert defect["branch"] == "dev"
        assert defect["message"], "a defect must carry an actionable message"


# ---------------------------------------------------------------------------
# AC4 — a healthy origin-tracked branch produces no findings
# ---------------------------------------------------------------------------


class TestHealthyBranchNotFlagged:
    """A branch tracking a real ``origin`` remotely is never a defect.

    The canonical healthy shape is ``remote = origin`` /
    ``merge = refs/heads/<branch>`` — the ``merge`` ref naming the same branch
    is correct because it resolves at ``origin``, not locally.
    """

    def test_healthy_dev_and_main_produce_no_defects(self, tmp_path):
        """AC4: ``dev`` and ``main`` tracked at ``origin`` → no findings."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)

        # Fixture sanity: both long-lived branches are the healthy shape.
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")
        assert _tracking(repo, "main") == ("origin", "refs/heads/main")

        findings = detection.detect_self_referential_upstreams(repo)
        assert _defects(findings) == []


# ---------------------------------------------------------------------------
# AC5 — a wl-* branch with "." is expected, never a defect
# ---------------------------------------------------------------------------


class TestWorktreeBranchIsExpected:
    """``wl-*`` branches legitimately carry ``remote = .`` (worktree ownership)."""

    def test_wl_branch_with_dot_remote_produces_no_defect(self, tmp_path):
        """AC5: a ``wl-*`` branch with ``remote = .`` is not a defect."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-expected",
                str(tmp_path / "wt"),
                "dev",
            ],
            check=True,
        )

        # Fixture sanity: the wl-* branch really is self-referential.
        assert _tracking(repo, "wl-SA-TEST-expected")[0] == "."

        findings = detection.detect_self_referential_upstreams(repo)
        assert _defects(findings) == [], "a wl-* branch must never be a defect"
        assert not [
            f
            for f in findings
            if f.get("branch") == "wl-SA-TEST-expected" and f.get("type") == "defect"
        ]

    def test_wl_branch_is_classified_expected_when_reported(self, tmp_path):
        """AC3 (child 2): if the finding is reported, its type is ``expected``."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-classified",
                str(tmp_path / "wt2"),
                "dev",
            ],
            check=True,
        )

        findings = detection.detect_self_referential_upstreams(repo)
        reported = [f for f in findings if f.get("branch") == "wl-SA-TEST-classified"]
        # The detection contract reports worktree branches as ``expected``
        # (callers ignore them); omitted entries are equally acceptable.
        assert all(f.get("type") == "expected" for f in reported)
