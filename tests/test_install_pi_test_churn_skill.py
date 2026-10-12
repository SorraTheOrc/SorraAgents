"""Install-wiring test for the test-churn skill (SA-0MV28WS5G004YNNS).

Verifies that a fresh ``scripts/install_pi.sh`` run exposes the test-churn
skill through the global skills symlink and reports it explicitly. Runs
against a sandboxed ``$HOME`` so the real installation is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_pi.sh"


@pytest.fixture()
def sandbox_home():
    home = Path(tempfile.mkdtemp(prefix="test_churn_install_"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _run_install(home: Path) -> subprocess.CompletedProcess:
    src_link = home / "projects" / "SorraAgents"
    src_link.parent.mkdir(parents=True, exist_ok=True)
    if not src_link.exists():
        src_link.symlink_to(REPO_ROOT)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PI_SKIP_HOOK_INSTALL"] = "1"
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        check=False,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_fresh_install_exposes_test_churn_skill(sandbox_home):
    result = _run_install(sandbox_home)
    assert result.returncode == 0, result.stderr

    exposed = (
        sandbox_home / ".pi" / "agent" / "skills" / "test-churn" / "SKILL.md"
    )
    assert exposed.is_file(), (
        f"test-churn SKILL.md not exposed after install: {exposed}\n"
        f"install output:\n{result.stdout}"
    )
    assert "OK: skill exposed: test-churn" in result.stdout
