"""Tests for refactor scan-scope path filtering.

The refactor skill must analyse only code that belongs to the project being
inspected: vendored code, npm-installed packages and anything the project's
``.gitignore`` excludes are dropped before auto-fix and smell detection.

Covers:
- AC1: default external directories excluded at any depth (full-project scan).
- AC2: ``.gitignore`` honoured for tracked files too (single batched call).
- AC3: config ``exclude``/``include`` support, additive and overridable.
- AC4: project-owned directories stay in scope.
- AC5: session-boundary scans apply the same filter.
- AC6: report counts reflect the filtered set.

Related work item: SA-0MVPDVXT90067E3M
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_repo(
    root: Path,
    files: dict[str, str],
    gitignore: str | None = None,
    force_add: tuple[str, ...] = (),
) -> Path:
    """Create a temporary git repository containing *files*.

    ``gitignore`` (when given) is written to the repository root as
    ``.gitignore``.  Files listed in ``force_add`` are added with ``git add
    -f`` so they can be *tracked yet gitignored* (the case AC2 pins).
    """
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"], check=True, capture_output=True
    )
    for rel in force_add:
        subprocess.run(
            ["git", "-C", str(root), "add", "-f", rel],
            check=True,
            capture_output=True,
        )
    return root


def _tracked_files(root: Path) -> list[str]:
    """Return the paths tracked by git in *root*."""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


_EXTERNAL_FILES = {
    "node_modules/dep/index.js": "module.exports = 1;\n",
    "vendor/lib.py": "VALUE = 1\n",
    "third_party/other.py": "VALUE = 2\n",
    "third-party/hyphen.py": "VALUE = 3\n",
    "site-packages/pkg/mod.py": "VALUE = 4\n",
    "bower_components/c.js": "var a = 1;\n",
    "jspm_packages/j.js": "var b = 2;\n",
    "packages/npm-pkg/index.ts": "export const c = 3;\n",
    "src/nested/node_modules/deep/index.js": "module.exports = 4;\n",
}

_PROJECT_FILES = {
    "src/app.py": "def main():\n    return 0\n",
    "skill/thing/helper.py": "HELP = 1\n",
    "command/thing/script.py": "CMD = 1\n",
    "scripts/tool.py": "TOOL = 1\n",
    "pi-client/src/index.ts": "export const x = 1;\n",
    "tests/test_app.py": "def test_app():\n    assert True\n",
}


# ---------------------------------------------------------------------------
# AC1 — default external directories are excluded at any depth
# ---------------------------------------------------------------------------


class TestDefaultDirectoryExclusions:
    """Vendored and npm-installed directories never appear in scans."""

    def test_full_project_scan_excludes_external_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full-project scan returns project files only (AC1)."""
        repo = _make_repo(tmp_path, {**_EXTERNAL_FILES, **_PROJECT_FILES})
        monkeypatch.chdir(repo)

        from refactor.scripts.refactor import detect_session_files

        session = detect_session_files("dev", full_project=True, repo_root=repo)
        found = session["all_files"]

        for external in _EXTERNAL_FILES:
            assert external not in found, f"{external} should be excluded"
        assert "src/app.py" in found
        assert "pi-client/src/index.ts" in found

    def test_get_all_source_files_excludes_external_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get_all_source_files`` drops external directories (AC1)."""
        repo = _make_repo(tmp_path, {**_EXTERNAL_FILES, **_PROJECT_FILES})
        monkeypatch.chdir(repo)

        from refactor.session_boundary import get_all_source_files

        found = get_all_source_files()

        assert sorted(found) == sorted(_PROJECT_FILES), (
            "only project-owned source files should be enumerated"
        )

    @pytest.mark.parametrize(
        "excluded",
        [
            ".git/config.py",
            "__pycache__/cached.py",
            "build/out.py",
            "dist/bundle.js",
            ".venv/lib/mod.py",
            "venv/lib/mod.py",
            ".tox/env/mod.py",
            ".eggs/pkg/mod.py",
            "src/pkg.egg-info/SOURCES.py",
            ".worklog/hook.py",
        ],
    )
    def test_pre_existing_default_patterns_still_excluded(self, excluded: str) -> None:
        """The pre-existing default exclusions keep working (AC1)."""
        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            [excluded, "src/app.py"], repo_root=None, use_gitignore=False
        )
        assert result == ["src/app.py"]

    def test_non_source_extensions_excluded_in_full_project_scan(self) -> None:
        """Only source extensions are scanned in full-project mode (AC1)."""
        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["src/app.py", "docs/readme.md", "package.json", "src/app.ts"],
            repo_root=None,
            use_gitignore=False,
        )
        assert result == ["src/app.py", "src/app.ts"]


# ---------------------------------------------------------------------------
# AC4 — project-owned directories stay in scope
# ---------------------------------------------------------------------------


class TestProjectDirectoryRetention:
    """Project-owned directories are never filtered out."""

    def test_project_owned_directories_retained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``skill/``, ``command/``, ``scripts/``, ``pi-client/``, ``tests/`` survive (AC4)."""
        repo = _make_repo(tmp_path, _PROJECT_FILES)
        monkeypatch.chdir(repo)

        from refactor.scripts.refactor import detect_session_files

        session = detect_session_files("dev", full_project=True, repo_root=repo)
        found = set(session["all_files"])

        assert set(_PROJECT_FILES) <= found


# ---------------------------------------------------------------------------
# AC2 — .gitignore rules are honoured for tracked files too
# ---------------------------------------------------------------------------


class TestGitignoreFiltering:
    """Tracked-but-gitignored files are excluded from scans."""

    def test_tracked_but_gitignored_file_excluded(self, tmp_path: Path) -> None:
        """A tracked file matching ``.gitignore`` is excluded (AC2)."""
        repo = _make_repo(
            tmp_path,
            {"vendor/lib.py": "VALUE = 1\n", "src/app.py": "VALUE = 2\n"},
            gitignore="vendor/\n",
            force_add=("vendor/lib.py",),
        )
        # Guard against a vacuous pass: the vendored file really is tracked.
        assert "vendor/lib.py" in _tracked_files(repo)

        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["vendor/lib.py", "src/app.py"],
            repo_root=repo,
            use_default_excludes=False,
        )
        assert result == ["src/app.py"]

    def test_nested_gitignore_honoured(self, tmp_path: Path) -> None:
        """A nested ``.gitignore`` excludes its sibling files (AC2)."""
        repo = _make_repo(
            tmp_path,
            {"sub/gen.py": "GEN = 1\n", "sub/real.py": "REAL = 1\n"},
            gitignore="",
        )
        (repo / "sub" / ".gitignore").write_text("gen.py\n", encoding="utf-8")

        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["sub/gen.py", "sub/real.py"],
            repo_root=repo,
            use_default_excludes=False,
        )
        assert result == ["sub/real.py"]

    def test_gitignore_filtering_uses_single_batched_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gitignore filtering spawns one ``git check-ignore`` for all paths (AC2)."""
        repo = _make_repo(tmp_path, {"src/app.py": "VALUE = 1\n"}, gitignore="")
        paths = [f"src/module{i}.py" for i in range(5)]

        calls: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(cmd, list) and "check-ignore" in cmd:
                calls.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", counting_run)

        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(paths, repo_root=repo, use_default_excludes=False)

        assert result == paths
        assert len(calls) == 1, "gitignore filtering must be a single batch call"
        assert "--stdin" in calls[0]
        assert "--no-index" in calls[0]

    def test_gitignore_failure_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing ``git check-ignore`` warns and keeps the other filters (AC2)."""
        def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("git is not available")

        monkeypatch.setattr(subprocess, "run", boom)

        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["node_modules/dep/index.js", "src/app.py"],
            repo_root=None,
        )
        assert result == ["src/app.py"]


# ---------------------------------------------------------------------------
# AC3 — configuration is supported and additive
# ---------------------------------------------------------------------------


class TestConfigFilters:
    """``exclude``/``include`` come from ``.refactor.json``."""

    def test_config_exclude_adds_to_defaults(self) -> None:
        """Config excludes are additive to the defaults (AC3)."""
        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["src/legacy/old.py", "vendor/lib.py", "src/app.py"],
            repo_root=None,
            config={"exclude": ["src/legacy"]},
            use_gitignore=False,
        )
        assert result == ["src/app.py"]

    def test_config_include_reincludes_packages(self) -> None:
        """``include`` re-enables a default-excluded directory (AC3)."""
        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["packages/npm-pkg/index.ts", "src/app.py"],
            repo_root=None,
            config={"include": ["packages"]},
            use_gitignore=False,
        )
        assert result == ["packages/npm-pkg/index.ts", "src/app.py"]

    def test_include_overrides_config_exclude(self) -> None:
        """``include`` wins over ``exclude`` for the same path (AC3)."""
        from refactor.path_filters import filter_scannable_paths

        result = filter_scannable_paths(
            ["gen/model.py", "gen/other.py"],
            repo_root=None,
            config={"exclude": ["gen"], "include": ["gen/model.py"]},
            use_gitignore=False,
        )
        assert result == ["gen/model.py"]

    def test_refactor_config_parses_path_keys(self, tmp_path: Path) -> None:
        """``RefactorConfig`` exposes ``path_exclude``/``path_include`` (AC3)."""
        config_file = tmp_path / ".refactor.json"
        config_file.write_text(
            '{"exclude": ["gen"], "include": ["packages"]}', encoding="utf-8"
        )

        from refactor.scripts.config import RefactorConfig

        config = RefactorConfig.load(str(config_file))

        assert config.path_exclude == ["gen"]
        assert config.path_include == ["packages"]
        assert config.to_dict()["exclude"] == ["gen"]
        assert config.to_dict()["include"] == ["packages"]

    def test_missing_path_keys_preserve_defaults(self) -> None:
        """A config without path keys keeps the new default behaviour (AC3)."""
        from refactor.scripts.config import RefactorConfig

        config = RefactorConfig.defaults()

        assert config.path_exclude == []
        assert config.path_include == []
        assert config.linter_enabled is True

    def test_filter_accepts_refactor_config_instance(self) -> None:
        """``filter_scannable_paths`` accepts a ``RefactorConfig`` (AC3)."""
        from refactor.path_filters import filter_scannable_paths
        from refactor.scripts.config import RefactorConfig

        config = RefactorConfig._from_dict({"exclude": ["src/legacy"]})

        result = filter_scannable_paths(
            ["src/legacy/old.py", "src/app.py"],
            repo_root=None,
            config=config,
            use_gitignore=False,
        )
        assert result == ["src/app.py"]

    def test_example_config_file_is_loadable_and_documents_path_keys(
        self, tmp_path: Path
    ) -> None:
        """The shipped example config parses and carries the path keys (AC7)."""
        from refactor.scripts.config import RefactorConfig

        repo_root = Path(__file__).resolve().parents[2]
        example = repo_root / ".refactor.json.example"
        assert example.is_file()

        config = RefactorConfig.load(str(example))

        assert config.path_exclude, "the example should illustrate 'exclude'"
        assert config.path_include, "the example should illustrate 'include'"
        assert config.linter_enabled is True


# ---------------------------------------------------------------------------
# AC5 — session-boundary scans apply the same filter
# ---------------------------------------------------------------------------


class TestSessionScanFiltering:
    """Session scans never analyse external paths."""

    def test_external_session_paths_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changed/untracked external paths are dropped before detection (AC5)."""
        from refactor.scripts import refactor as refactor_module

        monkeypatch.setattr(
            refactor_module,
            "get_changed_files",
            lambda parent_branch="dev": [
                {"status": "M", "file": "node_modules/dep/index.js"},
                {"status": "M", "file": "src/app.py"},
            ],
        )
        monkeypatch.setattr(
            refactor_module,
            "get_untracked_files",
            lambda: ["packages/npm-pkg/a.js", "src/new.py"],
        )

        session = refactor_module.detect_session_files("dev", repo_root=None)

        assert session["all_files"] == ["src/app.py", "src/new.py"]
        assert session["changed"] == [{"status": "M", "file": "src/app.py"}]
        assert session["untracked"] == ["src/new.py"]

    def test_non_source_session_files_retained(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session scans keep non-source changes (only exclusions filter, AC5)."""
        from refactor.scripts import refactor as refactor_module

        monkeypatch.setattr(
            refactor_module,
            "get_changed_files",
            lambda parent_branch="dev": [
                {"status": "M", "file": "docs/notes.md"},
                {"status": "M", "file": "node_modules/dep/index.js"},
            ],
        )
        monkeypatch.setattr(refactor_module, "get_untracked_files", list)

        session = refactor_module.detect_session_files("dev", repo_root=None)

        assert session["all_files"] == ["docs/notes.md"]

    def test_session_filter_honours_config_include(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config ``include`` re-enables a path in session scans (AC5)."""
        from refactor.scripts import refactor as refactor_module

        monkeypatch.setattr(
            refactor_module,
            "get_changed_files",
            lambda parent_branch="dev": [
                {"status": "M", "file": "packages/npm-pkg/index.ts"},
            ],
        )
        monkeypatch.setattr(refactor_module, "get_untracked_files", list)

        session = refactor_module.detect_session_files(
            "dev", config={"include": ["packages"]}, repo_root=None
        )

        assert session["all_files"] == ["packages/npm-pkg/index.ts"]


# ---------------------------------------------------------------------------
# AC6 — report counts reflect the filtered set
# ---------------------------------------------------------------------------


class TestReportCounts:
    """Report counts never include excluded paths."""

    def test_counts_reflect_filtered_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``files_analyzed`` and remediation counts use the filtered set (AC6)."""
        from refactor.scripts import refactor as refactor_module

        monkeypatch.setattr(
            refactor_module,
            "get_changed_files",
            lambda parent_branch="dev": [
                {"status": "M", "file": "node_modules/dep/index.js"},
                {"status": "M", "file": "src/app.py"},
            ],
        )
        monkeypatch.setattr(
            refactor_module,
            "get_untracked_files",
            lambda: ["vendor/lib.py", "src/new.py"],
        )
        monkeypatch.setattr(
            refactor_module,
            "auto_fix_files",
            lambda files, dry_run=False: {"fixes_applied": False, "fixed_findings": []},
        )
        monkeypatch.setattr(refactor_module, "run_smell_detection", lambda **kw: [])

        report = refactor_module.refactor_pipeline(parent_branch="dev")

        assert report["session_files"]["all_files"] == ["src/app.py", "src/new.py"]
        assert report["summary"]["files_analyzed"] == 2
        assert report["summary"]["total_smells"] == 0
        assert report["summary"]["work_items_created"] == 0
        assert report["summary"]["comments_injected"] == 0
