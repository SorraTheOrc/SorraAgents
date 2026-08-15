"""F1 test contract (SA-0MSTMW275003BDWU): suite-command resolution.

Pins the suite-command resolution contract for the audit skill's
execution-dependent verification:

- AC1 (GREEN since F2): ``full_suite_commands`` on a TCE-like repo (npm test
  script, no pytest config, no ``tests/{unit,node,cli}`` dirs) must detect
  the ``npm test`` suite command — never a phantom pytest command.
- AC4 (GREEN since F2): ``full_suite_commands`` must read
  ``<project_root>/.pi/test-config.json`` when present and use its
  ``suiteCommands`` entries as the primary command list.

Both were RED at F1's commit (the phantom pytest command was emitted
unconditionally and the extension file was ignored); F2
(SA-0MSTMYE79006NA61) landed the behavior and removed the ``xfail(strict)``
markers that pinned the red state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.test.scripts.run_tests import full_suite_commands


def _write_tce_like_repo(root: Path) -> None:
    """Shape *root* like Tableau-Card-Engine: npm test, no pytest config,
    no ``tests/{unit,node,cli}`` dirs."""
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )


def _write_extension_file(root: Path, suite_commands: list[str]) -> None:
    """Write ``.pi/test-config.json`` with the given *suite_commands*."""
    (root / ".pi").mkdir(exist_ok=True)
    (root / ".pi" / "test-config.json").write_text(
        json.dumps(
            {
                "suiteCommands": suite_commands,
                "timeoutPerCommand": 600,
            }
        ),
        encoding="utf-8",
    )


class TestTceLayoutDetection:
    """AC1 (GREEN since F2): npm-test convention detection for TCE-like repos.

    The TCE incident (SA-0MSTEW41N005ZCBC): Tableau-Card-Engine is vitest /
    ``npm test`` with no ``tests/{unit,node,cli}`` dirs and no pytest config,
    yet ``full_suite_commands`` emitted the phantom pytest command
    unconditionally, so its real suite was never cacheable/verifiable.
    """

    def test_tce_like_repo_detects_npm_test(self, tmp_path: Path) -> None:
        """A TCE-like repo resolves to the npm test suite command — never a
        phantom pytest command."""
        _write_tce_like_repo(tmp_path)
        cmds = full_suite_commands(tmp_path)
        assert any("npm --silent test" in c for c in cmds), (
            f"expected an npm test command in {cmds}"
        )
        assert not any(c.startswith("pytest") for c in cmds), (
            f"phantom pytest command emitted for a no-pytest repo: {cmds}"
        )

    def test_tce_like_repo_emits_no_phantom_pytest(self, tmp_path: Path) -> None:
        """The npm-test command fully replaces the pytest command for a
        no-pytest repo (no pytest fallback alongside npm test)."""
        _write_tce_like_repo(tmp_path)
        cmds = full_suite_commands(tmp_path)
        assert all("npm" in c for c in cmds), (
            f"expected only npm-based commands for a TCE-like repo: {cmds}"
        )


class TestExtensionFileResolution:
    """AC4 (GREEN since F2): the per-project extension file.

    ``<project_root>/.pi/test-config.json`` with a ``suiteCommands`` list is
    the primary suite-command source; when present, convention detection is
    skipped (F2 AC1).
    """

    def test_extension_file_suite_commands_used(self, tmp_path: Path) -> None:
        """``suiteCommands`` from the extension file drive the returned set."""
        suite_commands = ["npm --silent test", "npm run build"]
        _write_extension_file(tmp_path, suite_commands)
        assert full_suite_commands(tmp_path) == suite_commands

    def test_extension_file_skips_convention_detection(self, tmp_path: Path) -> None:
        """A TCE-like repo WITH an extension file uses the extension commands
        — the npm-test convention must not be re-detected."""
        _write_tce_like_repo(tmp_path)
        suite_commands = ["npm run custom:suite"]
        _write_extension_file(tmp_path, suite_commands)
        assert full_suite_commands(tmp_path) == suite_commands
