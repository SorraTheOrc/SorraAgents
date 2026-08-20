"""F2 unit tests (SA-0MSTMYE79006NA61): suite-command resolution.

Covers the reworked ``full_suite_commands()`` contract:

- AC1: per-project extension file ``<root>/.pi/test-config.json`` —
  ``suiteCommands`` is the primary command list; convention detection is
  skipped when present; ``timeoutPerCommand`` sets the per-command timeout.
- AC2: npm-test convention fallback — a TCE-like repo (package.json ``test``
  script, no pytest config, no ``tests/{unit,node,cli}`` dirs) resolves to
  ``npm --silent test`` instead of the phantom pytest command.
- AC3: pytest is emitted only when the repo declares a pytest suite
  (pytest.ini, or a pyproject.toml ``[tool.pytest.ini_options]`` marker).
- AC5: backward compatibility — conventional repos (pytest config and/or
  node suite dirs) resolve exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from skill.test.scripts.run_tests import (
    build_parser,
    full_suite_commands,
    node_suite_commands,
    suite_timeout_per_command,
)

PYTEST_CMD = "pytest -q -r a --disable-warnings"


def _write_package_json(root: Path, scripts: dict[str, str] | None = None) -> None:
    """Write a package.json with the given scripts (default: a test script)."""
    (root / "package.json").write_text(
        json.dumps({"scripts": scripts or {"test": "vitest run"}}),
        encoding="utf-8",
    )


def _write_pytest_ini(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")


def _write_test_config(root: Path, suite_commands: list[str], timeout: int | None = None) -> None:
    """Write ``.pi/test-config.json`` with the given suite commands."""
    (root / ".pi").mkdir(exist_ok=True)
    config: dict = {"suiteCommands": suite_commands}
    if timeout is not None:
        config["timeoutPerCommand"] = timeout
    (root / ".pi" / "test-config.json").write_text(
        json.dumps(config), encoding="utf-8",
    )


class TestExtensionFileResolution:
    """AC1: the per-project extension file is the primary command source."""

    def test_extension_file_commands_used_verbatim(self, tmp_path: Path) -> None:
        """suiteCommands entries are returned verbatim (no convention re-detection)."""
        commands = ["npm --silent test", "npm run build"]
        _write_test_config(tmp_path, commands)
        assert full_suite_commands(tmp_path) == commands

    def test_extension_file_skips_convention_detection(self, tmp_path: Path) -> None:
        """A repo with BOTH an extension file and a pytest config uses the
        extension commands only — the convention is never re-detected."""
        _write_pytest_ini(tmp_path)
        _write_package_json(tmp_path)
        commands = ["npm run custom:suite"]
        _write_test_config(tmp_path, commands)
        assert full_suite_commands(tmp_path) == commands

    def test_extension_file_without_suite_commands_falls_back_to_conventions(
        self, tmp_path: Path,
    ) -> None:
        """A present extension file without a suiteCommands list does not
        suppress convention detection (fail-open, never an error)."""
        _write_pytest_ini(tmp_path)
        (tmp_path / ".pi").mkdir(exist_ok=True)
        (tmp_path / ".pi" / "test-config.json").write_text(
            json.dumps({"timeoutPerCommand": 300}), encoding="utf-8",
        )
        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_timeout_per_command_resolved(self, tmp_path: Path) -> None:
        """timeoutPerCommand is exposed for per-command execution."""
        _write_test_config(tmp_path, ["npm --silent test"], timeout=900)
        assert suite_timeout_per_command(tmp_path) == 900

    def test_timeout_per_command_absent(self, tmp_path: Path) -> None:
        """No extension file → no per-command timeout override."""
        assert suite_timeout_per_command(tmp_path) is None


class TestNpmTestConvention:
    """AC2: npm-test convention fallback for TCE-like repos."""

    def test_tce_like_repo_resolves_npm_test(self, tmp_path: Path) -> None:
        """A package.json test script with no pytest config and no node suite
        dirs resolves to the whole-suite npm test command — never phantom
        pytest."""
        _write_package_json(tmp_path)
        cmds = full_suite_commands(tmp_path)
        assert cmds == ["npm --silent test"]
        assert not any(c.startswith("pytest") for c in cmds)

    def test_npm_test_not_used_when_node_dirs_exist(self, tmp_path: Path) -> None:
        """A repo with a tests/unit dir uses the per-dir node command, not the
        whole-suite npm fallback."""
        _write_package_json(tmp_path)
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        cmds = full_suite_commands(tmp_path)
        assert "npm --silent test -- tests/unit" in cmds
        assert "npm --silent test" not in cmds


class TestPytestDetection:
    """AC3: pytest is emitted only for repos that declare a pytest suite."""

    def test_pytest_ini_emits_pytest(self, tmp_path: Path) -> None:
        _write_pytest_ini(tmp_path)
        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_pyproject_marker_emits_pytest(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
            encoding="utf-8",
        )
        assert full_suite_commands(tmp_path) == [PYTEST_CMD]

    def test_node_dirs_only_emits_no_pytest(self, tmp_path: Path) -> None:
        """Node suite dirs without any pytest config must NOT drag in a
        phantom pytest command (SA-0MSQ72BVV0011SRU AC3)."""
        for d in ("tests/node", "tests/cli", "tests/unit"):
            (tmp_path / d).mkdir(parents=True)
        cmds = full_suite_commands(tmp_path)
        assert cmds  # node commands remain
        assert not any("pytest" in c for c in cmds)

    def test_bare_repo_yields_empty_set(self, tmp_path: Path) -> None:
        """No pytest config, no node dirs, no package.json → no commands at
        all (the audit's never-block path reports this documented reason)."""
        assert full_suite_commands(tmp_path) == []


class TestBackwardCompatibility:
    """AC5: conventional repos keep resolving exactly as before."""

    def test_pytest_plus_all_node_dirs_unchanged(self, tmp_path: Path) -> None:
        """pytest config + all three node suite dirs → pytest + 3 node cmds."""
        _write_pytest_ini(tmp_path)
        for d in ("tests/node", "tests/cli", "tests/unit"):
            (tmp_path / d).mkdir(parents=True)
        cmds = full_suite_commands(tmp_path)
        assert cmds[0] == PYTEST_CMD
        assert len(cmds) == 4
        joined = " | ".join(cmds)
        assert "tests/node" in joined
        assert "tests/cli" in joined
        assert "tests/unit" in joined

    def test_missing_node_dirs_still_skipped(self, tmp_path: Path) -> None:
        """SA-0MSJELL44009XYIL: missing node suite dirs are skipped, pytest
        config keeps its command."""
        _write_pytest_ini(tmp_path)
        (tmp_path / "tests" / "cli").mkdir(parents=True)
        cmds = full_suite_commands(tmp_path)
        assert cmds[0] == PYTEST_CMD
        assert len(cmds) == 2
        assert "tests/node" not in " | ".join(cmds)
        assert "tests/unit" not in " | ".join(cmds)

    def test_node_suite_commands_still_per_dir_with_npm_script(
        self, tmp_path: Path,
    ) -> None:
        """node_suite_commands keeps its npm per-dir form for node dirs."""
        _write_package_json(tmp_path)
        (tmp_path / "tests" / "unit").mkdir(parents=True)
        cmds = node_suite_commands(tmp_path)
        assert cmds == ["npm --silent test -- tests/unit"]


class TestTimeoutResolution:
    """Test the timeout resolution chain in main().

    Root cause: ``--timeout`` argparse default=600 made ``args.timeout`` always
    truthy, short-circuiting the or-chain
    ``args.timeout or suite_timeout_per_command(project_root) or 600``.

    Fix: default=None so the or-chain falls through to config, then 600.

    Acceptance Criteria:
    - AC: --timeout argparse default is None (not 600)
    - AC: Without --timeout, suite_timeout_per_command() config value is consulted
    - AC: --timeout CLI flag overrides config when explicitly provided
    - AC: Fallback to 600 still applies when neither --timeout nor config is set
    """

    def test_args_timeout_default_is_none(self) -> None:
        """args.timeout should be None when --timeout is not provided,
        allowing the or-chain to fall through to suite_timeout_per_command."""
        args = build_parser().parse_args([])
        assert args.timeout is None

    def test_args_timeout_set_when_provided(self) -> None:
        """When --timeout is explicitly provided, it should have the given value."""
        args = build_parser().parse_args(["--timeout", "120"])
        assert args.timeout == 120

    def test_timeout_from_config_when_no_flag(self) -> None:
        """Without --timeout, suite_timeout_per_command config is consulted.

        Simulates: args.timeout=None, config returns 900.
        Resolution: None or 900 or 600 → 900.
        """
        args = build_parser().parse_args([])
        config_timeout = 900
        resolved = args.timeout or config_timeout or 600
        assert resolved == 900

    def test_timeout_fallback_to_600_when_no_config(self) -> None:
        """Without --timeout and without config, fallback to 600.

        Simulates: args.timeout=None, config returns None.
        Resolution: None or None or 600 → 600.
        """
        args = build_parser().parse_args([])
        config_timeout = None
        resolved = args.timeout or config_timeout or 600
        assert resolved == 600

    def test_timeout_cli_overrides_config(self) -> None:
        """When --timeout is explicitly provided, it overrides config.

        Simulates: args.timeout=120, config returns 900.
        Resolution: 120 or 900 or 600 → 120 (CLI wins).
        """
        args = build_parser().parse_args(["--timeout", "120"])
        config_timeout = 900
        resolved = args.timeout or config_timeout or 600
        assert resolved == 120

    def test_timeout_chain_integration_config_applied(self) -> None:
        """Integration: main() passes correct timeout to run_all when
        suite_timeout_per_command returns a config value.

        Verifies the full chain: main() parses args → resolves timeout via
        or-chain → passes to run_all().
        """
        from skill.test.scripts import run_tests as run_tests_module

        captured_timeout: list[int | None] = [None]

        def capture_run_all(*args, **kwargs):
            captured_timeout[0] = kwargs.get("timeout")
            return {
                "success": True,
                "suites": {},
                "failures": [],
                "notices": [],
            }

        with patch.object(
            run_tests_module,
            "suite_timeout_per_command",
            return_value=900,
        ), patch.object(
            run_tests_module,
            "run_all",
            side_effect=capture_run_all,
        ):
            try:
                run_tests_module.main([])
            except SystemExit:
                pass  # main() calls sys.exit(0) on success

        assert captured_timeout[0] == 900, (
            f"Expected timeout 900 from config, got {captured_timeout[0]}"
        )

    def test_timeout_chain_integration_cli_overrides(self) -> None:
        """Integration: main() passes CLI --timeout to run_all, ignoring config.

        Verifies that explicit --timeout 120 overrides a config value of 900.
        """
        from skill.test.scripts import run_tests as run_tests_module

        captured_timeout: list[int | None] = [None]

        def capture_run_all(*args, **kwargs):
            captured_timeout[0] = kwargs.get("timeout")
            return {
                "success": True,
                "suites": {},
                "failures": [],
                "notices": [],
            }

        with patch.object(
            run_tests_module,
            "suite_timeout_per_command",
            return_value=900,
        ), patch.object(
            run_tests_module,
            "run_all",
            side_effect=capture_run_all,
        ):
            try:
                run_tests_module.main(["--timeout", "120"])
            except SystemExit:
                pass

        assert captured_timeout[0] == 120, (
            f"Expected CLI timeout 120 to override config, got {captured_timeout[0]}"
        )

    def test_timeout_chain_integration_fallback_to_600(self) -> None:
        """Integration: main() falls back to 600 when config is absent.

        Verifies: no --timeout, config returns None → timeout = 600.
        """
        from skill.test.scripts import run_tests as run_tests_module

        captured_timeout: list[int | None] = [None]

        def capture_run_all(*args, **kwargs):
            captured_timeout[0] = kwargs.get("timeout")
            return {
                "success": True,
                "suites": {},
                "failures": [],
                "notices": [],
            }

        with patch.object(
            run_tests_module,
            "suite_timeout_per_command",
            return_value=None,
        ), patch.object(
            run_tests_module,
            "run_all",
            side_effect=capture_run_all,
        ):
            try:
                run_tests_module.main([])
            except SystemExit:
                pass

        assert captured_timeout[0] == 600, (
            f"Expected fallback timeout 600, got {captured_timeout[0]}"
        )
