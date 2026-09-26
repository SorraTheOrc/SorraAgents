"""Regression test for SA-0MUFSP9QN005KYWT: pi-client extension install wiring.

Verifies that `scripts/install_pi.sh` symlinks this repository's
`pi-client/*` extensions (including `voice-input`) into the global
`~/.pi/agent/extensions/` discovery directory, and that the wiring is
idempotent.

Install-wiring assertions run against a sandboxed `$HOME` so the real
installation is never touched.
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


def _run_install(home: Path) -> subprocess.CompletedProcess:
    """Run install_pi.sh with a sandboxed HOME and no hook wiring."""
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
        timeout=120,
    )


@pytest.fixture()
def sandbox_home():
    home = Path(tempfile.mkdtemp(prefix="install_ext_test_"))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


class TestExtensionSymlinkWiring:
    def test_voice_input_extension_is_symlinked(self, sandbox_home):
        result = _run_install(sandbox_home)
        link = sandbox_home / ".pi" / "agent" / "extensions" / "voice-input"
        assert link.is_symlink(), (
            f"voice-input extension not symlinked at {link}; "
            f"install output:\n{result.stdout}\n{result.stderr}"
        )
        target = Path(os.path.realpath(link))
        assert target == REPO_ROOT / "pi-client" / "voice-input", (
            f"voice-input symlink points at {target}, expected the repo extension"
        )

    def test_extension_entry_point_is_reachable_through_the_symlink(self, sandbox_home):
        _run_install(sandbox_home)
        link = sandbox_home / ".pi" / "agent" / "extensions" / "voice-input"
        assert (link / "index.ts").is_file()

    def test_sibling_extensions_are_also_wired(self, sandbox_home):
        _run_install(sandbox_home)
        extensions_dir = sandbox_home / ".pi" / "agent" / "extensions"
        assert (extensions_dir / "proxy-sse-signals").is_symlink()

    def test_wiring_is_idempotent(self, sandbox_home):
        _run_install(sandbox_home)
        link = sandbox_home / ".pi" / "agent" / "extensions" / "voice-input"
        first_target = os.path.realpath(link)

        result = _run_install(sandbox_home)
        assert result.returncode == 0, f"re-run failed: {result.stderr}"
        assert link.is_symlink()
        assert os.path.realpath(link) == first_target
