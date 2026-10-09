"""Tests for the non-destructive GIT environment propagation tracer.

Covers the tracer used as evidence for SA-0MUIZSXEY008NGR8: it must detect
repository-override variables in a process environment, parse both
``/proc/<pid>/environ`` (NUL-separated) and ``env`` output (newline-separated),
and never point an override at the live checkout.

The full scrub contract (``scrub_repository_overrides`` etc.) is covered by
the dedicated regression test in ``skill/test/tests/test_git_env_scrub.py``
(child SA-0MV0G9CD1007A0IC); these tests cover the *tracer* only.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "docs" / "dev" / "trace_git_env_propagation.py"


def _load_tracer():
    """Import the tracer module by file location (it is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "trace_git_env_propagation", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


tracer = _load_tracer()


def _write_fake_proc(proc_root: Path, pid: int, comm: str, env: dict[str, str]) -> None:
    """Create a synthetic /proc/<pid>/{comm,environ} entry."""
    entry = proc_root / str(pid)
    entry.mkdir(parents=True)
    (entry / "comm").write_text(f"{comm}\n")
    payload = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
    (entry / "environ").write_bytes(payload)


def test_scan_proc_environ_detects_override_in_matched_process(tmp_path):
    """A matched agent process carrying GIT_DIR is reported."""
    proc_root = tmp_path / "proc"
    _write_fake_proc(
        proc_root,
        101,
        "pi",
        {"PATH": "/usr/bin", "GIT_DIR": "/somewhere/.git"},
    )

    findings = tracer.scan_proc_environ(proc_root)

    assert findings == [
        {"pid": 101, "comm": "pi", "overrides": ["GIT_DIR"]},
    ]


def test_scan_proc_environ_ignores_unmatched_process(tmp_path):
    """An unmatched process with an override is not reported."""
    proc_root = tmp_path / "proc"
    _write_fake_proc(proc_root, 202, "unrelated-daemon", {"GIT_WORK_TREE": "/x"})

    assert tracer.scan_proc_environ(proc_root) == []


def test_scan_proc_environ_returns_empty_for_clean_matched_process(tmp_path):
    """A matched process without overrides produces no finding."""
    proc_root = tmp_path / "proc"
    _write_fake_proc(proc_root, 303, "run-pi-agent", {"PATH": "/usr/bin"})

    assert tracer.scan_proc_environ(proc_root) == []


def test_read_env_dump_parses_newline_separated_output(tmp_path):
    """The ``env`` dump parser handles newline-separated KEY=VALUE lines."""
    dump = tmp_path / "env.txt"
    dump.write_text("PATH=/usr/bin\nGIT_PREFIX=\nEMPTY=\nnot-a-line\n")

    assert tracer._read_env_dump(dump) == {
        "PATH": "/usr/bin",
        "GIT_PREFIX": "",
        "EMPTY": "",
    }


def test_read_environ_parses_nul_separated_bytes(tmp_path):
    """The /proc parser handles NUL-separated KEY=VALUE entries."""
    environ = tmp_path / "environ"
    environ.write_bytes(b"PATH=/usr/bin\0GIT_EXEC_PATH=/usr/lib/git-core\0")

    assert tracer._read_environ(environ) == {
        "PATH": "/usr/bin",
        "GIT_EXEC_PATH": "/usr/lib/git-core",
    }


def test_cli_json_confirms_propagation_against_throwaway_repos():
    """The CLI demonstrates both facts and confines overrides to temp dirs."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    # Fact 1: git sets its hook-subprocess context (the incident signature).
    assert report["hook_git_env"].get("GIT_EXEC_PATH")
    assert report["hook_sets_git_context"] is True
    # Fact 2: forwarding the parent env carries overrides into the child.
    assert report["spawn_forwards_overrides"] is True
    # Safety: the only override the tracer sets points at a throwaway repo,
    # never the live checkout.
    git_dir = report["spawned_child_overrides"]["GIT_DIR"]
    assert "SorraAgents" not in git_dir
    assert not Path(git_dir).resolve().is_relative_to(REPO_ROOT)


def test_capture_fresh_child_env_forwards_repository_override():
    """A fresh child spawned with a forwarded env sees the override.

    This is the controlled-dump contract (SA-0MV0G9BVW008M8AT): forwarding
    the parent environment — the launcher's spawn semantics — carries a
    repository-override variable into the child unchanged.
    """
    observed = tracer.capture_fresh_child_env(
        {"PATH": "/usr/bin:/bin", "GIT_DIR": "/somewhere/.git"}
    )

    assert observed.get("GIT_DIR") == "/somewhere/.git"
    assert tracer.repository_overrides_in_env(observed) == ("GIT_DIR",)


def test_capture_fresh_child_env_is_clean_without_overrides():
    """A fresh child of a clean environment reports no overrides."""
    observed = tracer.capture_fresh_child_env({"PATH": "/usr/bin:/bin"})

    assert tracer.repository_overrides_in_env(observed) == ()


def test_cli_session_env_json_reports_clean_session():
    """The controlled dump exits 0 when the live session is clean.

    Repository-override variables must not be present in the agent/test
    session that runs this probe; a leak fails the check (non-zero exit).
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--session-env", "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    report = json.loads(result.stdout)
    assert report["clean"] is (result.returncode == 0)
    if report["clean"]:
        assert report["session_overrides"] == []
        assert report["fresh_child_overrides"] == []
    else:
        assert report["session_overrides"] or report["fresh_child_overrides"]
