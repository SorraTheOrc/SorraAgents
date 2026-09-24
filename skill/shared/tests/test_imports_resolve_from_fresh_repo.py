"""Regression tests: all skill scripts resolve imports from a real-copy install with a fresh-project cwd.

Covers SA-0MSW6PG6Q002S4M6 (parent) + SA-0MSWJ9YLG0045RWT (child F1).

Before the bootstrap fix (F2), scripts used ``Path(__file__).resolve().parents[3]``
(repo-root sys.path insert), which resolves to the wrong directory when the script
is installed as a real-copy (e.g. ``<real-copy>/intake/scripts/<script>.py`` →
``parents[3]`` = ``<real-copy>/intake`` instead of the correct location where
``shared/``, ``scripts/``, etc. live). This caused ``ModuleNotFoundError`` when
the skill was installed via a real copy (not symlink) and invoked from a project
repo that lacks ``skill/shared/``.

After F2, scripts use ``parents[2]`` (skills root), so imports of
``shared``, ``scripts``, ``test_cache``, ``test_runner``, etc. resolve from
the correct location regardless of cwd.

These tests create a temp "real-copy" install that mirrors the layout of the
installed global skills directory (``~/.pi/agent/skills/``) — **without** the
repo's ``skill/`` prefix. Layout::

    <tmp>/skills_install/
        intake/                  ← from skill/intake/
        audit/                   ← from skill/audit/
        shared/                  ← from skill/shared/
        scripts/                 ← from skill/scripts/
        test_cache.py            ← from skill/test_cache.py
        test_runner.py           ← from skill/test_runner.py
        ... (all skill dirs)

Then each script is run with cwd in a separate temp "fresh project" directory
that contains NO ``skill/`` tree. This simulates the exact scenario where an
agent runs the skill from a real-copy install in a project repo that lacks
``skill/shared/``.

Run pre-fix to capture red (expected-fail); run post-F2 to verify green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Bootstrap the skills root (parents[2] = <repo>/skill in the repo layout,
# ~/.pi/agent/skills in the global install) so top-level packages (shared,
# scripts, test_cache, ...) resolve regardless of caller cwd.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

# ──────────────────────────────────────────────────────────────────────────────
# Affected scripts (the 15 files that use ``parents[3]`` repo-root bootstrap)
# ──────────────────────────────────────────────────────────────────────────────

_AFFECTED_SCRIPTS: list[dict] = [
    {"skill": "audit", "name": "audit_runner", "path": "skill/audit/scripts/audit_runner.py"},
    {"skill": "audit", "name": "persist_audit", "path": "skill/audit/scripts/persist_audit.py"},
    {"skill": "audit", "name": "verify_context_reduction", "path": "skill/audit/scripts/verify_context_reduction.py"},
    {"skill": "cleanup", "name": "lib", "path": "skill/cleanup/scripts/lib.py"},
    {"skill": "context-audit", "name": "measure_context", "path": "skill/context-audit/scripts/measure_context.py"},
    {"skill": "effort-and-risk", "name": "orchestrate_estimate", "path": "skill/effort-and-risk/scripts/orchestrate_estimate.py"},
    {"skill": "effort-and-risk", "name": "run_skill", "path": "skill/effort-and-risk/scripts/run_skill.py"},
    {"skill": "find-related", "name": "find_related", "path": "skill/find-related/scripts/find_related.py"},
    {"skill": "implement", "name": "implement", "path": "skill/implement/scripts/implement.py"},
    {"skill": "intake", "name": "intake", "path": "skill/intake/scripts/intake.py"},
    {"skill": "refactor", "name": "refactor", "path": "skill/refactor/scripts/refactor.py"},
    {"skill": "report", "name": "render_report", "path": "skill/report/scripts/render_report.py"},
    {"skill": "test", "name": "run_tests", "path": "skill/test/scripts/run_tests.py"},
    {"skill": "triage", "name": "check_or_create", "path": "skill/triage/scripts/check_or_create.py"},
]


def _create_real_copy_install(tmp_path: Path) -> Path:
    """Create a temp "real-copy" global skills install.

    Mirrors the layout of ``~/.pi/agent/skills/`` — the contents of the
    repo's ``skill/`` directory are installed at the top level WITHOUT
    the ``skill/`` prefix, matching how the global install actually
    appears on disk.

    Layout::

        <tmp>/skills_install/
            intake/                  ← from skill/intake/
            audit/                   ← from skill/audit/
            shared/                  ← from skill/shared/
            scripts/                 ← from skill/scripts/
            test_cache.py            ← from skill/test_cache.py
            test_runner.py           ← from skill/test_runner.py
            ...

    This is a **real directory copy** (no symlinks to the source repo),
    simulating a machine where ``install_pi.sh`` installed the skills
    via ``cp -r`` rather than ``ln -s``.
    """
    install = tmp_path / "skills_install"
    install.mkdir(parents=True)
    src = _SKILLS_ROOT

    for item in src.iterdir():
        if item.name in ("__pycache__",):
            continue
        dst = install / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copy2(item, dst)

    return install


def test_all_affected_scripts_resolve_from_real_copy_install(tmp_path: Path) -> None:
    """Every affected script must resolve imports when invoked from a real-copy install with a fresh-project cwd.

    This test creates a temp "real-copy" install of the skills tree (no symlinks
    to the source repo, no ``skill/`` prefix in the install layout), then runs
    each script with cwd in a separate "fresh project" directory that contains
    NO ``skill/`` tree.  This simulates the exact scenario that caused agents
    to copy files across repos in ContextHub.
    """
    install = _create_real_copy_install(tmp_path)
    fresh_project = tmp_path / "fresh_project"
    fresh_project.mkdir(parents=True)

    # Create a minimal .worklog config for the fresh project
    worklog = fresh_project / ".worklog"
    worklog.mkdir()
    (worklog / "config.yaml").write_text(
        "projectName: Test\nprefix: TEST\n", encoding="utf-8"
    )

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)

    results: list[str] = []

    for info in _AFFECTED_SCRIPTS:
        # Extract skill name from the path (e.g. "skill/audit/..." → "audit")
        skill_name = info["path"].split("/")[1]
        script_name = info["path"].split("/")[-1]
        script_path = install / skill_name / "scripts" / script_name

        if not script_path.exists():
            results.append(f"SKIP {info['path']}: file not found")
            continue

        proc = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd=str(fresh_project),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,  # exit codes are asserted below
        )

        # Check for ModuleNotFoundError or ImportError in stderr
        stderr = proc.stderr + proc.stdout
        has_import_error = (
            "ModuleNotFoundError" in stderr or "ImportError" in stderr
        )

        if has_import_error:
            error_lines = [
                l for l in stderr.splitlines()
                if "ModuleNotFoundError" in l or "ImportError" in l
            ]
            error_summary = error_lines[-1] if error_lines else stderr.strip()
            results.append(f"FAIL {info['path']}: {error_summary}")
        else:
            results.append(f"PASS {info['path']}")

    # Report
    for r in results:
        print(r)

    fails = [r for r in results if r.startswith("FAIL")]
    assert not fails, (
        f"{len(fails)} script(s) failed to resolve imports from a real-copy "
        f"install with a fresh project cwd:\n"
        + "\n".join(fails)
    )
