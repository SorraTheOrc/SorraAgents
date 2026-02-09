import datetime as dt
import json
import subprocess

from ampa.scheduler import (
    CommandRunResult,
    CommandSpec,
    Scheduler,
    SchedulerConfig,
    SchedulerStore,
)


class DummyStore(SchedulerStore):
    def __init__(self) -> None:
        self.path = ":memory:"
        self.data = {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        return None


def _make_scheduler(run_shell_callable, tmp_path):
    store = DummyStore()
    config = SchedulerConfig(
        poll_interval_seconds=1,
        global_min_interval_seconds=1,
        priority_weight=0.1,
        store_path=str(tmp_path / "store.json"),
        llm_healthcheck_url="http://localhost/health",
        max_run_history=5,
    )

    def _executor(_spec):
        now = dt.datetime.now(dt.timezone.utc)
        return CommandRunResult(start_ts=now, end_ts=now, exit_code=0, output="")

    return Scheduler(
        store,
        config,
        run_shell=run_shell_callable,
        command_cwd=str(tmp_path),
        executor=_executor,
    )


def _delegation_spec():
    return CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation Report",
        command_type="delegation",
    )


def test_delegation_in_progress_prints_single_line(tmp_path, capsys):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            out = json.dumps({"workItems": [{"id": "SA-1", "title": "Busy"}]})
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=out, stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Found 1 in progress", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {"workItem": {"id": "SA-9", "title": "Next", "stage": "idea"}}
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert (
        out.strip()
        == "There is work in progress and thus no new work will be delegated."
    )


def test_delegation_idle_prints_markdown_summary(tmp_path, capsys):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "workItem": {"id": "SA-42", "title": "Do thing", "stage": "idea"}
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if cmd.strip() == "wl show SA-42 --json":
            item = {
                "id": "SA-42",
                "title": "Do thing",
                "stage": "idea",
                "priority": 2,
                "assignee": "alex",
                "description": "A short description that should be included in the summary.",
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(item), stderr=""
            )
        if cmd.strip().startswith("opencode run"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert out.startswith("Starting work on")
    assert "# Do thing - SA-42" in out
    assert "- ID: SA-42" in out
    assert "- Status/Stage: idea" in out
    assert "- Assignee: alex" in out
    assert "```json" in out


def test_delegation_idle_falls_back_when_show_invalid(tmp_path, capsys):
    def fake_run_shell(cmd, **kwargs):
        if cmd.strip() == "wl in_progress --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps({"workItems": []}), stderr=""
            )
        if cmd.strip() == "wl in_progress":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        if cmd.strip() == "wl next --json":
            payload = {
                "workItem": {"id": "SA-99", "title": "Fallback", "stage": "idea"}
            }
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if cmd.strip() == "wl show SA-99 --json":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="{bad json", stderr=""
            )
        if cmd.strip().startswith("opencode run"):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sched = _make_scheduler(fake_run_shell, tmp_path)
    sched.start_command(_delegation_spec())
    out = capsys.readouterr().out

    assert "Starting work on: Fallback - SA-99" in out
