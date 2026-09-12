#!/usr/bin/env python3
"""Tests: generate_standup.py default output path (SA-0MTVB0NAQ007UKID).

Covers:
  - When --output-path is omitted, the report writes to ./standups/YYYY_MM_DD.md
    relative to the project root (worklog parent when --worklog-dir / WL_WORKLOG_DIR
    is set, otherwise cwd).
  - The standups directory is created if it does not exist.
  - When --output-path IS supplied, it writes to that explicit path only.
  - Default uses YYYY_MM_DD of the generation date (local, zero-padded).
  - --json content is written to the same default/override path when output is file-based.

Regenerates ACs:
  1. default dated file (created, under correct project root)
  2. override via --output-path (explicit path only, no default write)
"""
from __future__ import annotations

import builtins
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

_MODULE_NAME = "generate_standup"


def _reload_standup(env_value=None):
    """(Re)import generate_standup with optional WL_WORKLOG_DIR env; returns module."""
    if env_value is not None:
        import os as _os

        _os.environ["WL_WORKLOG_DIR"] = env_value
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]
    # Direct import path
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _cleanup_standup_env():
    """Pop WL_WORKLOG_DIR and purge generate_standup modules so subsequent tests start clean."""
    import os

    os.environ.pop("WL_WORKLOG_DIR", None)
    for k in list(sys.modules):
        if "generate_standup" in k:
            del sys.modules[k]


def _fake_run_wl_empty_git_fail(mod, wl_json=None):
    """Patch mod.subprocess.run so every wl call returns empty data and git fails."""
    wl_json = wl_json or json.dumps({"results": [], "workItems": []})

    def _fake(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "wl":
            return subprocess.CompletedProcess(cmd, 0, wl_json, "")
        if isinstance(cmd, list) and cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 1, "", "not a git repo")
        cap = " ".join(cmd) if isinstance(cmd, list) else cmd
        if isinstance(cmd, str) and "wl " in cap:
            return subprocess.CompletedProcess(cmd, 0, wl_json, "")
        return subprocess.CompletedProcess(cmd if isinstance(cmd, list) else [cmd], 1, "", "")

    return mock.patch.object(mod.subprocess, "run", side_effect=_fake)


class TestDefaultOutputPath:
    """Default: writes markdown (and --json) to ./standups/YYYY_MM_DD.md."""

    def test_default_path_writes_markdown_and_creates_dir(self, tmp_path):
        """No --output-path → creates standups/ and writes YYYY_MM_DD.md with the report."""
        repo_root = tmp_path / "proj"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)
        standups_dir = repo_root / "standups"
        assert not standups_dir.exists()

        mod = _reload_standup()
        try:
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                        rc = mod.main()
                    assert rc == 0, "main() should succeed"
                today = datetime.now().strftime("%Y_%m_%d")
                expected = repo_root / "standups" / f"{today}.md"
                assert standups_dir.is_dir(), "standups/ directory should be created"
                assert expected.is_file(), f"Expected report at {expected}"
                body = expected.read_text(encoding="utf-8")
                assert "Standup Report" in body
        finally:
            _cleanup_standup_env()

    def test_default_path_format_is_YYYY_MM_DD_zero_padded(self, tmp_path):
        """Default filename is exactly YYYY_MM_DD.md (zero-padded, 4_2_2 digits)."""
        repo_root = tmp_path / "proj2"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)

        mod = _reload_standup()
        try:
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                        rc = mod.main()
                    assert rc == 0
                today = datetime.now().strftime("%Y_%m_%d")
                expected = repo_root / "standups" / f"{today}.md"
                assert expected.is_file()
                # Verify naming shape: YYYY_MM_DD.md (zero-padded)
                import re
                name = expected.name
                assert len(name) == len("0000_00_00.md")
                assert re.match(r"^\d{4}_\d{2}_\d{2}\.md$", name), f"Bad name {name}"
                y, m, dm = name[:4], name[5:7], name[8:10]
                assert y.isdigit() and m.isdigit() and dm.isdigit()
                assert name == f"{today}.md"
        finally:
            _cleanup_standup_env()

    def test_default_json_writes_json_to_standups_dated_file(self, tmp_path):
        """--json with no --output-path also writes to the default dated file (as JSON)."""
        repo_root = tmp_path / "proj3"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)

        mod = _reload_standup()
        try:
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--json", "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                        rc = mod.main()
                    assert rc == 0
                today = datetime.now().strftime("%Y_%m_%d")
                expected = repo_root / "standups" / f"{today}.md"
                assert expected.is_file()
                raw = expected.read_text(encoding="utf-8")
                # Should be JSON (not markdown) when --json is used
                data = json.loads(raw)
                assert isinstance(data, dict)
                assert "herdr_count" in data or "window" in data
        finally:
            _cleanup_standup_env()

    def test_default_does_not_write_to_stdout_file(self, tmp_path):
        """Default path writes to a file; stdout is not the delivery channel."""
        repo_root = tmp_path / "proj4"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)

        mod = _reload_standup()
        try:
            printed_stdout = []
            printed_stderr = []

            def capture_print(*args, **kwargs):
                text = " ".join(str(a) for a in args)
                stream = kwargs.get("file", sys.stdout)
                if stream is sys.stderr:
                    printed_stderr.append(text)
                else:
                    printed_stdout.append(text)

            with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=capture_print):
                        rc = mod.main()
                    assert rc == 0
                # Report is NOT printed to stdout; it is written to the file and
                # a short confirmation goes to stderr.
                assert "".join(printed_stdout) == "", f"Expected no stdout, got: {printed_stdout}"
                assert "Report written to" in " ".join(printed_stderr)
                today = datetime.now().strftime("%Y_%m_%d")
                assert (repo_root / "standups" / f"{today}.md").is_file()
        finally:
            _cleanup_standup_env()


class TestOverridePath:
    """--output-path <path> overrides the default dated file."""

    def test_explicit_output_path_used_instead_of_default(self, tmp_path):
        """--output-path <path> writes to that explicit path only; default not written."""
        repo_root = tmp_path / "proj_override"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)
        explicit_path = str(tmp_path / "my_custom_report.md")

        mod = _reload_standup()
        try:
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--output-path", explicit_path, "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    written = {}
                    orig_open = builtins.open

                    def _open(path, *a, **kw):
                        written[str(path)] = True
                        return orig_open(path, *a, **kw)

                    with mock.patch("builtins.open", _open):
                        with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                            rc = mod.main()
                    assert rc == 0
                    assert explicit_path in written
                    today = datetime.now().strftime("%Y_%m_%d")
                    default_hits = [f for f in written if "standups" in f and today in f]
                    assert default_hits == [], f"Should not write default dated file, but wrote {default_hits}"
                    assert Path(explicit_path).is_file()
        finally:
            _cleanup_standup_env()

    def test_explicit_json_output_path_respected(self, tmp_path):
        """--json + --output-path <path> writes JSON to that explicit path."""
        repo_root = tmp_path / "proj_override_json"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)
        explicit_path = str(tmp_path / "explicit.json")

        mod = _reload_standup()
        try:
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--json", "--output-path", explicit_path, "--worklog-dir", str(worklog)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                        rc = mod.main()
                    assert rc == 0
                    assert Path(explicit_path).is_file()
                    data = json.loads(Path(explicit_path).read_text(encoding="utf-8"))
                    assert "herdr_count" in data or "yesterday" in data
        finally:
            _cleanup_standup_env()


class TestWorklogDirPathResolution:
    """Default standups/ location tracks the worklog's project root."""

    def test_default_root_is_worklog_parent_when_set(self, tmp_path):
        """WL_WORKLOG_DIR / --worklog-dir → default is <worklog-parent>/standups/YYYY_MM_DD.md."""
        project_b = tmp_path / "project_b"
        worklog_b = project_b / ".worklog"
        worklog_b.mkdir(parents=True)

        mod = _reload_standup(env_value=str(worklog_b))
        try:
            today = datetime.now().strftime("%Y_%m_%d")
            project_root = str(Path(mod.WORKLOG_DIR).resolve().parent)
            computed = Path(project_root) / "standups" / f"{today}.md"
            assert str(computed) == str((project_b / "standups" / f"{today}.md").resolve())
        finally:
            _cleanup_standup_env()

    def test_explicit_worklog_dir_overrides_env_for_default_location(self, tmp_path):
        """--worklog-dir on argv takes precedence over WL_WORKLOG_DIR for locating standups/."""
        project_env = tmp_path / "from_env"
        (project_env / ".worklog").mkdir(parents=True)
        project_arg = tmp_path / "from_arg"
        worklog_arg = project_arg / ".worklog"
        worklog_arg.mkdir(parents=True)

        # Env points to from_env; argv will point to from_arg
        mod = _reload_standup(env_value=str(project_env / ".worklog"))
        try:
            # argv sets --worklog-dir to worklog_arg, so main() should update WORKLOG_DIR to it
            with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(worklog_arg)]):
                with _fake_run_wl_empty_git_fail(mod):
                    with mock.patch("builtins.print", side_effect=lambda *a, **kw: None):
                        rc = mod.main()
                    assert rc == 0
                    assert mod.WORKLOG_DIR is not None
                    today = datetime.now().strftime("%Y_%m_%d")
                    expected = project_arg / "standups" / f"{today}.md"
                    assert expected.is_file(), f"Expected {expected}"
                    # Must NOT write to the env-based project
                    not_expected = project_env / "standups" / f"{today}.md"
                    assert not not_expected.exists(), f"Should not write to env project {not_expected}"
        finally:
            _cleanup_standup_env()
