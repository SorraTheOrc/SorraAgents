from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts import ralph_control


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeWlRunner:
    def __init__(self, items: dict[str, dict[str, str]]):
        self.items = items
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["wl", "show", cmd[2]] and "--children" in cmd:
            item = self.items[cmd[2]]
            children = [
                {"id": child_id, "stage": self.items[child_id].get("stage", "")}
                for child_id in item.get("children", [])
            ]
            return Result(stdout=json.dumps({"success": True, "workItem": item, "children": children}))
        if cmd[:2] == ["wl", "show"]:
            item = self.items[cmd[2]]
            return Result(stdout=json.dumps({"success": True, "workItem": item}))
        raise AssertionError(f"Unexpected wl command: {cmd}")


class DummyProc:
    def __init__(self, pid: int = 4321):
        self.pid = pid


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Path:
    runtime = tmp_path / ".worklog" / "ralph"
    runtime.mkdir(parents=True)
    return runtime


def test_launch_background_records_command_and_runtime_context(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProc(pid=4242)

    monkeypatch.setattr(ralph_control.subprocess, "Popen", fake_popen)

    context = ralph_control.launch_background(
        ["SA-123", "--json"],
        cwd=tmp_path,
        popen=fake_popen,
    )

    assert context.pid == 4242
    assert context.target_id == "SA-123"
    assert context.log_path.exists()
    assert context.state_path.exists()
    assert "nohup" in captured["cmd"][0] or "nohup" in " ".join(captured["cmd"])
    assert "ralph_loop.py" in " ".join(captured["cmd"])

    state = json.loads(context.state_path.read_text(encoding="utf-8"))
    assert state["pid"] == 4242
    assert state["target_id"] == "SA-123"
    assert state["log_path"] == str(context.log_path)


def test_status_snapshot_tracks_recent_activity_and_status_deltas(runtime_dir: Path):
    log_path = runtime_dir / "SA-TARGET.log"
    log_path.write_text(
        "INFO ralph.loop.start target=SA-TARGET scope=SA-TARGET,SA-CHILD max_attempts=10\n"
        "INFO ralph.loop.child_focus parent=SA-TARGET child=SA-CHILD\n"
        "INFO ralph.loop.audit.complete target=SA-TARGET attempt=1 ready=False unmet=1 criteria=2\n",
        encoding="utf-8",
    )
    state_path = runtime_dir / "current.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "target_id": "SA-TARGET",
                "log_path": str(log_path),
                "status_cursor": 0,
                "last_counts": {},
                "last_active_task": None,
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )

    items = {
        "SA-TARGET": {"id": "SA-TARGET", "stage": "plan_complete", "status": "open", "children": ["SA-CHILD"]},
        "SA-CHILD": {"id": "SA-CHILD", "stage": "in_review", "status": "in-progress", "children": []},
    }
    wl_runner = FakeWlRunner(items)

    snapshot = ralph_control.inspect_status(
        runtime_dir=runtime_dir,
        wl_runner=wl_runner,
        pid_is_alive=lambda pid: True,
    )

    assert snapshot["state"] == "running"
    assert snapshot["target_id"] == "SA-TARGET"
    assert snapshot["active_task"] == "SA-CHILD"
    assert snapshot["recent_activity"]
    assert "child_focus" in "\n".join(snapshot["recent_activity"])
    assert snapshot["status_counts"]["open"] == 1
    assert snapshot["status_counts"]["in-progress"] == 1
    assert snapshot["status_deltas"]["open"] == 1
    assert snapshot["status_deltas"]["in-progress"] == 1

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["status_cursor"] == 3
    assert saved["last_active_task"] == "SA-CHILD"
    assert saved["last_counts"]["open"] == 1

    # Append new activity and change the scope counts.
    log_path.write_text(log_path.read_text(encoding="utf-8") + "INFO ralph.loop.attempt.start target=SA-TARGET attempt=2\n", encoding="utf-8")
    items["SA-TARGET"]["status"] = "in-progress"
    items["SA-CHILD"]["status"] = "done"

    snapshot2 = ralph_control.inspect_status(
        runtime_dir=runtime_dir,
        wl_runner=wl_runner,
        pid_is_alive=lambda pid: True,
    )

    assert snapshot2["state"] == "running"
    assert snapshot2["recent_activity"] == ["INFO ralph.loop.attempt.start target=SA-TARGET attempt=2"]
    assert snapshot2["status_counts"]["in-progress"] == 1
    assert snapshot2["status_counts"]["done"] == 1
    assert snapshot2["status_deltas"]["in-progress"] == 0
    assert snapshot2["status_deltas"]["done"] == 1


def test_status_snapshot_reports_final_exit_code_when_process_is_gone(runtime_dir: Path):
    log_path = runtime_dir / "SA-TARGET.log"
    log_path.write_text("INFO ralph.loop.merge target=SA-TARGET confirm=False\n", encoding="utf-8")
    state_path = runtime_dir / "current.json"
    state_path.write_text(
        json.dumps(
            {
                "pid": 12345,
                "target_id": "SA-TARGET",
                "log_path": str(log_path),
                "status_cursor": 1,
                "last_counts": {"open": 1},
                "last_active_task": "SA-TARGET",
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )
    (runtime_dir / "current.exitcode").write_text("0\n", encoding="utf-8")

    snapshot = ralph_control.inspect_status(
        runtime_dir=runtime_dir,
        wl_runner=FakeWlRunner({"SA-TARGET": {"id": "SA-TARGET", "stage": "in_review", "status": "closed", "children": []}}),
        pid_is_alive=lambda pid: False,
    )

    assert snapshot["state"] == "stopped"
    assert snapshot["exit_code"] == 0
    assert snapshot["recent_activity"] == []
    assert snapshot["final_summary"]
