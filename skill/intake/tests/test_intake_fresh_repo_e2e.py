"""E2E regression (F5, SA-0MSWJA08G003BVUI): full intake lifecycle in a fresh repo.

Terminal verification for the parent epic (SA-0MSW6PG6Q002S4M6, AC6):

- A fresh project (temp dir with a minimal ``.worklog`` store, NO ``skill/``
  tree) runs the full intake lifecycle via the **installed global script
  path** (``~/.pi/agent/skills/intake/scripts/intake.py`` — the canonical
  install; falls back to the repo path only if the global install is absent).
- ``start`` succeeds (status ``in_progress``), ``auto-complete`` succeeds
  (``open`` / ``intake_complete``), and after the run the temp project
  contains **zero** ``skill/`` files — proving the scripts resolve their
  shared libraries from the skills root without copying anything into the
  project repo.

Depends on F2 (self-contained bootstrap) + F3 (graceful failure): if the
scripts could not resolve imports from a real-copy/fresh-project cwd, the
runs below would exit non-zero (red); they must exit 0 (green).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILLS_ROOT = _REPO_ROOT / "skill"

# Canonical installed script path (production layout: install_pi.sh symlinks
# ~/.pi/agent/skills -> <repo>/skill). Fall back to the repo path only when
# the global install is absent (e.g. CI without install_pi.sh).
_GLOBAL_INTAKE = Path.home() / ".pi" / "agent" / "skills" / "intake" / "scripts" / "intake.py"
_INTAKE_SCRIPT = _GLOBAL_INTAKE if _GLOBAL_INTAKE.is_file() else (
    _SKILLS_ROOT / "intake" / "scripts" / "intake.py"
)

_AGENT = "e2e-probe-agent"


def _init_store(store: Path) -> None:
    """Non-interactively initialize a minimal .worklog store."""
    proc = subprocess.run(
        [
            "wl", "init",
            "--worklog-dir", str(store),
            "--project-name", "E2E-FRESH-REPO",
            "--prefix", "E2E",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--json",
        ],
        input="\n",  # satisfy any residual interactive prompt
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"wl init failed: {proc.stdout} {proc.stderr}"
    assert store.is_dir(), f"store not created: {store}"


def _create_item(store: Path, title: str) -> str:
    """Create a work item in *store* and return its id."""
    proc = subprocess.run(
        [
            "wl", "create",
            "--worklog-dir", str(store),
            "--title", title,
            "--description", "Auto-created by the fresh-repo E2E regression.",
            "--issue-type", "task",
            "--priority", "low",
            "--status", "open",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"wl create failed: {proc.stdout} {proc.stderr}"
    data = json.loads(proc.stdout)
    item = data.get("workItem", data)
    return item["id"]


def _show_status(store: Path, item_id: str) -> tuple[str, str]:
    """Return (status, stage) of *item_id* in *store*."""
    proc = subprocess.run(
        ["wl", "show", "--worklog-dir", str(store), item_id, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"wl show failed: {proc.stdout} {proc.stderr}"
    data = json.loads(proc.stdout)
    item = data.get("workItem", data)
    return item["status"], item.get("stage", "")


def _run_intake(project: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the canonical intake script with cwd in the fresh project."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(_INTAKE_SCRIPT), *args],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def test_full_intake_lifecycle_in_fresh_repo(tmp_path) -> None:
    """start + auto-complete succeed from a fresh project and copy nothing."""
    project = tmp_path / "fresh-project"
    project.mkdir()
    store = project / ".worklog"
    _init_store(store)

    item_id = _create_item(store, "E2E fresh-repo intake regression")

    # 1. Full intake lifecycle via the installed global script path.
    start = _run_intake(project, "start", item_id, "--assignee", _AGENT)
    assert start.returncode == 0, (
        f"intake start failed:\nstdout={start.stdout}\nstderr={start.stderr}"
    )
    result = json.loads(start.stdout)
    assert result.get("success") is True, f"start not successful: {result}"
    assert result.get("action") == "started"
    status, _ = _show_status(store, item_id)
    # wl's CLI normalizes the status spelling to `in-progress` in `show` output.
    assert status.replace("-", "_") == "in_progress", f"expected in_progress, got {status!r}"

    complete = _run_intake(project, "auto-complete", item_id)
    assert complete.returncode == 0, (
        f"intake auto-complete failed:\nstdout={complete.stdout}\nstderr={complete.stderr}"
    )
    result = json.loads(complete.stdout)
    assert result.get("success") is True, f"auto-complete not successful: {result}"
    status, stage = _show_status(store, item_id)
    assert status == "open", f"expected open, got {status!r}"
    assert stage == "intake_complete", f"expected intake_complete, got {stage!r}"


def test_no_skill_dir_copied_into_fresh_repo(tmp_path) -> None:
    """Post-run, the fresh project contains no skill/ files (AC6 no-copy)."""
    project = tmp_path / "fresh-project"
    project.mkdir()
    store = project / ".worklog"
    _init_store(store)
    item_id = _create_item(store, "E2E no-copy regression")

    assert _run_intake(project, "start", item_id, "--assignee", _AGENT).returncode == 0
    assert _run_intake(project, "auto-complete", item_id).returncode == 0

    skill_leaks = [p for p in project.rglob("skill*") if p.name.lower().startswith("skill")]
    assert not skill_leaks, f"skill files/dirs copied into the fresh repo: {skill_leaks}"
    assert not (project / "skill").exists()
    # The store itself may contain a skills folder name? Verify no 'skill' path
    # component anywhere under the project excluding the store's own data files.
    for p in project.rglob("*"):
        if p.is_dir() and p.name == "skill":
            raise AssertionError(f"skill directory created in fresh repo: {p}")


def test_intake_script_path_is_canonical(tmp_path) -> None:
    """The test drives the installed global script path when available."""
    if _GLOBAL_INTAKE.is_file():
        assert str(_INTAKE_SCRIPT) == str(_GLOBAL_INTAKE), (
            "canonical global path must be the E2E invocation path"
        )
        assert _INTAKE_SCRIPT.is_file()
    else:
        # No global install (CI without install_pi.sh): the repo copy is
        # functionally identical (install_pi.sh symlinks to it).
        assert _INTAKE_SCRIPT.is_file()