"""Wiring tests: self-referential-upstream detection runs where it matters
(SA-0MVX35AUC003CNUD — child 5 of SA-0MVX1M6HH004YPX2).

`branch.<name>.remote = "."` on a long-lived branch makes a plain `git pull`
a silent no-op (stale skills, stale worklog data). Detection must therefore
run **automatically** in two places:

* ``scripts/hygiene_check.sh`` — the periodic/CI hygiene report, reporting
  defects as warnings (exit 1);
* ``implement.py start`` — a warning at the start of every implement run.

Both are **fail-open by design** (parent AC4): they never block a work item,
never rewrite config, and degrade to a skip when the detection module or the
repository is unavailable. Both emit ``--json`` for programmatic consumption.

The hygiene script resolves the detection module from its **own** framework
tree, so ``--repo-root`` can point at any project (every fixture here is a
throwaway scratch repository).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

from skill.implement.scripts import implement

_SHARED_ROOT = REPO_ROOT / "skill"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.append(str(_SHARED_ROOT))

from shared import git_sandbox as gs

HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "hygiene_check.sh"
CHECK = "self_referential_upstream"


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """A ``dev`` checkout whose ``dev``/``main`` track a bare local origin."""
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    gs.run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    origin = gs.init_bare_remote(tmp_path / f"{name}-origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["branch", "main"], check=True)
    gs.run_git(repo, ["push", "-q", "-u", "origin", "main"], check=True)
    return repo, origin


def _make_defect(repo: Path, branch: str = "dev") -> None:
    """Record the self-referential upstream (the ``push -u .`` shape)."""
    gs.run_git(repo, ["push", "-q", "-u", ".", f"{branch}:{branch}"], check=True)


def _tracking_remote(repo: Path, branch: str) -> str | None:
    out = gs.run_git(repo, ["config", "--get", f"branch.{branch}.remote"]).stdout
    return out.strip() or None


def _run_hygiene(
    repo: Path, *args: str, script: Path = HYGIENE_SCRIPT
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), "--repo-root", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _hygiene_lines(proc: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# AC1 — hygiene_check.sh reports defects as warnings
# ---------------------------------------------------------------------------


class TestHygieneIntegration:
    def test_defect_is_reported_as_warning_json(self, tmp_path):
        """AC1: a defective dev → warning line naming the branch, exit 1."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")

        proc = _run_hygiene(repo, "--json")
        lines = _hygiene_lines(proc)
        check = next(l for l in lines if l.get("check") == CHECK)
        assert check["status"] == "warning"
        assert check["defect_count"] == 1
        assert [d["branch"] for d in check["defects"]] == ["dev"]
        assert proc.returncode == 1

    def test_healthy_repo_is_ok_json(self, tmp_path):
        """AC1/AC4: a healthy repo reports ok (and does not fail hygiene)."""
        repo, _origin = _repo_with_origin(tmp_path)

        proc = _run_hygiene(repo, "--json")
        lines = _hygiene_lines(proc)
        check = next(l for l in lines if l.get("check") == CHECK)
        assert check["status"] == "ok"
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_wl_branch_with_dot_remote_is_not_a_defect(self, tmp_path):
        """AC1: the legitimate wl-* ownership signal never warns."""
        repo, _origin = _repo_with_origin(tmp_path)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-wire",
                str(tmp_path / "wt"),
                "dev",
            ],
            check=True,
        )

        proc = _run_hygiene(repo, "--json")
        check = next(l for l in _hygiene_lines(proc) if l.get("check") == CHECK)
        assert check["status"] == "ok"
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_human_output_names_the_defect_and_the_repair(self, tmp_path):
        """Human output explains the breakage and the explicit repair path."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")

        proc = _run_hygiene(repo)
        assert "dev" in proc.stdout
        assert "silent no-op" in proc.stdout
        assert "--fix" in proc.stdout, "the repair must be surfaced"
        assert proc.returncode == 1

    def test_missing_detection_module_is_a_fail_open_skip(self, tmp_path):
        """AC3: no detection module → skipped, never an error."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")  # a defect that CANNOT be detected here
        scripts_dir = tmp_path / "framework" / "scripts"
        scripts_dir.mkdir(parents=True)
        lonely_script = scripts_dir / "hygiene_check.sh"
        shutil.copy(HYGIENE_SCRIPT, lonely_script)

        proc = _run_hygiene(repo, "--json", script=lonely_script)
        check = next(l for l in _hygiene_lines(proc) if l.get("check") == CHECK)
        assert check["status"] == "skipped"
        assert "detection module not present" in check["reason"]
        assert proc.returncode == 0, "a missing checker must never fail hygiene"


# ---------------------------------------------------------------------------
# AC2 — implement.py start surfaces the same warning, fail-open
# ---------------------------------------------------------------------------


class TestImplementStartWarning:
    def test_defect_produces_a_warning_without_rewriting_config(self, tmp_path):
        """AC2/AC3: warning verdict, defect named, config untouched."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")

        verdict = implement.warn_self_referential_upstream(str(repo))

        assert verdict["status"] == "warning"
        assert verdict["defect_count"] == 1
        assert [d["branch"] for d in verdict["defects"]] == ["dev"]
        # never rewrites config implicitly — repair stays explicit
        assert _tracking_remote(repo, "dev") == "."

    def test_healthy_repo_is_ok(self, tmp_path):
        """AC2: a healthy repo produces an ok verdict (no warning)."""
        repo, _origin = _repo_with_origin(tmp_path)
        verdict = implement.warn_self_referential_upstream(str(repo))
        assert verdict["status"] == "ok"

    def test_unavailable_detection_module_skips_fail_open(self, tmp_path, monkeypatch):
        """AC3: a partial install (module absent) must never block a start."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")
        monkeypatch.setattr(implement, "_detect_self_referential_upstreams", None)

        verdict = implement.warn_self_referential_upstream(str(repo))
        assert verdict["status"] == "skipped"
        assert "not available" in verdict["reason"]

    def test_detection_error_skips_fail_open(self, tmp_path, monkeypatch):
        """AC3: a git/repo failure inside detection is a skip, not a failure."""

        def _boom(_repo):  # pragma: no cover - exercised via the wrapper
            raise RuntimeError("not a git repository")

        repo, _origin = _repo_with_origin(tmp_path)
        monkeypatch.setattr(implement, "_detect_self_referential_upstreams", _boom)

        verdict = implement.warn_self_referential_upstream(str(repo))
        assert verdict["status"] == "skipped"
        assert "not a git repository" in verdict["reason"]
