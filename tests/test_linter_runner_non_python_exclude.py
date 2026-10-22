"""Ruff must never lint non-Python config files (SA-0MT2Q10PX0078DEM).

The whole-project ruff scan path (``files=None``) previously only excluded
TS/JS extensions. When a project's ruff config routes ``*.yaml`` into the
checker (as the llm project's did — LP-0MT1F4IQU00828FC), the whole-project
scan linted YAML as Python and accumulated 155 false-positive
``invalid-syntax`` findings on ``proxy/config-cheap.yaml``.

These tests pin an **additive, project-agnostic** exclusion in BOTH ruff
paths (whole-project scan and explicit-file scan): ``--extend-exclude``
covers the non-Python config extensions ``.yaml``/``.yml``/``.json``/
``.toml``/``.md`` (plus the pre-existing TS/JS entries) via glob patterns
only — no project-specific paths or names — while genuine ``.py`` findings
are never suppressed.

The two skill copies that ship ``linter_runner`` are exercised
(``code_review.scripts.linter_runner`` is the live import used by the
audit/refactor paths; ``skill/code-review/scripts/linter_runner.py`` is the
documented-canonical copy that has since drifted) so the fix cannot silently
regress the other copy.
"""

from __future__ import annotations

import importlib.util
import inspect
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_review.scripts import linter_runner

#: Extensions the exclusion must cover (at minimum, per the ACs).
NON_PYTHON_EXTENSIONS = (".yaml", ".yml", ".json", ".toml", ".md")
#: Pre-existing TS/JS extensions that must remain excluded.
TS_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
#: Glob suffixes that make a project-agnostic (non-hardcoded) pattern.
_EXPECTED_GLOBS = tuple(
    f"**/*{ext}" for ext in (*TS_JS_EXTENSIONS, *NON_PYTHON_EXTENSIONS)
)


def _mock_result(returncode: int = 0, stdout: str = "[]", stderr: str = ""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class _RecordingRunner:
    """Runner stub that records every constructed command."""

    def __init__(self, stdout: str = "[]", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return _mock_result(self._returncode, self._stdout)


# ---------------------------------------------------------------------------
# Loading both shipped copies of linter_runner
# ---------------------------------------------------------------------------


def _load_hyphen_copy():
    """Load skill/code-review/scripts/linter_runner.py in isolation.

    The hyphen-named copy prepends its own script directory to sys.path on
    import; sys.path and the flat ``detection`` module it registers are
    restored afterwards so the session is not polluted for sibling tests.
    """
    path = REPO_ROOT / "skill" / "code-review" / "scripts" / "linter_runner.py"
    before_path = list(sys.path)
    before_modules = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(
            "_hyphen_linter_runner", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = before_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


def _recorded_ruff_calls(module, root: Path, files=None, fix: bool = False):
    """Run a ruff command builder and return the recorded commands."""
    runner = _RecordingRunner()
    # The hyphen-named copy has no explicit-file scope (its builders take no
    # ``files`` argument), so only pass it where the module supports it.
    kwargs = {"files": files or None} if "files" in inspect.signature(
        module._run_ruff_fix_mode if fix else module._run_ruff_check
    ).parameters else {}
    if fix:
        module._run_ruff_fix_mode(root, runner, **kwargs)
    else:
        module._run_ruff_check(root, runner, **kwargs)
    return runner.calls


def _assert_exclude_covers_non_python(cmd: list[str]):
    """Assert the --extend-exclude value covers all expected globs."""
    assert "--extend-exclude" in cmd, f"missing --extend-exclude in {cmd}"
    value = cmd[cmd.index("--extend-exclude") + 1]
    for glob in _EXPECTED_GLOBS:
        assert glob in value, f"missing {glob} in exclude: {value!r}"
    assert "--force-exclude" in cmd


def _assert_project_agnostic(cmd: list[str]):
    """Assert the exclusion carries no project-specific path or name."""
    value = cmd[cmd.index("--extend-exclude") + 1]
    for part in value.split(","):
        assert part.startswith("**/*."), f"non-glob exclusion part: {part!r}"
        assert not part.startswith("/"), f"absolute path in exclusion: {part!r}"
    for banned in ("/home/", "/tmp", "llm", "proxy/"):
        assert banned not in value.lower(), f"project-specific {banned!r} in {value!r}"


# ---------------------------------------------------------------------------
# Command construction (live copy: code_review.scripts.linter_runner)
# ---------------------------------------------------------------------------


class TestRuffWholeProjectScanExclude:
    """files=None (whole-project scan) excludes non-Python config files."""

    @pytest.mark.parametrize(
        "files, target", [(None, "/proj"), ([], "/proj")]
    )
    def test_whole_project_command_excludes_non_python(self, files, target):
        root = Path(target)
        calls = _recorded_ruff_calls(linter_runner, root, files=files)
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "ruff" and "check" in cmd
        assert str(root) in cmd
        _assert_exclude_covers_non_python(cmd)
        _assert_project_agnostic(cmd)

    def test_explicit_files_command_filters_yaml_and_excludes(self):
        root = Path("/proj")
        calls = _recorded_ruff_calls(
            linter_runner,
            root,
            files=[
                "src/app.py",
                "config.yaml",
                "service/config.yml",
                "data.json",
                "pyproject.toml",
                "README.md",
                "legacy.ts",
            ],
        )
        assert len(calls) == 1
        cmd = calls[0]
        # Only Python sources are passed as explicit targets.
        targets = [
            t for t in cmd[1:] if t.startswith("/proj") or t.endswith(".py")
        ]
        assert "src/app.py" in targets
        for non_py in ("config.yaml", "config.yml", "data.json",
                       "pyproject.toml", "README.md", "legacy.ts"):
            assert non_py not in cmd, f"{non_py} leaked into ruff command"
        _assert_exclude_covers_non_python(cmd)

    def test_py_only_explicit_scope_still_lints_python(self):
        """Genuine .py targets are passed through unchanged (AC2)."""
        root = Path("/proj")
        calls = _recorded_ruff_calls(
            linter_runner, root, files=["src/app.py", "src/missing.rs"]
        )
        assert len(calls) == 1
        assert "src/app.py" in calls[0]
        assert "src/missing.rs" not in calls[0]


class TestRuffExplicitFileExtensionFilter:
    """AC1: explicit-file scope is narrowed to Python extensions.

    Ruff lints every explicitly-passed path as Python and ignores
    ``exclude`` for explicit arguments, so the file list itself must be
    filtered. ``.ipynb`` is a supported ruff target; ``.pyx`` is not (ruff
    parses it as Python and emits ``invalid-syntax``), so it is excluded.
    """

    def test_python_source_extensions_match_the_contract(self):
        assert linter_runner.PYTHON_SOURCE_EXTENSIONS == (
            ".py", ".pyi", ".ipynb",
        )

    @pytest.mark.parametrize(
        "target",
        ["config.yaml", "service/config.yml", "data.json", "pyproject.toml",
         "README.md", "legacy.ts", "notes.txt", "ext.pyx"],
    )
    @pytest.mark.parametrize("fix", [False, True])
    def test_non_python_target_is_never_passed(self, target, fix):
        calls = _recorded_ruff_calls(
            linter_runner, Path("/proj"), files=["src/app.py", target],
            fix=fix,
        )
        assert calls
        for cmd in calls:
            assert target not in cmd, f"{target} leaked into ruff: {cmd}"
            assert "src/app.py" in cmd

    @pytest.mark.parametrize("fix", [False, True])
    def test_python_targets_including_notebooks_are_passed(self, fix):
        calls = _recorded_ruff_calls(
            linter_runner, Path("/proj"),
            files=["src/app.py", "src/app.pyi", "analysis.ipynb"], fix=fix,
        )
        assert calls
        for cmd in calls:
            assert "src/app.py" in cmd
            assert "src/app.pyi" in cmd
            assert "analysis.ipynb" in cmd

    @pytest.mark.parametrize("fix", [False, True])
    def test_non_python_only_scope_does_not_invoke_ruff(self, fix):
        calls = _recorded_ruff_calls(
            linter_runner, Path("/proj"),
            files=["config.yaml", "service/config.yml", "README.md"],
            fix=fix,
        )
        assert calls == []

    @pytest.mark.skipif(
        shutil.which("ruff") is None, reason="ruff not on PATH"
    )
    def test_run_ruff_with_non_python_scope_returns_no_findings(self):
        """End-to-end through run_ruff: nothing to lint, nothing to report."""
        root = Path(__file__)
        runner = _RecordingRunner(
            stdout='[{"code": "invalid-syntax", "message": "Expected a '
                   'statement", "location": {"row": 1, "column": 1}}]',
            returncode=1,
        )
        result = linter_runner.run_ruff(
            root, runner=runner, files=["config.yaml", "README.md"],
        )
        assert runner.calls == []
        assert result["findings"] == []


class TestRuffFixModeExclude:
    """_run_ruff_fix_mode excludes non-Python config files on both passes."""

    def test_whole_project_fix_and_rescan_exclude_non_python(self):
        root = Path("/proj")
        calls = _recorded_ruff_calls(linter_runner, root, files=None, fix=True)
        assert len(calls) == 2  # fix pass + rescan pass
        for cmd in calls:
            assert cmd[0] == "ruff" and "check" in cmd
            _assert_exclude_covers_non_python(cmd)
            _assert_project_agnostic(cmd)

    def test_explicit_scope_fix_and_rescan_exclude_non_python(self):
        root = Path("/proj")
        calls = _recorded_ruff_calls(
            linter_runner,
            root,
            files=["src/app.py", "config.yaml", "README.md"],
            fix=True,
        )
        assert len(calls) == 2
        for cmd in calls:
            assert "config.yaml" not in cmd
            assert "README.md" not in cmd
            _assert_exclude_covers_non_python(cmd)


# ---------------------------------------------------------------------------
# Additivity across both shipped copies (AC3 + parity)
# ---------------------------------------------------------------------------


class TestBothLinterRunnerCopies:
    """skill/code_review/scripts (live) and skill/code-review/scripts (canonical
    copy) must carry the identical, project-agnostic exclusion so the fix
    holds wherever ruff is invoked."""

    def test_hyphen_copy_whole_project_scan_excludes_non_python(self):
        hyphen = _load_hyphen_copy()
        calls = _recorded_ruff_calls(hyphen, Path("/proj"), files=None)
        assert len(calls) == 1
        _assert_exclude_covers_non_python(calls[0])
        _assert_project_agnostic(calls[0])

    def test_hyphen_copy_fix_mode_excludes_non_python(self):
        hyphen = _load_hyphen_copy()
        calls = _recorded_ruff_calls(hyphen, Path("/proj"), files=None, fix=True)
        assert len(calls) == 2
        for cmd in calls:
            _assert_exclude_covers_non_python(cmd)

    def test_both_copies_build_identical_exclusion(self):
        hyphen = _load_hyphen_copy()
        live_value = linter_runner.RUFF_NON_PYTHON_EXCLUDE
        assert hyphen.RUFF_NON_PYTHON_EXCLUDE == live_value


# ---------------------------------------------------------------------------
# Behavioural regression against the real ruff binary (AC4)
# ---------------------------------------------------------------------------

_RUFF_AVAILABLE = shutil.which("ruff") is not None


@pytest.mark.skipif(not _RUFF_AVAILABLE, reason="ruff executable not on PATH")
class TestRealRuffNonPythonScan:
    """Scanning a tree containing YAML (whole-project and explicit-file)
    yields zero findings for the YAML while a genuine .py issue still
    surfaces."""

    @pytest.fixture()
    def project(self, tmp_path: Path, monkeypatch) -> Path:
        # Ruff resolves --extend-exclude patterns relative to the directory it
        # is invoked from, so run from inside the project exactly as the audit
        # does for a real project root.
        monkeypatch.chdir(tmp_path)
        # A project whose ruff config deliberately routes *.yaml into the
        # checker — the configuration under which the llm project's
        # proxy/config-cheap.yaml produced 155 false positives.
        (tmp_path / "ruff.toml").write_text(
            'include = ["*.py", "*.yaml"]\n', encoding="utf-8"
        )
        (tmp_path / "config.yaml").write_text(
            "name: demo\nservers:\n  - host: a\n    port: 8080\n",
            encoding="utf-8",
        )
        (tmp_path / "service").mkdir()
        (tmp_path / "service" / "config.yml").write_text(
            "endpoint:\n  url: /api\n  retries: 3\n", encoding="utf-8"
        )
        # A .py file with a genuine, automatable ruff finding (F401).
        (tmp_path / "fixable.py").write_text(
            "import os\n", encoding="utf-8"
        )
        # A .py file ruff cannot auto-fix (E999 invalid syntax).
        (tmp_path / "broken.py").write_text(
            "def broken(:\n    pass\n", encoding="utf-8"
        )
        return tmp_path

    def test_whole_project_scan_ignores_yaml_but_reports_py(
        self, project: Path
    ):
        result = linter_runner.run_ruff(project)
        filenames = [f["file"] for f in result["findings"]]
        assert filenames, "expected genuine .py findings"
        assert all(f.endswith(".py") for f in filenames)
        assert not any(
            f.endswith((".yaml", ".yml")) for f in filenames
        )
        assert any(f.endswith("broken.py") for f in filenames)

    def test_explicit_file_scan_ignores_yaml_but_reports_py(
        self, project: Path
    ):
        yaml = str(project / "config.yaml")
        broken = str(project / "broken.py")
        result = linter_runner.run_ruff(project, files=[yaml, broken])
        filenames = [f["file"] for f in result["findings"]]
        assert any(f.endswith("broken.py") for f in filenames)
        assert not any(f.endswith(".yaml") for f in filenames)

    def test_different_project_roots_build_same_exclusion(
        self, project: Path, tmp_path: Path
    ):
        """Additivity (AC3): the exclusion is identical for any project root."""
        other = tmp_path / "another-project"
        other.mkdir()
        (other / "config.yaml").write_text("k: v\n", encoding="utf-8")
        runner_a = _RecordingRunner()
        runner_b = _RecordingRunner()
        linter_runner._run_ruff_check(project, runner_a, files=None)
        linter_runner._run_ruff_check(other, runner_b, files=None)
        exclude_a = runner_a.calls[0][
            runner_a.calls[0].index("--extend-exclude") + 1
        ]
        exclude_b = runner_b.calls[0][
            runner_b.calls[0].index("--extend-exclude") + 1
        ]
        assert exclude_a == exclude_b
        assert str(project) not in exclude_a
        assert str(other) not in exclude_a
