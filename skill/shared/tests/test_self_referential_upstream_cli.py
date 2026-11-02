
# <!-- REFACTOR-SA-0MVYA3VEC000EB97
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->
"""CLI and classification-edge tests for self-referential upstream detection.

The authoritative detection contract — scratch-repo fixtures, the defect
fields and the healthy-remote / ``wl-*`` classification — is pinned by the
sibling test child SA-0MVX31GLC002IPR8 in ``test_self_referential_upstream.py``.
This file covers what that contract leaves open (SA-0MVX359B7008R9F3
deliverables):

* the **command-line entry point**: ``--json`` output shape (AC4 fields),
  ``--repo-root`` and the exit-code contract (deliverable: "Exit 0 when no
  defects found; non-zero when defects are present");
* the **classification edge AC1 names explicitly**: a *non*-``wl-*`` branch
  with a self-referential upstream is a **defect** — the ``remote = .``
  worktree-ownership signal is legitimate only for ``wl-*`` branches.

Both suites run the module as a subprocess (the shape the bash callers in
``scripts/hygiene_check.sh`` / ``implement.py start`` will use) and, for the
direct-function assertions, import it exactly like the contract file does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

#: Detection module implemented by this work item (SA-0MVX359B7008R9F3).
DETECTION_MODULE = "shared.self_referential_upstream"
DETECTION_SCRIPT = REPO_ROOT / "skill" / "shared" / "self_referential_upstream.py"

FIXTURE_VERSION = "self-ref-cli-fixture-v1"


def _detection():
    """Import the detection module (skips cleanly until it lands)."""
    return pytest.importorskip(DETECTION_MODULE)


def _defects(findings: list[dict]) -> list[dict]:
    """Only the ``type == "defect"`` findings (never ``expected``)."""
    return [f for f in findings if f.get("type") == "defect"]


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> Path:
    """A ``dev`` checkout whose ``dev`` and ``main`` track ``origin``."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    gs.run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    origin = gs.init_bare_remote(tmp_path / f"{name}-origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["branch", "main"], check=True)
    gs.run_git(repo, ["push", "-q", "-u", "origin", "main"], check=True)
    return repo


# ---------------------------------------------------------------------------
# CLI contract (deliverable: exit 0 no defects / non-zero defects, --json)
# ---------------------------------------------------------------------------


class TestCliContract:
    """The check is callable from bash with ``--json`` and exit-code semantics.

    The wire child (SA-0MVX35AUC003CNUD) consumes these shapes from
    ``hygiene_check.sh`` / ``implement.py start``; the Repair child
    (SA-0MVX359TI0069ECK) joins ``--fix`` onto the same entry point.
    """

    def _run_cli(self, repo: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(DETECTION_SCRIPT),
                "--repo-root",
                str(repo),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=gs.sanitized_git_env(),
        )

    def test_cli_reports_defect_and_exits_nonzero(self, tmp_path):
        """AC1/AC2: ``dev`` with ``remote = .`` → exit 1, JSON defect, all fields."""
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        proc = self._run_cli(repo, "--json")
        assert proc.returncode != 0, "a defect must produce a non-zero exit"
        payload = json.loads(proc.stdout)

        defects = _defects(payload["findings"])
        assert [f["branch"] for f in defects] == ["dev"]
        for key in ("branch", "type", "remote", "merge", "message"):
            assert key in defects[0], f"finding is missing the {key!r} field"
        assert defects[0]["remote"] == "."
        assert defects[0]["merge"] == "refs/heads/dev"
        assert defects[0]["message"]
        assert payload["ok"] is False
        assert payload["defect_count"] == 1

    def test_cli_healthy_repo_exits_zero(self, tmp_path):
        """AC4: a healthy ``origin``-tracked repo → exit 0, no findings."""
        repo = _repo_with_origin(tmp_path)

        proc = self._run_cli(repo, "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["defect_count"] == 0
        assert payload["findings"] == []

    def test_cli_worktree_branch_is_expected_and_exits_zero(self, tmp_path):
        """AC3/AC5: a ``wl-*`` branch with ``remote = .`` → expected, exit 0."""
        repo = _repo_with_origin(tmp_path)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-cli-expected",
                str(tmp_path / "wt"),
                "dev",
            ],
            check=True,
        )

        proc = self._run_cli(repo, "--json")
        assert proc.returncode == 0, "no defects → exit 0 (wl-* is expected)"
        payload = json.loads(proc.stdout)
        expected = [
            f for f in payload["findings"] if f["branch"] == "wl-SA-TEST-cli-expected"
        ]
        assert all(f["type"] == "expected" for f in expected)
        assert payload["defect_count"] == 0

    def test_cli_human_output_is_readable_and_exits_nonzero(self, tmp_path):
        """Default (non-JSON) output names the defect; exit stays non-zero."""
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        proc = self._run_cli(repo)
        assert proc.returncode != 0
        assert "dev" in proc.stdout
        assert "defect" in proc.stdout.lower()
        assert "--json" in proc.stdout, "help text mentions the --json mode"


# ---------------------------------------------------------------------------
# Classification edge: AC1's "(not wl-*)" — any non-worktree branch with a
# self-referential upstream is a defect
# ---------------------------------------------------------------------------


class TestNonWorktreeBranchIsDefect:
    """The ``remote = .`` ownership signal is defined only for ``wl-*``.

    A feature branch (or any branch that is not a worktree branch) with a
    self-referential upstream is the same machine-wide breakage the epic
    documents — ``git pull`` silently fetches from ``.``.
    """

    def test_feature_branch_with_dot_remote_is_a_defect(self, tmp_path):
        """AC1 edge: ``feature/x`` with ``remote = .`` → defect, not expected."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["branch", "feature/x"], check=True)
        gs.run_git(repo, ["config", "branch.feature/x.remote", "."], check=True)
        gs.run_git(
            repo, ["config", "branch.feature/x.merge", "refs/heads/feature/x"], check=True
        )

        findings = detection.detect_self_referential_upstreams(repo)
        defects = _defects(findings)
        assert [f["branch"] for f in defects] == ["feature/x"]
        assert defects[0]["remote"] == "."

    def test_branch_without_tracking_config_produces_no_finding(self, tmp_path):
        """An untracked branch (no ``remote``/``merge`` keys) is not reported."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["branch", "untracked-branch"], check=True)

        findings = detection.detect_self_referential_upstreams(repo)
        assert _defects(findings) == []
        assert not [
            f for f in findings if f.get("branch") == "untracked-branch"
        ], "a branch with no tracking config must not be reported"

    def test_findings_are_sorted_by_branch_name(self, tmp_path):
        """Deterministic ordering: findings come back sorted by branch name."""
        detection = _detection()
        repo = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["branch", "zeta"], check=True)
        gs.run_git(repo, ["config", "branch.zeta.remote", "."], check=True)
        gs.run_git(repo, ["config", "branch.zeta.merge", "refs/heads/zeta"], check=True)
        gs.run_git(repo, ["push", "-q", "-u", ".", "dev:dev"], check=True)

        findings = detection.detect_self_referential_upstreams(repo)
        assert [f["branch"] for f in findings] == sorted(
            f["branch"] for f in findings
        )
        assert [f["branch"] for f in _defects(findings)] == ["dev", "zeta"]
