"""Code-quality scans must not depend on the caller's working directory.

Ghost ``E902`` findings (WL-0MWVZEZXC00748QD): the audit pipeline validates a
linter's file scope against ``project_root`` (the audit code root), but the
invocation ran with the **inherited** process cwd. Ruff resolves relative path
arguments — and config discovery — against that cwd, so a scan driven from a
*different* repository reported ``E902 No such file or directory`` for files
that exist under ``project_root`` (see WL-0MWTZEDWG008YMGQ, audit comment
2026-11-25T05:49:58Z: three false-HIGH ``E902`` findings on ContextHub paths
while the audit code root was SorraAgents).

These tests pin two properties of the ruff invocation path:

1. **Cwd independence** — with the process cwd deliberately different from
   ``project_root``, a file that exists under ``project_root`` produces no
   finding, because the child process cwd is pinned to ``project_root``.
2. **No over-broad suppression** — a genuinely absent path still reports
   ``E902``, so real findings are never silenced.

No real ``ruff`` binary is required: the runner is injectable and the double
below resolves path arguments against its effective cwd exactly as ruff does,
which is what makes a ghost ``E902`` observable at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_review.scripts import code_quality, linter_runner


def _mock_result(returncode: int = 0, stdout: str = "[]", stderr: str = ""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class _RuffPathEmulator:
    """Runner double that resolves paths like the real ruff binary.

    Ruff resolves relative path arguments against the **subprocess** cwd, so a
    runner that honours the ``cwd`` keyword reproduces the ghost ``E902`` when
    (and only when) the invocation is not pinned to ``project_root``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], cwd: str | None = None):
        effective_cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.calls.append({"cmd": list(cmd), "cwd": cwd})

        findings: list[dict[str, Any]] = []
        # ``ruff check <targets...> <flags...>`` — targets always precede flags.
        for arg in cmd[2:]:
            if arg.startswith("-"):
                break
            target = Path(arg)
            if not target.is_absolute():
                target = effective_cwd / target
            if not target.exists():
                findings.append(
                    {
                        "code": "E902",
                        "message": "No such file or directory (os error 2)",
                        "filename": str(target),
                        "location": {"row": 1, "column": 1},
                    }
                )

        return _mock_result(1 if findings else 0, json.dumps(findings))

    @property
    def cwds(self) -> list[str | None]:
        """The ``cwd`` keyword each invocation passed (``None`` when absent)."""
        return [call["cwd"] for call in self.calls]


def _project_with_python_file(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "module.py").write_text("x = 1\n", encoding="utf-8")
    return project


def _foreign_cwd(tmp_path: Path, monkeypatch) -> Path:
    """Move the process cwd away from every fixture project root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


class TestScopedRuffScanIsCwdIndependent:
    """The explicit-file (audit changed-file) scope is pinned to the root."""

    def test_existing_file_from_foreign_cwd_emits_no_ghost_e902(
        self, tmp_path, monkeypatch
    ):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _RuffPathEmulator()

        findings = linter_runner._run_ruff_check(
            project, runner, files=["pkg/module.py"]
        )

        assert findings == [], (
            "a file that exists under project_root produced a ghost finding: "
            f"{findings}"
        )
        assert runner.cwds == [str(project)]

    def test_absent_file_still_reports_e902(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _RuffPathEmulator()

        findings = linter_runner._run_ruff_check(
            project, runner, files=["pkg/missing.py"]
        )

        assert [finding["code"] for finding in findings] == ["E902"]
        assert findings[0]["severity"] == "high"
        assert findings[0]["file"] == str(project / "pkg" / "missing.py")
        assert runner.cwds == [str(project)]

    def test_runner_without_cwd_parameter_is_still_supported(
        self, tmp_path, monkeypatch
    ):
        """Legacy cmd-only test doubles keep working (no signature break)."""
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def cmd_only_runner(cmd):
            calls.append(list(cmd))
            return _mock_result(0, "[]")

        findings = linter_runner._run_ruff_check(
            project, cmd_only_runner, files=["pkg/module.py"]
        )

        assert findings == []
        assert calls and calls[0][0] == "ruff"


class TestWholeProjectRuffScanIsCwdIndependent:
    """The whole-project scan is pinned too (config discovery uses cwd)."""

    def test_whole_project_scan_pins_cwd_to_project_root(
        self, tmp_path, monkeypatch
    ):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _RuffPathEmulator()

        findings = linter_runner._run_ruff_check(project, runner)

        assert findings == []
        assert runner.cwds == [str(project)]


class TestRunRuffEntryPointIsCwdIndependent:
    """The public ``run_ruff`` entry point inherits the pinning."""

    def test_run_ruff_from_foreign_cwd_emits_no_ghost_e902(
        self, tmp_path, monkeypatch
    ):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _RuffPathEmulator()

        monkeypatch.setattr(
            linter_runner, "probe_linter", lambda name: {"available": True}
        )
        monkeypatch.setattr(
            linter_runner, "detect_languages", lambda root: ["python"]
        )

        result = linter_runner.run_ruff(project, runner=runner, files=["pkg/module.py"])

        assert result["findings"] == []
        assert runner.cwds == [str(project)]


# ---------------------------------------------------------------------------
# Every other linter invocation must be pinned too (WL-0MWVZF57Y005VOKN)
# ---------------------------------------------------------------------------
#
# The ruff read path was pinned by WL-0MWVZEZXC00748QD; the fix-mode, eslint,
# markdownlint, shellcheck and dotnet-format call sites still invoked the
# runner with the inherited process cwd. These tests assert a single funnel
# (``_invoke_pinned``) is used everywhere, so a code-quality scan can never
# depend on where the caller stands.


class _CwdRecordingRunner:
    """Runner double that records the effective ``cwd`` of every invocation.

    Unlike :class:`_RuffPathEmulator` this does not emulate a linter: the
    assertions here are about *where* the child process would run, which is
    the property that makes the scan cwd-independent. A non-empty default
    stdout keeps the markdownlint/eslint fix-mode commit probe faithful to a
    linter that reported something.
    """

    def __init__(self, stdout: str = "[]", returncode: int = 0, stderr: str = ""):
        self.calls: list[dict[str, Any]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, cmd, cwd=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd})
        return _mock_result(self._returncode, self._stdout, self._stderr)

    def cwds_for(self, binary: str) -> list[str | None]:
        return [call["cwd"] for call in self.calls if call["cmd"][0] == binary]


def _project_with_file(tmp_path: Path, relative: str, content: str = "x\n") -> Path:
    project = tmp_path / "project"
    target = project / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return project


def _force_linter(monkeypatch, language: str, linter: str) -> None:
    """Make ``run_*`` proceed for *language* without a real binary on PATH."""
    monkeypatch.setattr(
        linter_runner, "probe_linter",
        lambda name: {"name": name, "available": True},
    )
    monkeypatch.setattr(
        linter_runner, "detect_languages", lambda root: [language]
    )
    monkeypatch.setattr(
        linter_runner, "get_linters_for_language", lambda lang: [linter]
    )


class TestMarkdownlintIsCwdIndependent:
    def test_check_scan_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "docs/guide.md", "# Guide\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "markdown", "markdownlint")
        runner = _CwdRecordingRunner()

        linter_runner.run_markdownlint(project, runner=runner)

        assert runner.cwds_for("markdownlint") == [str(project)]

    def test_fix_mode_pins_cwd_on_both_passes(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "docs/guide.md", "# Guide\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "markdown", "markdownlint")
        runner = _CwdRecordingRunner()

        linter_runner.run_markdownlint(
            project, runner=runner, fix=True, files=["docs/guide.md"]
        )

        assert runner.cwds_for("markdownlint") == [str(project), str(project)]


class TestEslintIsCwdIndependent:
    def test_check_scan_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "src/app.ts", "export const x = 1;\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "typescript", "eslint")
        runner = _CwdRecordingRunner()

        linter_runner.run_eslint(project, runner=runner, files=["src/app.ts"])

        assert runner.cwds_for("eslint") == [str(project)]

    def test_fix_mode_pins_cwd_on_both_passes(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "src/app.ts", "export const x = 1;\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "typescript", "eslint")
        runner = _CwdRecordingRunner()

        linter_runner.run_eslint(
            project, runner=runner, fix=True, files=["src/app.ts"]
        )

        assert runner.cwds_for("eslint") == [str(project), str(project)]


class TestShellcheckIsCwdIndependent:
    def test_scan_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "scripts/run.sh", "#!/bin/sh\necho hi\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "shell", "shellcheck")
        runner = _CwdRecordingRunner()

        linter_runner.run_shellcheck(project, runner=runner)

        assert runner.cwds_for("shellcheck") == [str(project)]


class TestDotnetFormatIsCwdIndependent:
    def test_check_scan_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "src/Program.cs", "class P {}\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "csharp", "dotnet-format")
        runner = _CwdRecordingRunner()

        linter_runner.run_dotnet_format(project, runner=runner)

        assert runner.cwds_for("dotnet") == [str(project)]

    def test_fix_mode_pins_cwd_on_both_passes(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "src/Program.cs", "class P {}\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "csharp", "dotnet-format")
        runner = _CwdRecordingRunner()

        linter_runner.run_dotnet_format(project, runner=runner, fix=True)

        assert runner.cwds_for("dotnet") == [str(project), str(project)]


class TestRuffFixModeIsCwdIndependent:
    def test_fix_and_rescan_are_pinned(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _CwdRecordingRunner()

        linter_runner._run_ruff_fix_mode(project, runner, files=["pkg/module.py"])

        assert runner.cwds_for("ruff") == [str(project), str(project)]


class TestSharedFixModeFunnelIsCwdIndependent:
    """``_run_linter_fix_mode`` is the single fix-mode funnel for every linter."""

    def test_fix_and_rescan_are_pinned(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        runner = _CwdRecordingRunner()

        linter_runner._run_linter_fix_mode(
            "demo",
            project,
            runner,
            fix_cmd_builder=lambda root: ["demo", "--fix", str(root)],
            rescan_cmd_builder=lambda root: ["demo", str(root)],
            fixes_detected=lambda result, output: False,
            rescan_parser=lambda result: [],
        )

        assert runner.cwds_for("demo") == [str(project), str(project)]


class TestCmdOnlyRunnerStillSupported:
    """Pinning must never break an injectable runner without a ``cwd`` kwarg."""

    def test_markdownlint_cmd_only_double(self, tmp_path, monkeypatch):
        project = _project_with_file(tmp_path, "docs/guide.md", "# Guide\n")
        _foreign_cwd(tmp_path, monkeypatch)
        _force_linter(monkeypatch, "markdown", "markdownlint")
        calls: list[list[str]] = []

        def cmd_only_runner(cmd):
            calls.append(list(cmd))
            return _mock_result(0, "[]")

        linter_runner.run_markdownlint(project, runner=cmd_only_runner)

        assert calls and calls[0][0] == "markdownlint"


class TestRunCodeQualityIsCwdIndependent:
    """End-to-end: the audit's entry point emits no ghost E902 from a
    foreign cwd."""

    def test_no_e902_for_files_under_project_root(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)

        monkeypatch.setattr(
            linter_runner, "probe_linter",
            lambda name: {"name": name, "available": True},
        )
        monkeypatch.setattr(
            linter_runner, "detect_languages", lambda root: ["python"]
        )
        monkeypatch.setattr(
            linter_runner, "get_linters_for_language", lambda lang: ["ruff"]
        )
        monkeypatch.setattr(code_quality, "probe_linter", linter_runner.probe_linter)
        monkeypatch.setattr(
            code_quality, "detect_languages", linter_runner.detect_languages
        )
        monkeypatch.setattr(
            code_quality, "get_linters_for_language",
            linter_runner.get_linters_for_language,
        )
        runner = _RuffPathEmulator()

        result = code_quality.run_code_quality(
            project_root=project, runner=runner, files=["pkg/module.py"]
        )

        assert result["success"] is True
        assert result["findings"] == [], (
            "code-quality scan produced ghost findings from a foreign cwd: "
            f"{result['findings']}"
        )
        assert runner.cwds == [str(project)]


def _load_hyphen_copy():
    """Load ``skill/code-review/scripts/linter_runner.py`` in isolation."""
    import importlib.util

    path = REPO_ROOT / "skill" / "code-review" / "scripts" / "linter_runner.py"
    before_path = list(sys.path)
    before_modules = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(
            "_hyphen_linter_runner_cwd", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = before_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


class TestHyphenCopyIsCwdIndependent:
    """The documented-canonical ``code-review`` copy is pinned for parity."""

    def test_ruff_check_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        hyphen = _load_hyphen_copy()
        runner = _CwdRecordingRunner()

        hyphen._run_ruff_check(project, runner)

        assert runner.cwds_for("ruff") == [str(project)]

    def test_ruff_fix_mode_pins_cwd(self, tmp_path, monkeypatch):
        project = _project_with_python_file(tmp_path)
        _foreign_cwd(tmp_path, monkeypatch)
        hyphen = _load_hyphen_copy()
        runner = _CwdRecordingRunner()

        hyphen._run_ruff_fix_mode(project, runner)

        assert runner.cwds_for("ruff") == [str(project), str(project)]
