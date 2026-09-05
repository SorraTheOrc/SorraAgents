#!/usr/bin/env python3
"""Tests: generate_standup.py resolves worklog cwd-independently via --worklog-dir / WL_WORKLOG_DIR.

Covers SA-0MTOMCML900254CI:
  - wl invocations (wl next/list/dep) inject --worklog-dir when set
  - git log/show invocations target the worklog-derived project root when set
  - bare --worklog-dir <project-root> normalizes to <root>/.worklog
  - WL_WORKLOG_DIR env alone is sufficient; default w/ no flag is cwd-based
"""  # noqa: EXE001
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))


def _reload_standup(env_value=None):
    """(Re)import generate_standup with optional WL_WORKLOG_DIR env."""
    if env_value is not None:
        import os as _os
        _os.environ["WL_WORKLOG_DIR"] = env_value
        # Force reimport to pick up new env
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]
    # Direct import path
    spec_path = REPO_ROOT / "skill" / "standup" / "scripts" / "generate_standup.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("generate_standup", str(spec_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class TestWlFlagInjection:
    def test_wl_command_injects_worklog_dir_from_env(self, tmp_path):
        mod = _reload_standup(env_value=str(tmp_path / ".worklog"))
        decorated, use_shell = mod._inject_worklog_dir("wl next -n 5 --include-in-progress --json")
        assert isinstance(decorated, list)
        assert decorated[0] == "wl"
        assert decorated[1] == "--worklog-dir"
        assert decorated[2] == str(tmp_path / ".worklog")
        assert use_shell is False
        # Clean up env
        import os
        del os.environ["WL_WORKLOG_DIR"]
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]

    def test_non_wl_command_not_decorated(self, tmp_path):
        mod = _reload_standup(env_value=str(tmp_path / ".worklog"))
        decorated, use_shell = mod._inject_worklog_dir('git log --all --before="2026-01-01" --format="%H"')
        assert decorated == 'git log --all --before="2026-01-01" --format="%H"'
        assert use_shell is True
        import os
        del os.environ["WL_WORKLOG_DIR"]
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]

    def test_no_flag_when_env_absent(self, tmp_path):
        import os
        os.environ.pop("WL_WORKLOG_DIR", None)
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]
        mod = _reload_standup()
        decorated, use_shell = mod._inject_worklog_dir("wl list --json")
        assert decorated == "wl list --json"
        assert use_shell is True

    def test_fetch_json_calls_run_cmd_withinjection(self, tmp_path):
        mod = _reload_standup(env_value=str(tmp_path / ".worklog"))
        with mock.patch.object(mod.subprocess, "run") as m:
            m.return_value = subprocess.CompletedProcess(
                ["wl"], 0, '{"results":[]}', ""
            )
            data, err = mod.fetch_json("wl next -n 1 --include-in-progress --json")
            # Should have been called with list including --worklog-dir
            assert m.called
            cmd = m.call_args[0][0]
            assert isinstance(cmd, list)
            assert cmd[0] == "wl"
            assert cmd[1] == "--worklog-dir"
            assert err is None
        import os
        del os.environ["WL_WORKLOG_DIR"]
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]


class TestGitAnchoring:
    def test_git_dir_flag_derived_from_worklog(self, tmp_path):
        mod = _reload_standup(env_value=str(tmp_path / ".worklog"))
        flags = mod._git_dir_flag()
        assert flags == ["-C", str(Path(str(tmp_path / ".worklog")).resolve().parent)]
        import os
        del os.environ["WL_WORKLOG_DIR"]
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]

    def test_run_git_targets_worklog_root(self, tmp_path):
        mod = _reload_standup(env_value=str(tmp_path / "proj" / ".worklog"))
        with mock.patch.object(mod.subprocess, "run") as m:
            m.return_value = subprocess.CompletedProcess(
                ["git", "-C", str(tmp_path / "proj"), "log"], 0, "abc123\n", ""
            )
            ok, stdout, stderr = mod._run_git(["log", "--all", "--format=%H"])
            assert ok is True
            assert stdout == "abc123"
            cmd = m.call_args[0][0]
            assert cmd[0] == "git"
            assert "-C" in cmd
            assert str(tmp_path / "proj") in cmd
        import os
        del os.environ["WL_WORKLOG_DIR"]
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]

    def test_run_git_no_flag_without_env(self):
        import os
        os.environ.pop("WL_WORKLOG_DIR", None)
        for k in list(sys.modules):
            if "generate_standup" in k:
                del sys.modules[k]
        mod = _reload_standup()
        with mock.patch.object(mod.subprocess, "run") as m:
            m.return_value = subprocess.CompletedProcess(["git", "log"], 0, "abc\n", "")
            ok, _, _ = mod._run_git(["log", "--format=%H"])
            assert ok is True
            cmd = m.call_args[0][0]
            assert "-C" not in cmd


class TestWorklogDirNormalization:
    def test_worklog_dir_bare_root_normalizes(self, tmp_path):
        """A bare project root argument is normalized to <root>/.worklog."""
        import json

        repo_root = tmp_path / "proj"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)
        (worklog / "config.yaml").write_text("prefix: XX\n", encoding="utf-8")

        # Mock subprocess for wl call + git
        mod = _reload_standup()
        # Simulate running main with --worklog-dir <repo_root> (bare, not .worklog)
        with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(repo_root), "--json"]):
            with mock.patch.object(mod.subprocess, "run") as m:
                def fake_run(cmd, *args, **kwargs):
                    cap = " ".join(cmd) if isinstance(cmd, list) else cmd
                    if isinstance(cmd, list) and cmd[0] == "wl":
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "workItems": []}), "")
                    if isinstance(cmd, list) and cmd[0] == "git":
                        return subprocess.CompletedProcess(cmd, 1, "", "not a git repo")
                    if isinstance(cmd, str) and "wl " in cap:
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "workItems": []}), "")
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                m.side_effect = fake_run
                with mock.patch("builtins.print"):
                    rc = mod.main()
                # Regardless of rc, WORKLOG_DIR should be normalized
                assert mod.WORKLOG_DIR is not None
                assert Path(mod.WORKLOG_DIR).name == ".worklog"
                assert Path(mod.WORKLOG_DIR).resolve() == worklog.resolve()
            # No extra cleanup needed; WL_WORKLOG_DIR not set

    def test_worklog_dir_explicit_dotworklog_preserved(self, tmp_path):
        import json

        repo_root = tmp_path / "proj2"
        worklog = repo_root / ".worklog"
        worklog.mkdir(parents=True)
        (worklog / "config.yaml").write_text("prefix: YY\n", encoding="utf-8")
        mod = _reload_standup()
        with mock.patch.object(sys, "argv", ["generate_standup.py", "--worklog-dir", str(worklog), "--json"]):
            with mock.patch.object(mod.subprocess, "run") as m:
                def fake_run(cmd, *args, **kwargs):
                    if isinstance(cmd, list) and cmd[0] == "wl":
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "workItems": []}), "")
                    if isinstance(cmd, list) and cmd[0] == "git":
                        return subprocess.CompletedProcess(cmd, 1, "", "")
                    cap = " ".join(cmd) if isinstance(cmd, list) else cmd
                    if isinstance(cmd, str) and "wl " in cap:
                        return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "workItems": []}), "")
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                m.side_effect = fake_run
                with mock.patch("builtins.print"):
                    rc = mod.main()
                assert Path(mod.WORKLOG_DIR).resolve() == worklog.resolve()
