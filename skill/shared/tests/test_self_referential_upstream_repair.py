
# <!-- REFACTOR-SA-0MVYABWFT005B601
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->
"""Repair tests for self-referential upstream detection (SA-0MVX359TI0069ECK).

The detection module (``shared.self_referential_upstream``, SA-0MVX359B7008R9F3)
reports branches whose ``branch.<name>.remote`` is ``"."`` — the defect that
makes ``git pull`` a silent no-op on worktree machines (SA-0MVX1M6HH004YPX2).
This file pins the safe ``--fix`` repair contract:

* repairs a defect to ``remote=origin`` / ``merge=refs/heads/<branch>`` **only**
  when ``origin`` is a real configured remote whose ``refs/heads/<branch>``
  exists;
* refuses with an actionable message otherwise (never guesses a remote, never
  rewrites to a non-existent branch);
* never touches ``wl-*`` worktree branches (where ``remote = "."`` is the
  legitimate ownership signal);
* never checks out or modifies working trees — only ``branch.*`` config keys;
* dry-run mode previews the repair without writing anything;
* machine-readable before/after via ``--json``.

Every scenario is a throwaway network-free scratch repo under ``tmp_path``
with a real bare ``origin`` remote (local path), so ``git ls-remote`` against
it is deterministic and offline.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (str(REPO_ROOT), str(REPO_ROOT / "skill")):
    if _path not in sys.path:
        sys.path.append(_path)

from shared import git_sandbox as gs

DETECTION_SCRIPT = REPO_ROOT / "skill" / "shared" / "self_referential_upstream.py"


def _tracking(repo: Path, branch: str) -> tuple[str | None, str | None]:
    """Read ``(remote, merge)`` for *branch* from the repository's config."""
    remote = gs.run_git(repo, ["config", "--get", f"branch.{branch}.remote"]).stdout
    merge = gs.run_git(repo, ["config", "--get", f"branch.{branch}.merge"]).stdout
    return (remote.strip() or None, merge.strip() or None)


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """A ``dev`` checkout whose ``dev`` and ``main`` track a bare local origin.

    Returns ``(repo, origin)`` — the working checkout and its bare ``origin``.
    """
    repo = gs.init_repo(tmp_path / name, default_branch="dev")
    gs.run_git(repo, ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
    origin = gs.init_bare_remote(tmp_path / f"{name}-origin.git")
    gs.add_remote(repo, "origin", str(origin))
    gs.run_git(repo, ["push", "-q", "-u", "origin", "dev"], check=True)
    gs.run_git(repo, ["branch", "main"], check=True)
    gs.run_git(repo, ["push", "-q", "-u", "origin", "main"], check=True)
    return repo, origin


def _make_defect(repo: Path, branch: str = "dev") -> None:
    """Record the self-referential upstream for *branch* (the push -u . shape)."""
    gs.run_git(repo, ["push", "-q", "-u", ".", f"{branch}:{branch}"], check=True)


def _run_cli(repo: Path, *extra: str) -> subprocess.CompletedProcess:
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


# ---------------------------------------------------------------------------
# AC1 — repair restores the healthy origin upstream
# ---------------------------------------------------------------------------


class TestRepairRestoresOriginUpstream:
    def test_repair_dev_to_origin(self, tmp_path):
        """AC1: ``remote = .`` on dev → repaired to ``origin``, exit 0."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")
        assert _tracking(repo, "dev") == (".", "refs/heads/dev")

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")

    def test_repair_reports_machine_readable_before_after(self, tmp_path):
        """AC5: the --json payload carries before/after remote+merge per branch."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")

        proc = _run_cli(repo, "--fix", "--json")
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert payload["defect_count"] == 0  # repaired → nothing left

        [dev] = [r for r in payload["repairs"] if r["branch"] == "dev"]
        assert dev["remote_before"] == "."
        assert dev["remote_after"] == "origin"
        assert dev["merge_before"] == "refs/heads/dev"
        assert dev["merge_after"] == "refs/heads/dev"
        assert dev["outcome"] == "repaired"

    def test_repair_sets_merge_even_when_unset(self, tmp_path):
        """A defect without a merge key still gets the full healthy pair."""
        repo, _origin = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["config", "branch.dev.remote", "."], check=True)

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")


# ---------------------------------------------------------------------------
# AC2 — refusal (actionable, config untouched)
# ---------------------------------------------------------------------------


class TestRepairRefusal:
    def test_refuses_when_origin_missing(self, tmp_path):
        """AC2: no ``origin`` remote → refuse, config unchanged, exit 2."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")
        gs.run_git(repo, ["remote", "remove", "origin"], check=True)

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        assert "origin" in payload["message"].lower()
        # config untouched (still self-referential, and no crash mid-repair)
        assert _tracking(repo, "dev") == (".", "refs/heads/dev")

    def test_refuses_when_branch_missing_on_origin(self, tmp_path):
        """AC2: origin lacks refs/heads/<branch> → refuse with the branch named."""
        repo, origin = _repo_with_origin(tmp_path)
        gs.run_git(repo, ["branch", "local-only"], check=True)
        _make_defect(repo, "local-only")
        assert _tracking(repo, "local-only") == (".", "refs/heads/local-only")
        # sanity: origin really has no such branch
        ls = gs.run_git(repo, ["ls-remote", str(origin), "refs/heads/local-only"]).stdout
        assert ls.strip() == ""

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout)
        [entry] = [
            r for r in payload["repairs"] if r["branch"] == "local-only"
        ]
        assert entry["outcome"] == "refused"
        assert "local-only" in entry["message"]
        assert "origin" in entry["message"]
        # dev (origin has it) was still repaired; local-only left untouched
        assert _tracking(repo, "local-only") == (".", "refs/heads/local-only")
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")


# ---------------------------------------------------------------------------
# AC3/AC4 — wl-* never touched; config-only, never a working-tree change
# ---------------------------------------------------------------------------


class TestRepairSafety:
    def test_never_touches_wl_branch(self, tmp_path):
        """AC3: a ``wl-*`` branch with ``remote = .`` stays untouched."""
        repo, _origin = _repo_with_origin(tmp_path)
        gs.run_git(
            repo,
            [
                "worktree",
                "add",
                "--track",
                "-b",
                "wl-SA-TEST-repair",
                str(tmp_path / "wt"),
                "dev",
            ],
            check=True,
        )
        _make_defect(repo, "dev")

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # the ownership signal survives on the worktree branch
        assert _tracking(repo, "wl-SA-TEST-repair") == (".", "refs/heads/dev")
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")

    def test_repair_changes_config_only(self, tmp_path):
        """AC4: no checkout, no working-tree mutation — only branch.* config."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")
        (repo / "tracked.txt").write_text("file", encoding="utf-8")
        gs.run_git(repo, ["add", "tracked.txt"], check=True)
        gs.run_git(repo, ["commit", "-q", "-m", "tracked"], check=True)
        before_head = gs.run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        before_status = gs.run_git(repo, ["status", "--porcelain"]).stdout.strip()

        proc = _run_cli(repo, "--fix", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr

        assert gs.run_git(repo, ["rev-parse", "HEAD"]).stdout.strip() == before_head
        assert gs.run_git(repo, ["status", "--porcelain"]).stdout.strip() == before_status
        assert _tracking(repo, "dev") == ("origin", "refs/heads/dev")


# ---------------------------------------------------------------------------
# Dry-run — preview only, never writes
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_reports_without_changing_config(self, tmp_path):
        """--dry-run previews the repair and leaves the config byte-for-byte."""
        repo, _origin = _repo_with_origin(tmp_path)
        _make_defect(repo, "dev")

        proc = _run_cli(repo, "--fix", "--dry-run", "--json")
        assert proc.returncode == 1, (
            "dry-run must not claim success: the defect is still present"
        )
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False
        [dev] = [r for r in payload["repairs"] if r["branch"] == "dev"]
        assert dev["outcome"] == "would-repair"
        assert dev["remote_after"] == "origin"
        assert _tracking(repo, "dev") == (".", "refs/heads/dev"), (
            "dry-run must not write branch config"
        )

    def test_dry_run_with_no_defects_exits_zero(self, tmp_path):
        """A healthy repo under --dry-run has nothing to repair → exit 0."""
        repo, _origin = _repo_with_origin(tmp_path)

        proc = _run_cli(repo, "--fix", "--dry-run", "--json")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["repairs"] == []
