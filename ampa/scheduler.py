"""AMPA command scheduler with persistent state."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    requests = None

try:
    from . import daemon
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    daemon = importlib.import_module("ampa.daemon")

LOG = logging.getLogger("ampa.scheduler")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def _seconds_between(now: dt.datetime, then: Optional[dt.datetime]) -> Optional[float]:
    if then is None:
        return None
    return (now - then).total_seconds()


@dataclasses.dataclass(frozen=True)
class CommandSpec:
    command_id: str
    command: str
    requires_llm: bool
    frequency_minutes: int
    priority: int
    metadata: Dict[str, Any]
    max_runtime_minutes: Optional[int] = None
    command_type: str = "shell"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.command_id,
            "command": self.command,
            "requires_llm": self.requires_llm,
            "frequency_minutes": self.frequency_minutes,
            "priority": self.priority,
            "metadata": self.metadata,
            "max_runtime_minutes": self.max_runtime_minutes,
            "type": self.command_type,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "CommandSpec":
        return CommandSpec(
            command_id=str(data["id"]),
            command=str(data["command"]),
            requires_llm=bool(data.get("requires_llm", False)),
            frequency_minutes=int(data.get("frequency_minutes", 1)),
            priority=int(data.get("priority", 0)),
            metadata=dict(data.get("metadata", {})),
            max_runtime_minutes=data.get("max_runtime_minutes"),
            command_type=str(data.get("type", "shell")),
        )


@dataclasses.dataclass(frozen=True)
class SchedulerConfig:
    poll_interval_seconds: int
    global_min_interval_seconds: int
    priority_weight: float
    store_path: str
    llm_healthcheck_url: str
    max_run_history: int

    @staticmethod
    def from_env() -> "SchedulerConfig":
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name, str(default))
            try:
                value = int(raw)
                if value <= 0:
                    raise ValueError("must be positive")
                return value
            except Exception:
                LOG.warning("Invalid %s=%r; using %s", name, raw, default)
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name, str(default))
            try:
                value = float(raw)
                if value < 0:
                    raise ValueError("must be non-negative")
                return value
            except Exception:
                LOG.warning("Invalid %s=%r; using %s", name, raw, default)
                return default

        store_path = os.getenv("AMPA_SCHEDULER_STORE") or os.path.join(
            os.path.dirname(__file__), "scheduler_store.json"
        )
        return SchedulerConfig(
            poll_interval_seconds=_int("AMPA_SCHEDULER_POLL_INTERVAL_SECONDS", 5),
            global_min_interval_seconds=_int(
                "AMPA_SCHEDULER_GLOBAL_MIN_INTERVAL_SECONDS", 60
            ),
            priority_weight=_float("AMPA_SCHEDULER_PRIORITY_WEIGHT", 0.1),
            store_path=store_path,
            llm_healthcheck_url=os.getenv(
                "AMPA_LLM_HEALTHCHECK_URL", "http://localhost:8000/health"
            ),
            max_run_history=_int("AMPA_SCHEDULER_MAX_RUN_HISTORY", 50),
        )


@dataclasses.dataclass(frozen=True)
class RunResult:
    start_ts: dt.datetime
    end_ts: dt.datetime
    exit_code: int

    @property
    def duration_seconds(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds()


@dataclasses.dataclass(frozen=True)
class CommandRunResult(RunResult):
    output: str


class SchedulerStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("store root must be object")
                return data
        except FileNotFoundError:
            return {"commands": {}, "state": {}, "last_global_start_ts": None}
        except Exception:
            LOG.exception("Failed to read scheduler store; starting empty")
            return {"commands": {}, "state": {}, "last_global_start_ts": None}

    def save(self) -> None:
        dir_name = os.path.dirname(self.path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)

    def list_commands(self) -> List[CommandSpec]:
        return [
            CommandSpec.from_dict(value)
            for value in self.data.get("commands", {}).values()
        ]

    def add_command(self, spec: CommandSpec) -> None:
        self.data.setdefault("commands", {})[spec.command_id] = spec.to_dict()
        self.data.setdefault("state", {}).setdefault(spec.command_id, {})
        self.save()

    def remove_command(self, command_id: str) -> None:
        self.data.get("commands", {}).pop(command_id, None)
        self.data.get("state", {}).pop(command_id, None)
        self.save()

    def update_command(self, spec: CommandSpec) -> None:
        if spec.command_id not in self.data.get("commands", {}):
            raise KeyError(f"Unknown command id {spec.command_id}")
        self.data["commands"][spec.command_id] = spec.to_dict()
        self.save()

    def get_state(self, command_id: str) -> Dict[str, Any]:
        return dict(self.data.get("state", {}).get(command_id, {}))

    def update_state(self, command_id: str, state: Dict[str, Any]) -> None:
        self.data.setdefault("state", {})[command_id] = state
        self.save()

    def update_global_start(self, when: dt.datetime) -> None:
        self.data["last_global_start_ts"] = _to_iso(when)
        self.save()

    def last_global_start(self) -> Optional[dt.datetime]:
        return _from_iso(self.data.get("last_global_start_ts"))


def default_llm_probe(url: str) -> bool:
    if requests is None:
        LOG.debug("requests missing; assuming LLM unavailable")
        return False
    try:
        resp = requests.get(url, timeout=2)
        return resp.status_code < 500
    except Exception:
        return False


def default_executor(spec: CommandSpec, command_cwd: Optional[str] = None) -> RunResult:
    if spec.command_type == "heartbeat":
        start = _utc_now()
        try:
            config = daemon.get_env_config()
            status = daemon.run_once(config)
        except SystemExit as exc:
            status = getattr(exc, "code", 1) or 1
        end = _utc_now()
        return CommandRunResult(
            start_ts=start,
            end_ts=end,
            exit_code=int(status),
            output="heartbeat",
        )
    start = _utc_now()
    timeout = None
    if spec.max_runtime_minutes is not None:
        timeout = max(1, int(spec.max_runtime_minutes * 60))
    LOG.info("Starting command %s", spec.command_id)
    result = subprocess.run(  # nosec - shell execution is explicit configuration
        spec.command,
        shell=True,
        check=False,
        timeout=timeout,
        text=True,
        capture_output=True,
        cwd=command_cwd,
    )
    end = _utc_now()
    LOG.info(
        "Finished command %s exit=%s duration=%.2fs",
        spec.command_id,
        result.returncode,
        (end - start).total_seconds(),
    )
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += result.stderr
    return CommandRunResult(
        start_ts=start,
        end_ts=end,
        exit_code=result.returncode,
        output=output.strip(),
    )


def score_command(
    spec: CommandSpec,
    now: dt.datetime,
    last_run: Optional[dt.datetime],
    priority_weight: float,
) -> Tuple[float, float]:
    desired_interval = max(1.0, spec.frequency_minutes * 60.0)
    if last_run is None:
        time_since_last = now.timestamp()
    else:
        time_since_last = (now - last_run).total_seconds()
    lateness = time_since_last - desired_interval
    normalized_lateness = max(lateness / desired_interval, 0.0)
    priority_factor = 1.0 + max(priority_weight, 0.0) * spec.priority
    return normalized_lateness * priority_factor, normalized_lateness


class Scheduler:
    def __init__(
        self,
        store: SchedulerStore,
        config: SchedulerConfig,
        llm_probe: Optional[Callable[[str], bool]] = None,
        executor: Optional[Callable[[CommandSpec], RunResult]] = None,
        command_cwd: Optional[str] = None,
    ) -> None:
        self.store = store
        self.config = config
        self.llm_probe = llm_probe or default_llm_probe
        self.command_cwd = command_cwd or os.getcwd()
        if executor is None:
            self.executor = lambda spec: default_executor(spec, self.command_cwd)
        else:
            self.executor = executor

    def _global_rate_limited(self, now: dt.datetime) -> bool:
        last_start = self.store.last_global_start()
        if last_start is None:
            return False
        since = _seconds_between(now, last_start)
        if since is None:
            return False
        return since < self.config.global_min_interval_seconds

    def _eligible_commands(
        self, commands: Iterable[CommandSpec], llm_available: bool
    ) -> List[CommandSpec]:
        eligible = []
        for spec in commands:
            if spec.frequency_minutes <= 0:
                continue
            if spec.requires_llm and not llm_available:
                continue
            state = self.store.get_state(spec.command_id)
            if state.get("running") is True:
                continue
            eligible.append(spec)
        return eligible

    def select_next(self, now: Optional[dt.datetime] = None) -> Optional[CommandSpec]:
        now = now or _utc_now()
        if self._global_rate_limited(now):
            return None
        commands = self.store.list_commands()
        if not commands:
            return None
        llm_available = self.llm_probe(self.config.llm_healthcheck_url)
        eligible = self._eligible_commands(commands, llm_available)
        if not eligible:
            return None
        scored: List[Tuple[float, float, CommandSpec]] = []
        for spec in eligible:
            state = self.store.get_state(spec.command_id)
            last_run = _from_iso(state.get("last_run_ts"))
            score, normalized = score_command(
                spec, now, last_run, self.config.priority_weight
            )
            scored.append((score, normalized, spec))
        if not scored:
            return None
        scored.sort(
            key=lambda item: (item[0], item[1], item[2].command_id), reverse=True
        )
        if scored[0][0] <= 0:
            return None
        return scored[0][2]

    def _record_run(
        self,
        spec: CommandSpec,
        run: RunResult,
        exit_code: int,
        output: Optional[str],
    ) -> None:
        state = self.store.get_state(spec.command_id)
        state.update(
            {
                "running": False,
                "last_start_ts": _to_iso(run.start_ts),
                "last_run_ts": _to_iso(run.end_ts),
                "last_duration_seconds": run.duration_seconds,
                "last_exit_code": exit_code,
                "last_output": output,
            }
        )
        history = list(state.get("run_history", []))
        history.append(
            {
                "start_ts": _to_iso(run.start_ts),
                "end_ts": _to_iso(run.end_ts),
                "duration_seconds": run.duration_seconds,
                "exit_code": exit_code,
                "output": output,
            }
        )
        state["run_history"] = history[-self.config.max_run_history :]
        self.store.update_state(spec.command_id, state)

    def start_command(
        self, spec: CommandSpec, now: Optional[dt.datetime] = None
    ) -> RunResult:
        now = now or _utc_now()
        state = self.store.get_state(spec.command_id)
        state.update({"running": True, "last_start_ts": _to_iso(now)})
        self.store.update_state(spec.command_id, state)
        self.store.update_global_start(now)
        run = self.executor(spec)
        output: Optional[str] = None
        exit_code = run.exit_code
        if isinstance(run, CommandRunResult):
            output = run.output
            exit_code = run.exit_code
        self._record_run(spec, run, exit_code, output)
        self._post_discord(spec, run, output)
        return run

    def run_once(self) -> Optional[RunResult]:
        now = _utc_now()
        next_cmd = self.select_next(now)
        if not next_cmd:
            return None
        return self.start_command(next_cmd, now)

    def run_forever(self) -> None:
        LOG.info("Starting scheduler loop")
        self._post_startup_message()
        while True:
            try:
                self.run_once()
            except Exception:
                LOG.exception("Scheduler iteration failed")
            time.sleep(self.config.poll_interval_seconds)

    def simulate(
        self,
        duration_seconds: int,
        tick_seconds: int = 10,
        now: Optional[dt.datetime] = None,
    ) -> Dict[str, Any]:
        now = now or _utc_now()
        end = now + dt.timedelta(seconds=duration_seconds)
        observed: Dict[str, List[float]] = {}
        while now < end:
            candidates = self._eligible_commands(
                self.store.list_commands(), llm_available=True
            )
            if candidates:
                scores: List[Tuple[float, CommandSpec, Optional[dt.datetime]]] = []
                for spec in candidates:
                    state = self.store.get_state(spec.command_id)
                    last_run = _from_iso(state.get("last_run_ts"))
                    score, _normalized = score_command(
                        spec, now, last_run, self.config.priority_weight
                    )
                    scores.append((score, spec, last_run))
                scores.sort(
                    key=lambda item: (item[0], item[1].command_id), reverse=True
                )
                selected_score, selected_spec, last_run = scores[0]
                if selected_score > 0:
                    run = RunResult(start_ts=now, end_ts=now, exit_code=0)
                    self._record_run(selected_spec, run, 0, None)
                    self.store.update_global_start(now)
                    if last_run is not None:
                        delta = (now - last_run).total_seconds()
                        observed.setdefault(selected_spec.command_id, []).append(delta)
                    else:
                        observed.setdefault(selected_spec.command_id, [])
                else:
                    selected_spec = None
            now = now + dt.timedelta(seconds=tick_seconds)
        return {"observed": observed}

    def _post_discord(
        self, spec: CommandSpec, run: RunResult, output: Optional[str]
    ) -> None:
        if spec.command_type == "heartbeat":
            return
        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
        if not webhook:
            return
        hostname = os.uname().nodename
        ts = run.end_ts.isoformat()
        command_id = spec.command_id
        if spec.metadata.get("discord_label"):
            command_id = str(spec.metadata.get("discord_label"))
        payload = daemon.build_command_payload(
            hostname,
            ts,
            command_id,
            output,
            run.exit_code,
            title="AMPA Scheduler",
        )
        daemon.send_webhook(webhook, payload, message_type="command")

    def _post_startup_message(self) -> None:
        try:
            config = daemon.get_env_config()
        except SystemExit:
            return
        webhook = config.get("webhook")
        if not webhook:
            return
        hostname = os.uname().nodename
        ts = _utc_now().isoformat()
        payload = daemon.build_command_payload(
            hostname,
            ts,
            "scheduler_start",
            "Scheduler started",
            0,
            title="Scheduler Started",
        )
        daemon.send_webhook(webhook, payload, message_type="startup")


def load_scheduler(command_cwd: Optional[str] = None) -> Scheduler:
    config = SchedulerConfig.from_env()
    store = SchedulerStore(config.store_path)
    return Scheduler(store, config, command_cwd=command_cwd)


def _parse_metadata(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    raise ValueError("metadata must be a JSON object")


def _store_from_env() -> SchedulerStore:
    config = SchedulerConfig.from_env()
    return SchedulerStore(config.store_path)


def _cli_list(args: argparse.Namespace) -> int:
    store = _store_from_env()
    commands = [spec.to_dict() for spec in store.list_commands()]
    print(json.dumps(commands, indent=2, sort_keys=True))
    return 0


def _cli_add(args: argparse.Namespace) -> int:
    store = _store_from_env()
    spec = CommandSpec(
        command_id=args.command_id,
        command=args.command,
        requires_llm=args.requires_llm,
        frequency_minutes=args.frequency_minutes,
        priority=args.priority,
        metadata=_parse_metadata(args.metadata),
        max_runtime_minutes=args.max_runtime_minutes,
        command_type=args.command_type,
    )
    store.add_command(spec)
    return 0


def _cli_update(args: argparse.Namespace) -> int:
    store = _store_from_env()
    spec = CommandSpec(
        command_id=args.command_id,
        command=args.command,
        requires_llm=args.requires_llm,
        frequency_minutes=args.frequency_minutes,
        priority=args.priority,
        metadata=_parse_metadata(args.metadata),
        max_runtime_minutes=args.max_runtime_minutes,
        command_type=args.command_type,
    )
    store.update_command(spec)
    return 0


def _cli_remove(args: argparse.Namespace) -> int:
    store = _store_from_env()
    store.remove_command(args.command_id)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AMPA scheduler")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List scheduled commands")

    add = sub.add_parser("add", help="Add a scheduled command")
    add.add_argument("command_id")
    add.add_argument("command")
    add.add_argument("frequency_minutes", type=int)
    add.add_argument("priority", type=int)
    add.add_argument("--requires-llm", action="store_true")
    add.add_argument("--metadata")
    add.add_argument("--max-runtime-minutes", type=int, dest="max_runtime_minutes")
    add.add_argument("--type", dest="command_type", default="shell")

    update = sub.add_parser("update", help="Update a scheduled command")
    update.add_argument("command_id")
    update.add_argument("command")
    update.add_argument("frequency_minutes", type=int)
    update.add_argument("priority", type=int)
    update.add_argument("--requires-llm", action="store_true")
    update.add_argument("--metadata")
    update.add_argument("--max-runtime-minutes", type=int, dest="max_runtime_minutes")
    update.add_argument("--type", dest="command_type", default="shell")

    remove = sub.add_parser("remove", help="Remove a scheduled command")
    remove.add_argument("command_id")

    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    start_cwd = os.getcwd()
    parser = _build_parser()
    args = parser.parse_args()
    if not args.command:
        scheduler = load_scheduler(command_cwd=start_cwd)
        scheduler.run_forever()
        return
    handlers = {
        "list": _cli_list,
        "add": _cli_add,
        "update": _cli_update,
        "remove": _cli_remove,
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise SystemExit(2)
    handler(args)


if __name__ == "__main__":
    main()
