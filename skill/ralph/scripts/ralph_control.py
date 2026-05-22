#!/usr/bin/env python3
"""Ralph background launcher and status inspector.

This module keeps the foreground implement→audit loop in ``ralph_loop.py``
and adds a lightweight supervisor layer that can:

1. launch the loop in the background under ``nohup``
2. persist the current runtime context for later inspection
3. report live or final status snapshots without requiring the original
   work-item id to be supplied again
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("ralph")


@dataclass
class RalphRuntimeContext:
    target_id: str
    pid: int
    log_path: Path
    state_path: Path
    status_cursor: int = 0
    last_counts: dict[str, int] = field(default_factory=dict)
    last_active_task: str | None = None
    last_recent_activity: list[str] = field(default_factory=list)
    exit_code: int | None = None
    launched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "pid": self.pid,
            "log_path": str(self.log_path),
            "state_path": str(self.state_path),
            "status_cursor": self.status_cursor,
            "last_counts": self.last_counts,
            "last_active_task": self.last_active_task,
            "last_recent_activity": self.last_recent_activity,
            "exit_code": self.exit_code,
            "launched_at": self.launched_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, object], state_path: Path) -> "RalphRuntimeContext":
        return cls(
            target_id=str(payload.get("target_id") or ""),
            pid=int(payload.get("pid") or 0),
            log_path=Path(str(payload.get("log_path") or "")),
            state_path=state_path,
            status_cursor=int(payload.get("status_cursor") or 0),
            last_counts={str(k): int(v) for k, v in dict(payload.get("last_counts") or {}).items()},
            last_active_task=(str(payload["last_active_task"]) if payload.get("last_active_task") else None),
            last_recent_activity=[str(line) for line in list(payload.get("last_recent_activity") or [])],
            exit_code=(int(payload["exit_code"]) if payload.get("exit_code") is not None else None),
            launched_at=str(payload.get("launched_at") or datetime.now(timezone.utc).isoformat()),
        )


class RalphStatusError(RuntimeError):
    pass


_WORK_ITEM_ID = re.compile(r"^[A-Z][A-Z0-9]+-[A-Z0-9][A-Z0-9._-]*$")


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


Runner = Callable[[Sequence[str]], object]
PidChecker = Callable[[int], bool]


def _runtime_dir(cwd: Path | str | None = None) -> Path:
    base = Path(cwd) if cwd is not None else Path.cwd()
    if base.name == "ralph" and base.parent.name == ".worklog":
        return base
    if (base / "current.json").exists():
        return base
    return base / ".worklog" / "ralph"


def _state_path(runtime_dir: Path) -> Path:
    return runtime_dir / "current.json"


def _log_path(runtime_dir: Path, target_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", target_id).strip("._-") or "ralph"
    return runtime_dir / f"{safe}.log"


def _load_context(state_path: Path) -> RalphRuntimeContext:
    if not state_path.exists():
        raise RalphStatusError(f"No Ralph runtime context found at {state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RalphStatusError(f"Invalid Ralph runtime context in {state_path}")
    return RalphRuntimeContext.from_json(payload, state_path)


def _save_context(context: RalphRuntimeContext) -> None:
    context.state_path.parent.mkdir(parents=True, exist_ok=True)
    context.state_path.write_text(json.dumps(context.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_json_argument(args: list[str]) -> list[str]:
    if "--json" in args:
        return args
    return [*args, "--json"]


def _build_launch_command(target_args: Sequence[str], runtime_dir: Path, log_path: Path) -> list[str]:
    script_path = Path(__file__).resolve().with_name("ralph_loop.py")
    launch_args = _ensure_json_argument(list(target_args))
    cmd = ["nohup", sys.executable, "-u", str(script_path), *launch_args]
    logger.info(
        "ralph.launch.cmd cmd=%s log_path=%s",
        shlex.join(cmd),
        log_path,
        extra={"cmd": shlex.join(cmd), "argv": cmd, "log_path": str(log_path)},
    )
    return cmd


def launch_background(
    target_args: Sequence[str],
    cwd: Path | str | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> RalphRuntimeContext:
    """Launch Ralph in the background under nohup.

    The returned context records the runtime files used for later status
    inspection. The first positional argument is treated as the work-item id.
    """
    if not target_args:
        raise RalphStatusError("launch_background requires a target work-item id")

    runtime_dir = _runtime_dir(cwd)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_id = str(target_args[0])
    log_path = _log_path(runtime_dir, target_id)
    state_path = _state_path(runtime_dir)

    context = RalphRuntimeContext(
        target_id=target_id,
        pid=0,
        log_path=log_path,
        state_path=state_path,
    )
    _save_context(context)

    cmd = _build_launch_command(target_args, runtime_dir, log_path)
    log_handle = log_path.open("a", encoding="utf-8")
    try:
        proc = popen(
            cmd,
            cwd=str(Path(cwd) if cwd is not None else Path.cwd()),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    context.pid = int(getattr(proc, "pid", 0) or 0)
    _save_context(context)
    return context


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_work_item_id(candidate: str | None) -> bool:
    return bool(candidate and _WORK_ITEM_ID.match(candidate))


def _extract_active_task(lines: Sequence[str]) -> str | None:
    for pattern in (
        r"child_focus\s+parent=\S+\s+child=(\S+)",
        r"(?:^|\s)active_task=(\S+)",
        r"(?:^|\s)child=(\S+)",
    ):
        for line in reversed(lines):
            if not line:
                continue
            match = re.search(pattern, line)
            if match and _is_work_item_id(match.group(1)):
                return match.group(1)
    for line in reversed(lines):
        if not line:
            continue
        match = re.search(r"(?:^|\s)target=(\S+)", line)
        if match and _is_work_item_id(match.group(1)):
            return match.group(1)
    return None


def _count_scope_statuses(
    target_id: str,
    runner: Runner | None = None,
) -> dict[str, int]:
    def _runner(cmd: Sequence[str]):
        if runner is not None:
            return runner(cmd)
        return subprocess.run(cmd, check=False, text=True, capture_output=True)

    def _load_item(item_id: str) -> dict:
        cmd = ["wl", "show", item_id, "--json", "--children"]
        proc = _runner(cmd)
        if getattr(proc, "returncode", 0) != 0:
            raise RalphStatusError(f"Worklog command failed ({' '.join(cmd)}): {(getattr(proc, 'stderr', '') or '').strip()}")
        try:
            data = json.loads(getattr(proc, "stdout", "") or "{}")
        except json.JSONDecodeError as exc:
            raise RalphStatusError(f"Invalid JSON from {' '.join(cmd)}: {exc}") from exc
        if not isinstance(data, dict) or data.get("success") is False:
            raise RalphStatusError(f"Worklog command failed ({' '.join(cmd)}): {data.get('error', 'unknown error')}")
        return data

    counts: dict[str, int] = {}
    seen: set[str] = set()
    queue: list[str] = [target_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        data = _load_item(current)
        work_item = data.get("workItem", {}) if isinstance(data, dict) else {}
        status = str(work_item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        for child in data.get("children", []) if isinstance(data, dict) else []:
            if isinstance(child, dict):
                child_id = child.get("id")
                if child_id and child_id not in seen:
                    queue.append(str(child_id))
    return counts


_JSON_RESULT_LINE = re.compile(r"^\s*\{\s*\"status\"\s*:\s*\"(?P<status>[^\"]+)\".*\}\s*$")


def _extract_final_result(log_text: str) -> dict[str, object] | None:
    for line in reversed(log_text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    # Final result may be pretty-printed JSON; fall back to a lightweight search.
    match = _JSON_RESULT_LINE.search(log_text.splitlines()[-1] if log_text.splitlines() else "")
    if match:
        return {"status": match.group("status")}
    return None


def _exit_code_from_result(result: dict[str, object] | None) -> int | None:
    if not result:
        return None
    status = str(result.get("status") or "")
    if status == "success":
        return 0
    if status == "cancelled":
        return 3
    if status == "max_attempts":
        return 4
    if status:
        return 2
    return None


def inspect_status(
    runtime_dir: Path | str | None = None,
    wl_runner: Runner | None = None,
    pid_is_alive: PidChecker | None = None,
) -> dict[str, object]:
    runtime = _runtime_dir(runtime_dir)
    context = _load_context(_state_path(runtime))

    checker = pid_is_alive or _pid_is_alive
    running = checker(context.pid)
    log_text = context.log_path.read_text(encoding="utf-8") if context.log_path.exists() else ""
    lines = log_text.splitlines()
    recent_lines = lines[context.status_cursor :]
    active_task = _extract_active_task(recent_lines) or _extract_active_task(lines)
    counts = _count_scope_statuses(context.target_id, runner=wl_runner)
    deltas = {key: counts.get(key, 0) - context.last_counts.get(key, 0) for key in sorted(set(counts) | set(context.last_counts))}

    if recent_lines:
        recent_activity = [*context.last_recent_activity, *recent_lines] if context.last_recent_activity else list(recent_lines)
        context.last_recent_activity = list(recent_lines)
    else:
        recent_activity = ["No new log activity since the last check"]

    result = _extract_final_result(log_text)
    exit_code = context.exit_code
    if not running:
        exit_code = _exit_code_from_result(result)
        if exit_code is None:
            exit_file = runtime / "current.exitcode"
            if exit_file.exists():
                try:
                    exit_code = int(exit_file.read_text(encoding="utf-8").strip())
                except ValueError:
                    exit_code = None

    context.status_cursor = len(lines)
    context.last_counts = counts
    context.last_active_task = active_task
    context.exit_code = exit_code
    _save_context(context)

    if not result and not running:
        result = {"status": "stopped", "exit_code": exit_code, "summary": "Ralph background run finished"}

    snapshot = {
        "state": "running" if running else "stopped",
        "pid": context.pid,
        "target_id": context.target_id,
        "log_path": str(context.log_path),
        "active_task": active_task,
        "recent_activity": recent_activity,
        "status_counts": counts,
        "status_deltas": deltas,
        "exit_code": exit_code,
        "cursor": context.status_cursor,
        "final_summary": result,
    }
    return snapshot


def format_status(snapshot: dict[str, object]) -> str:
    lines = [
        f"ralph: {snapshot.get('state')} pid={snapshot.get('pid')} target={snapshot.get('target_id')}",
    ]
    active = snapshot.get("active_task")
    if active:
        lines.append(f"active task: {active}")
    if snapshot.get("recent_activity"):
        lines.append("recent activity:")
        for line in snapshot["recent_activity"]:
            lines.append(f"  - {line}")
    counts = snapshot.get("status_counts") or {}
    deltas = snapshot.get("status_deltas") or {}
    if isinstance(counts, dict) and counts:
        summary = ", ".join(
            f"{key}={counts[key]} ({deltas.get(key, 0):+d})" for key in sorted(counts)
        )
        lines.append(f"worklog status counts: {summary}")
    if snapshot.get("exit_code") is not None:
        lines.append(f"exit code: {snapshot['exit_code']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch or inspect Ralph background runs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="Launch Ralph in the background under nohup")
    launch.add_argument("work_item_id", help="Target Worklog work item id")
    launch.add_argument("args", nargs=argparse.REMAINDER, help="Additional Ralph loop arguments")
    launch.add_argument("--cwd", default=None, help="Base directory for runtime files (defaults to current directory)")

    status = subparsers.add_parser("status", help="Inspect the current Ralph runtime context")
    status.add_argument("--cwd", default=None, help="Base directory for runtime files (defaults to current directory)")
    status.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output")
    status.add_argument("--wl-bin", default="wl", help="Worklog binary to use when summarising status counts")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "launch":
            context = launch_background([args.work_item_id, *args.args], cwd=args.cwd)
            print(json.dumps(context.to_json(), ensure_ascii=False))
            return 0

        snapshot = inspect_status(runtime_dir=args.cwd, wl_runner=None)
        if args.json:
            print(json.dumps(snapshot, ensure_ascii=False))
        else:
            print(format_status(snapshot))
        return 0 if snapshot.get("state") == "running" else 1
    except RalphStatusError as exc:
        payload = {"error": str(exc)}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ralph: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
