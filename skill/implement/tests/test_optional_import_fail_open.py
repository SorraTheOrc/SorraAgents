"""The optional-import guard must survive any import-time failure (SA-0MW3AKSIT005J5YZ).

``implement.py`` imports the optional self-referential-upstream detector
*outside* its hard-fail shared block, so a partial skill install degrades to a
logged skip instead of blocking ``start`` (SA-0MVX35AUC003CNUD AC2/AC3). The
guard therefore has to catch ``Exception``, not just ``ImportError``: a module
that is present but broken can fail at import time with something else
entirely (a missing runtime dependency, an ``AttributeError`` from module-level
code, a ``RuntimeError`` raised by a guard clause, ...), and that failure must
also degrade to a skip rather than propagate out of ``implement.py``.

These tests pin both halves of that contract:

* importing ``implement.py`` with a detector module that raises a
  **non-``ImportError``** at import time succeeds and leaves
  ``_detect_self_referential_upstreams`` as ``None`` (nothing propagates);
* the resulting ``warn_self_referential_upstream`` verdict is ``skipped``, so
  an implement run is never blocked;
* as a control, the same probe against the real detector reports the symbol as
  available — proving the ``None`` above comes from the break, not the probe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / "skill"

# A detector module that fails at import time with a NON-ImportError: the
# guard must treat it exactly like a missing module (fail-open skip).
BROKEN_DETECTOR_SOURCE = '''"""Simulated partial install: raises at import time."""

raise RuntimeError("simulated import-time failure")
'''

# Runs in a subprocess so the broken module is imported by a *fresh* process
# (the real detector is already loaded in the test process' sys.modules).
PROBE_SOURCE = """
import json
import sys

skill_root, repo_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, skill_root)

from implement.scripts import implement

print(
    json.dumps(
        {
            "symbol_available": implement._detect_self_referential_upstreams
            is not None,
            "verdict": implement.warn_self_referential_upstream(repo_root),
        }
    )
)
"""


def _build_framework_tree(
    tmp_path: Path, detector_source: str | None = None
) -> Path:
    """A symlink farm copy of ``skill/`` with the detector module overridden.

    ``implement.py`` resolves its skills root from ``__file__``, so the copy
    must sit in the same layout (``<root>/skill/implement/scripts``). Every
    shared module is symlinked to the checkout's real one — except
    ``self_referential_upstream.py`` when ``detector_source`` is given.

    Args:
        tmp_path: Per-test scratch directory.
        detector_source: Source for the doctored detector, or ``None`` to
            symlink the real one (the control case).

    Returns:
        The synthesised ``skill`` directory to put on ``sys.path``.
    """
    fw_skill = tmp_path / "framework" / "skill"
    scripts_dir = fw_skill / "implement" / "scripts"
    scripts_dir.mkdir(parents=True)
    (fw_skill / "shared").mkdir()

    for package in (fw_skill, fw_skill / "implement"):
        shutil.copy(SKILL_ROOT / "__init__.py", package / "__init__.py")
    for package_init in (SKILL_ROOT / "implement" / "__init__.py",
                         SKILL_ROOT / "implement" / "scripts" / "__init__.py"):
        shutil.copy(package_init, fw_skill / package_init.relative_to(SKILL_ROOT))
    shutil.copy(
        SKILL_ROOT / "implement" / "scripts" / "implement.py",
        scripts_dir / "implement.py",
    )
    shutil.copy(SKILL_ROOT / "import_guard.py", fw_skill / "import_guard.py")
    (fw_skill / "test").symlink_to(SKILL_ROOT / "test", target_is_directory=True)

    for module in sorted((SKILL_ROOT / "shared").glob("*.py")):
        (fw_skill / "shared" / module.name).symlink_to(module)

    if detector_source is not None:
        doctored = fw_skill / "shared" / "self_referential_upstream.py"
        doctored.unlink()
        doctored.write_text(detector_source)

    return fw_skill


def _run_probe(tmp_path: Path, fw_skill: Path) -> tuple[dict, subprocess.CompletedProcess]:
    repo_dir = tmp_path / "probe-repo"
    repo_dir.mkdir()
    probe = tmp_path / "probe_optional_import.py"
    probe.write_text(PROBE_SOURCE)

    proc = subprocess.run(
        [sys.executable, str(probe), str(fw_skill), str(repo_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"importing implement.py must never fail: {proc.stdout}{proc.stderr}"
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    return json.loads(proc.stdout), proc


class TestOptionalImportFailOpen:
    def test_non_import_error_at_import_time_does_not_propagate(self, tmp_path):
        """A broken detector module degrades to ``None``, never an exception."""
        fw_skill = _build_framework_tree(tmp_path, BROKEN_DETECTOR_SOURCE)

        report, _proc = _run_probe(tmp_path, fw_skill)

        assert report["symbol_available"] is False, (
            "an import-time RuntimeError must leave the hook unavailable"
        )
        assert report["verdict"]["status"] == "skipped"
        assert "not available" in report["verdict"]["reason"]

    def test_real_detector_is_available_through_the_same_probe(self, tmp_path):
        """Control: the probe resolves the real detector when it is intact.

        The probe repo is not a git repository, so the verdict itself is a
        skip — but for an *environment* reason, never the module-absence
        reason the doctored tree reports.
        """
        fw_skill = _build_framework_tree(tmp_path)

        report, _proc = _run_probe(tmp_path, fw_skill)

        assert report["symbol_available"] is True
        assert "not available" not in report["verdict"].get("reason", "")
