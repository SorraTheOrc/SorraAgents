"""Regression tests for cross-repo worklog resolution in implement.py.

Contract (OSL-0MUI78ROQ005B37N):

``implement.py``'s local ``wl`` helpers (``wl_show``, ``wl_show_children``,
``wl_dep_blockers``, ``wl_add_comment``, ``_is_work_item_open``) must resolve
the worklog store by work-item **prefix** (prefix-to-sibling scan), not by the
current working directory. Otherwise a cross-repo item such as ``OSL-*`` cannot
be fetched when ``implement.py`` runs from the SorraAgents checkout (which has
its own initialised ``.worklog``), and ``implement.py start/parent OSL-...``
aborts with "Work item not found".

The tests build a fake sibling project whose ``.worklog/config.yaml`` declares
``prefix: OSL``, point ``SIBLING_SCAN_ROOT`` at it, and assert each helper
passes ``--worklog-dir <sibling>/.worklog`` to ``wl``.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IMPLEMENT_PY = _REPO_ROOT / "skill" / "implement" / "scripts" / "implement.py"


def _module_loader(cwd: Path) -> str:
    """Python preamble that loads implement.py as ``mod`` with cwd=*cwd*."""
    return textwrap.dedent(f"""\
        import json
        import os
        import sys
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        os.chdir({str(cwd)!r})
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "implement_under_test", {str(_IMPLEMENT_PY)!r},
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "implement_scripts"
        sys.modules["implement_under_test"] = mod
        spec.loader.exec_module(mod)
    """)


def _run_in_subprocess(tmp_path: Path, runner_source: str) -> subprocess.CompletedProcess:
    runner_path = tmp_path / "_cross_repo_runner.py"
    runner_path.write_text(runner_source)
    return subprocess.run(  # noqa: PLW1510
        [sys.executable, str(runner_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _make_sibling_project(tmp_path: Path) -> Path:
    """Create a fake sibling project declaring ``prefix: OSL``.

    Returns:
        The ``SIBLING_SCAN_ROOT`` directory containing the sibling.
    """
    scan_root = tmp_path / "projects"
    sibling = scan_root / "open_source_llm"
    (sibling / ".worklog").mkdir(parents=True)
    (sibling / ".worklog" / "config.yaml").write_text("prefix: OSL\n")
    return scan_root


def _capture_runner(tmp_path: Path, scan_root: Path, operation: str) -> str:
    """Build a runner that points the scan root at the fixture and captures
    the ``wl`` command produced by *operation*."""
    cwd = tmp_path / "current_repo"
    cwd.mkdir(exist_ok=True)
    return _module_loader(cwd) + textwrap.dedent(f"""\
        import shared.status_lifecycle as _sl
        _sl.SIBLING_SCAN_ROOT = __import__("pathlib").Path({str(scan_root)!r})

        captured = []

        def _fake_run_cmd(cmd, **kwargs):
            captured.append(list(cmd))
            joined = " ".join(cmd)
            if "dep list" in joined:
                stdout = json.dumps({{"outbound": []}})
            elif "--children" in cmd:
                stdout = json.dumps({{"children": []}})
            elif cmd[0] == "wl" and "comment" in cmd:
                stdout = json.dumps({{"success": True}})
            else:
                stdout = json.dumps({{"workItem": {{"id": "OSL-TEST", "status": "open"}}}})
            import subprocess as _sp
            return _sp.CompletedProcess(cmd, 0, stdout, "")

        mod.run_cmd = _fake_run_cmd

        {operation}

        print("CAPTURED:" + json.dumps(captured))
    """)


def _captured(proc: subprocess.CompletedProcess) -> list[list[str]]:
    assert proc.returncode == 0, proc.stderr
    for line in proc.stdout.splitlines():
        if line.startswith("CAPTURED:"):
            return json.loads(line[len("CAPTURED:"):])
    raise AssertionError(f"No CAPTURED line:\n{proc.stdout}\n{proc.stderr}")


class TestCrossRepoWorklogResolution:
    def test_wl_show_uses_prefix_resolver(self, tmp_path):
        scan_root = _make_sibling_project(tmp_path)
        runner = _capture_runner(
            tmp_path, scan_root, 'mod.wl_show("OSL-TEST")'
        )
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        assert captured == [[
            "wl", "--worklog-dir", str(scan_root / "open_source_llm" / ".worklog"),
            "show", "OSL-TEST", "--json",
        ]]

    def test_wl_show_children_uses_prefix_resolver(self, tmp_path):
        scan_root = _make_sibling_project(tmp_path)
        runner = _capture_runner(
            tmp_path, scan_root, 'mod.wl_show_children("OSL-TEST")'
        )
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        assert captured == [[
            "wl", "--worklog-dir", str(scan_root / "open_source_llm" / ".worklog"),
            "show", "OSL-TEST", "--children", "--json",
        ]]

    def test_wl_dep_blockers_uses_prefix_resolver(self, tmp_path):
        scan_root = _make_sibling_project(tmp_path)
        runner = _capture_runner(
            tmp_path, scan_root, 'mod.wl_dep_blockers("OSL-TEST")'
        )
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        assert captured == [[
            "wl", "--worklog-dir", str(scan_root / "open_source_llm" / ".worklog"),
            "dep", "list", "OSL-TEST", "--json",
        ]]

    def test_wl_add_comment_uses_prefix_resolver(self, tmp_path):
        scan_root = _make_sibling_project(tmp_path)
        runner = _capture_runner(
            tmp_path, scan_root, 'mod.wl_add_comment("OSL-TEST", "hello")'
        )
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        assert captured[0][:4] == [
            "wl", "--worklog-dir",
            str(scan_root / "open_source_llm" / ".worklog"), "comment",
        ]
        assert "OSL-TEST" in captured[0]
        assert "hello" in captured[0]

    def test_is_work_item_open_uses_prefix_resolver(self, tmp_path):
        scan_root = _make_sibling_project(tmp_path)
        runner = _capture_runner(
            tmp_path, scan_root, 'mod._is_work_item_open("OSL-TEST")'
        )
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        assert captured == [[
            "wl", "--worklog-dir", str(scan_root / "open_source_llm" / ".worklog"),
            "show", "OSL-TEST", "--json",
        ]]

    def test_local_repo_still_resolves_without_flag(self, tmp_path):
        """When no sibling matches the prefix, the resolver falls back to the
        cwd chain (an initialized worklog root) and adds no flag."""
        cwd = tmp_path / "current_repo"
        (cwd / ".worklog").mkdir(parents=True)
        (cwd / ".worklog" / "config.yaml").write_text("prefix: SA\n")
        (cwd / ".worklog" / "initialized").write_text("")
        empty_scan_root = tmp_path / "empty_projects"
        empty_scan_root.mkdir()

        runner = _capture_runner(tmp_path, empty_scan_root, 'mod.wl_show("SA-TEST")')
        captured = _captured(_run_in_subprocess(tmp_path, runner))
        # No --worklog-dir needed: wl resolves SA from the cwd chain.
        assert captured == [["wl", "show", "SA-TEST", "--json"]]
