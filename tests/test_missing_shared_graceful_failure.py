"""Regression tests: graceful failure + no-copy guardrail when shared is missing (F3, SA-0MSWJ9ZEU001HDVT).

Covers AC1+AC2 of the child: when a script cannot resolve its shared imports
(partial/copied installation where ``shared/`` is absent from the skills
root), it must exit non-zero with an actionable message that:

- names the missing module,
- gives the canonical invocation (global skills location / ``$(skill_path)``),
- states the no-cross-repo-copy rule, and
- never suggests or performs file copies between repositories.

The scenario mirrors the recurring failure (SA-0MSW6PG6Q002S4M6): a script
installed by real-copy (not symlink) into a project repo that lacks
``skill/shared/``. Here we build a temp copy of the whole skills root and
then DELETE ``shared/`` to simulate the partial install — then run scripts
with cwd in a fresh project and cleared PYTHONPATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILLS_SRC = _REPO_ROOT / "skill"

# Scripts whose top-level module imports hit `shared.*` (verified via the
# shared-import wrap in F3) — running `--help` triggers the imports.
_GUARDED_SCRIPTS: list[dict] = [
    {"skill": "intake", "name": "intake"},
    {"skill": "implement", "name": "implement"},
    {"skill": "effort-and-risk", "name": "run_skill"},
    {"skill": "audit", "name": "audit_runner"},
    {"skill": "refactor", "name": "refactor"},
]

# Canonical guard message fragments (must match skill/import_guard.py).
_MISSING_MODULE_MARKER = "'shared' could not be imported"
_CANONICAL_LOCATION_MARKERS = ("~/.pi/agent/skills", "skill_path")
_NO_COPY_RULE = "Do NOT copy skill scripts between repositories"


def _broken_install(tmp_path: Path) -> Path:
    """Copy the full skills root, then remove ``shared/`` (partial install)."""
    install = tmp_path / "skills_install"
    shutil.copytree(_SKILLS_SRC, install)
    shutil.rmtree(install / "shared")
    return install


def _run_script(install: Path, fresh_project: Path, skill_name: str, script_name: str) -> subprocess.CompletedProcess:
    script_path = install / skill_name / "scripts" / f"{script_name}.py"
    assert script_path.is_file(), f"missing {script_path}"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=str(fresh_project),
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
        check=False,
    )


def test_missing_shared_exits_nonzero_with_canonical_message(tmp_path) -> None:
    """Every guarded script fails gracefully and names the missing module."""
    install = _broken_install(tmp_path)
    fresh_project = tmp_path / "fresh-project"
    fresh_project.mkdir()
    tested = 0
    for entry in _GUARDED_SCRIPTS:
        proc = _run_script(install, fresh_project, entry["skill"], entry["name"])
        stderr = (proc.stderr + proc.stdout).strip()
        assert proc.returncode != 0, (
            f"{entry['skill']}/{entry['name']} should fail with missing shared; "
            f"stderr={stderr!r}"
        )
        assert _MISSING_MODULE_MARKER in stderr, (
            f"{entry['skill']}/{entry['name']} should name the missing module; "
            f"stderr={stderr!r}"
        )
        assert any(m in stderr for m in _CANONICAL_LOCATION_MARKERS), (
            f"{entry['skill']}/{entry['name']} should give the canonical location; "
            f"stderr={stderr!r}"
        )
        assert _NO_COPY_RULE in stderr, (
            f"{entry['skill']}/{entry['name']} should state the no-copy rule; "
            f"stderr={stderr!r}"
        )
        tested += 1
    assert tested == len(_GUARDED_SCRIPTS)


def test_guard_message_never_suggests_copies(tmp_path) -> None:
    """The guard message must not contain copy instructions between repos."""
    install = _broken_install(tmp_path)
    fresh_project = tmp_path / "fresh-project"
    fresh_project.mkdir()
    proc = _run_script(install, fresh_project, "intake", "intake")
    stderr = (proc.stderr + proc.stdout).strip()
    # Only the no-copy prohibition may mention "copy"; no shell copy commands,
    # no "copy from <repo>", no file-paste instructions.
    assert "cp " not in stderr
    assert "copy from" not in stderr
    assert stderr.count("copy") == stderr.count(_NO_COPY_RULE), (
        f"'copy' appears outside the prohibition sentence: {stderr!r}"
    )
    # And it must NOT suggest copying skill files anywhere.
    assert "cp " not in stderr and "rsync" not in stderr and "scp" not in stderr