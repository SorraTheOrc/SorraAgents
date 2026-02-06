"""Lightweight wl 'ampa' plugin implementation.

Provides: start, stop, status commands to manage a project-scoped daemon.

This implementation is intentionally small and dependency-light so it can be
used in tests and as a reference implementation for the work item. It stores
pid and log files under .worklog/ampa/<name>.* and supports --cmd, WL_AMPA_CMD
and simple project-config fallbacks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional
import shlex


def find_project_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(100):
        if (
            (cur / "worklog.json").exists()
            or (cur / ".worklog").exists()
            or (cur / ".git").exists()
        ):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise FileNotFoundError("project root not found (worklog.json, .worklog or .git)")


def resolve_command(cli_cmd: Optional[str], project_root: Path) -> Optional[List[str]]:
    # 1. CLI
    if cli_cmd:
        return shlex.split(cli_cmd)
    # 2. env
    env_cmd = os.getenv("WL_AMPA_CMD")
    if env_cmd:
        return shlex.split(env_cmd)
    # 3. project config: worklog.json then package.json then scripts
    wl = project_root / "worklog.json"
    if wl.exists():
        try:
            data = json.loads(wl.read_text())
            if isinstance(data, dict) and "ampa" in data:
                val = data["ampa"]
                if isinstance(val, str):
                    return shlex.split(val)
                if isinstance(val, list):
                    # assume list of args
                    return val
        except Exception:
            pass
    pkg = project_root / "package.json"
    if pkg.exists():
        try:
            pj = json.loads(pkg.read_text())
            scripts = pj.get("scripts", {})
            if "ampa" in scripts:
                return shlex.split(scripts["ampa"])
        except Exception:
            pass
    # 4. fallback executables
    for candidate in (
        project_root / "scripts" / "ampa",
        project_root / "scripts" / "daemon",
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return None


def ensure_dirs(project_root: Path, name: str) -> Path:
    base = project_root / ".worklog" / "ampa" / name
    base.mkdir(parents=True, exist_ok=True)
    return base


def pid_path(project_root: Path, name: str) -> Path:
    return ensure_dirs(project_root, name) / f"{name}.pid"


def log_path(project_root: Path, name: str) -> Path:
    return ensure_dirs(project_root, name) / f"{name}.log"


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start(
    project_root: Path, cmd: List[str], name: str = "default", foreground: bool = False
) -> int:
    ppath = pid_path(project_root, name)
    lpath = log_path(project_root, name)
    if ppath.exists():
        try:
            pid = int(ppath.read_text())
            if is_running(pid):
                print(f"Already running (pid={pid})")
                return 0
        except Exception:
            pass
    if foreground:
        # Run in foreground, stream stdout/stderr
        proc = subprocess.Popen(cmd, cwd=str(project_root))
        try:
            proc.wait()
            return proc.returncode or 0
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            return proc.returncode or 0
    # Detached
    f = open(lpath, "ab")
    # On Windows, close_fds semantics differ; keep simple cross-platform approach
    # Start a new session so the daemon is the leader of its process group.
    # This makes it easier to signal the whole group when stopping.
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=f,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    ppath.write_text(str(proc.pid))
    print(f"Started {name} pid={proc.pid} log={lpath}")
    return 0


def stop(project_root: Path, name: str = "default", timeout: int = 10) -> int:
    ppath = pid_path(project_root, name)
    if not ppath.exists():
        print("Not running (no pid file)")
        return 0
    try:
        pid = int(ppath.read_text())
    except Exception:
        ppath.unlink(missing_ok=True)
        print("Stale pid file removed")
        return 0
    if not is_running(pid):
        ppath.unlink(missing_ok=True)
        print("Not running (stale pid file cleared)")
        return 0
    try:
        # Try to terminate the whole process group first.
        try:
            os.killpg(pid, signal.SIGTERM)
        except AttributeError:
            # os.killpg not available on Windows; fall back to os.kill
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    # wait
    for _ in range(timeout * 10):
        if not is_running(pid):
            break
        time.sleep(0.1)
    if is_running(pid):
        try:
            try:
                os.killpg(pid, signal.SIGKILL)
            except AttributeError:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    if not is_running(pid):
        ppath.unlink(missing_ok=True)
        print(f"Stopped pid={pid}")
        return 0
    print(f"Failed to stop pid={pid}")
    return 1


def status(project_root: Path, name: str = "default") -> int:
    ppath = pid_path(project_root, name)
    lpath = log_path(project_root, name)
    if not ppath.exists():
        print("stopped")
        return 3
    try:
        pid = int(ppath.read_text())
    except Exception:
        ppath.unlink(missing_ok=True)
        print("stopped (cleared corrupt pid file)")
        return 3
    if is_running(pid):
        try:
            # uptime estimation via /proc not portable; show pid only
            print(f"running pid={pid} log={lpath}")
        except Exception:
            print(f"running pid={pid}")
        return 0
    else:
        ppath.unlink(missing_ok=True)
        print("stopped (stale pid file removed)")
        return 3


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(prog="wl ampa")
    sub = parser.add_subparsers(dest="subcmd")

    p_start = sub.add_parser("start")
    p_start.add_argument("--cmd", dest="cmd", help="Command to run (overrides config)")
    p_start.add_argument("--name", default="default")
    p_start.add_argument("--foreground", action="store_true")

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--name", default="default")

    p_status = sub.add_parser("status")
    p_status.add_argument("--name", default="default")

    ns = parser.parse_args(argv)
    try:
        cwd = Path.cwd()
        root = find_project_root(cwd)
    except FileNotFoundError as e:
        print(str(e))
        return 2

    if ns.subcmd == "start":
        cli_cmd = getattr(ns, "cmd", None)
        cmd = resolve_command(cli_cmd, root)
        if cmd is None:
            print(
                "No command resolved. Set --cmd, WL_AMPA_CMD or configure worklog.json/package.json/scripts."
            )
            return 2
        return start(root, cmd, name=ns.name, foreground=ns.foreground)
    if ns.subcmd == "stop":
        return stop(root, name=ns.name)
    if ns.subcmd == "status":
        return status(root, name=ns.name)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
