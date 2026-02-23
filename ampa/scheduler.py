"""AMPA command scheduler with persistent state."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import re
import uuid
import getpass
import shutil
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    requests = None

try:
    from . import daemon
    from . import webhook as webhook_module
    from . import selection
    from . import fallback
    from .error_report import (
        build_error_report,
        render_error_report,
        render_error_report_json,
    )
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    daemon = importlib.import_module("ampa.daemon")
    webhook_module = importlib.import_module("ampa.webhook")
    selection = importlib.import_module("ampa.selection")
    fallback = importlib.import_module("ampa.fallback")
    _er = importlib.import_module("ampa.error_report")
    build_error_report = _er.build_error_report
    render_error_report = _er.render_error_report
    render_error_report_json = _er.render_error_report_json

# Engine imports — the engine package is part of the ampa package and must
# always be available.
from .engine.core import Engine, EngineConfig, EngineResult, EngineStatus
from .engine.descriptor import load_descriptor
from .engine.candidates import CandidateSelector

from .engine.dispatch import OpenCodeRunDispatcher
from .engine.invariants import InvariantEvaluator
from .engine.adapters import (
    ShellCandidateFetcher,
    ShellInProgressQuerier,
    ShellWorkItemFetcher,
    ShellWorkItemUpdater,
    ShellCommentWriter,
    StoreDispatchRecorder,
    DiscordNotificationSender,
)

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
        # Accept common ISO forms including trailing 'Z' (UTC) by normalizing
        # to an offset-aware representation that datetime.fromisoformat can parse.
        v = value
        if isinstance(v, str) and v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return dt.datetime.fromisoformat(v)
    except Exception:
        return None


def _seconds_between(now: dt.datetime, then: Optional[dt.datetime]) -> Optional[float]:
    if then is None:
        return None
    return (now - then).total_seconds()


@dataclasses.dataclass(frozen=True)
class CommandSpec:
    # Keep positional ordering compatible with existing tests and callers.
    command_id: str
    command: str
    requires_llm: bool
    frequency_minutes: int
    priority: int
    metadata: Dict[str, Any]
    title: Optional[str] = None
    max_runtime_minutes: Optional[int] = None
    command_type: str = "shell"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.command_id,
            "command": self.command,
            "title": self.title,
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
            command=str(data.get("command", "")),
            requires_llm=bool(data.get("requires_llm", False)),
            frequency_minutes=int(data.get("frequency_minutes", 1)),
            priority=int(data.get("priority", 0)),
            metadata=dict(data.get("metadata", {})),
            title=data.get("title"),
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

        # The scheduler store MUST exist at the local per-project path.
        # The daemon is spawned with cwd=projectRoot (ampa.mjs) so
        # os.getcwd() gives the correct project root at startup.
        store_path = os.path.join(
            os.getcwd(), ".worklog", "ampa", "scheduler_store.json"
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
    metadata: Optional[Dict[str, Any]] = dataclasses.field(default=None)

    @property
    def duration_seconds(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds()


@dataclasses.dataclass(frozen=True)
class CommandRunResult(RunResult):
    output: str = ""


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
                data.setdefault("commands", {})
                data.setdefault("state", {})
                data.setdefault("last_global_start_ts", None)
                # append-only dispatch records for delegation actions
                data.setdefault("dispatches", [])
                return data
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Scheduler store not found at {self.path}. "
                "The local scheduler_store.json must exist at "
                "<projectRoot>/.worklog/ampa/scheduler_store.json before "
                "starting the scheduler. Copy scheduler_store_example.json "
                "to this location and configure your commands."
            ) from None
        except Exception:
            LOG.exception("Failed to read scheduler store at %s", self.path)
            raise

    def save(self) -> None:
        dir_name = os.path.dirname(self.path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)

    def append_dispatch(self, record: Dict[str, Any], retain_last: int = 100) -> str:
        """Append an append-only dispatch record and persist the store.

        Returns the generated dispatch id.
        """
        try:
            dispatch_id = record.get("id") or uuid.uuid4().hex
            record = dict(record)
            record["id"] = str(dispatch_id)
            record.setdefault("ts", _utc_now().isoformat())
            record.setdefault("session", uuid.uuid4().hex)
            # Best-effort runner identity
            try:
                record.setdefault("runner", getpass.getuser())
            except Exception:
                record.setdefault("runner", os.getenv("USER") or "(unknown)")
            self.data.setdefault("dispatches", []).append(record)
            # retention: keep only the most recent `retain_last` entries
            try:
                if isinstance(self.data.get("dispatches"), list):
                    self.data["dispatches"] = self.data["dispatches"][
                        -int(retain_last) :
                    ]
            except Exception:
                pass
            self.save()
            return str(dispatch_id)
        except Exception:
            LOG.exception("Failed to append dispatch record")
            # Fallback: return a best-effort id
            return str(uuid.uuid4().hex)

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

    def get_command(self, command_id: str) -> Optional[CommandSpec]:
        payload = self.data.get("commands", {}).get(command_id)
        if not payload:
            return None
        return CommandSpec.from_dict(payload)

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


def _bool_meta(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# ---------------------------------------------------------------------------
# Delegation helpers — canonical implementations live in ampa.delegation.
# Re-exported here for backward compatibility with existing callers/tests.
# ---------------------------------------------------------------------------
from .delegation import (  # noqa: E402, F401
    _summarize_for_discord,
    _trim_text,
    _content_hash,
    _format_in_progress_items,
    _format_candidate_line,
    _build_dry_run_report,
    _build_dry_run_discord_message,
    _build_delegation_report,
    _build_delegation_discord_message,
    DelegationOrchestrator,
)


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
    # Determine an execution timeout in seconds.
    # Priority (highest -> lowest):
    # 1. CommandSpec.max_runtime_minutes (per-command override)
    # 2. Delegation-specific env AMPA_DELEGATION_OPENCODE_TIMEOUT (used for opencode spawn)
    # 3. Global AMPA_CMD_TIMEOUT_SECONDS default
    timeout = None
    try:
        default_cmd_timeout = int(os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "3600"))
    except Exception:
        default_cmd_timeout = 3600
    if spec.max_runtime_minutes is not None:
        timeout = max(1, int(spec.max_runtime_minutes * 60))
    else:
        # Enforce a default timeout for delegation flows and for commands that
        # spawn `opencode run` to avoid leaving the scheduler marked running
        # indefinitely when a child process hangs. Non-opencode commands keep
        # the previous behaviour unless explicitly configured.
        try:
            delegate_env = os.getenv("AMPA_DELEGATION_OPENCODE_TIMEOUT")
            delegate_timeout = (
                int(delegate_env) if delegate_env else default_cmd_timeout
            )
        except Exception:
            delegate_timeout = default_cmd_timeout
        if spec.command_type == "delegation" or "opencode run" in (spec.command or ""):
            timeout = max(1, int(delegate_timeout))

    LOG.info("Starting command %s (timeout=%s)", spec.command_id, timeout)
    try:
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
    except subprocess.TimeoutExpired as e:
        # Normalize timeouts to exit code 124 and notify operators via
        # Discord when configured. Return a CompletedProcess-like object so
        # the rest of the function can treat the result uniformly.
        end = _utc_now()
        out = getattr(e, "output", None) or ""
        err = getattr(e, "stderr", None) or ""
        LOG.warning(
            "Command %s timed out after %s seconds",
            spec.command_id,
            timeout,
        )
        try:
            webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
            if webhook and webhook_module is not None:
                msg = f"Command {spec.command_id} timed out after {timeout}s: {spec.command}"
                payload = webhook_module.build_command_payload(
                    os.uname().nodename,
                    _utc_now().isoformat(),
                    spec.command_id,
                    msg,
                    124,
                    title=(spec.title or spec.command)[:128],
                )
                webhook_module.send_webhook(webhook, payload, message_type="error")
        except Exception:
            LOG.exception("Failed to send timeout webhook")
        result = subprocess.CompletedProcess(
            args=spec.command,
            returncode=124,
            stdout=out,
            stderr=err,
        )

    LOG.info(
        "Finished command %s exit=%s duration=%.2fs",
        spec.command_id,
        result.returncode,
        (end - start).total_seconds(),
    )
    output = ""
    if getattr(result, "stdout", None):
        output += result.stdout
    if getattr(result, "stderr", None):
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
        run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        engine: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.config = config
        self.llm_probe = llm_probe or default_llm_probe
        self.command_cwd = command_cwd or os.getcwd()
        if executor is None:
            self.executor = lambda spec: default_executor(spec, self.command_cwd)
        else:
            self.executor = executor
        # injectable shell runner (for tests); defaults to subprocess.run
        _orig_runner = run_shell or subprocess.run
        # default timeout for spawned commands (seconds); can be overridden
        # per-call by passing `timeout` to the runner. Default = 3600s (1 hour)
        try:
            _default_timeout = int(os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "3600"))
        except Exception:
            _default_timeout = 3600

        def _run_shell_with_timeout(*p_args, **p_kwargs) -> subprocess.CompletedProcess:
            # If caller provided an explicit timeout, respect it; otherwise use
            # configured default to avoid long-hanging child processes.
            if "timeout" not in p_kwargs:
                p_kwargs["timeout"] = _default_timeout
            try:
                return _orig_runner(*p_args, **p_kwargs)
            except TypeError as e:
                # Some injected test runners do not accept a `timeout` kwarg.
                # Retry without timeout when that is the case to remain
                # backwards-compatible with test doubles.
                msg = str(e)
                if "timeout" in msg or "unexpected keyword" in msg:
                    p_kwargs.pop("timeout", None)
                    return _orig_runner(*p_args, **p_kwargs)
                raise
            except subprocess.TimeoutExpired as e:
                # Convert TimeoutExpired into a CompletedProcess-like result so
                # callers can handle it consistently (they typically expect a
                # CompletedProcess and check returncode/stdout/stderr).
                out = getattr(e, "output", None)
                err = getattr(e, "stderr", None)
                LOG.warning(
                    "Command timed out after %s seconds: %s",
                    p_kwargs.get("timeout"),
                    p_args[0] if p_args else "(command)",
                )
                # send a Discord error notification when configured
                try:
                    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                    if webhook and webhook_module is not None:
                        msg = f"Command timed out after {p_kwargs.get('timeout')}s: {p_args[0] if p_args else '(command)'}"
                        payload = webhook_module.build_command_payload(
                            os.uname().nodename,
                            _utc_now().isoformat(),
                            "command_timeout",
                            msg,
                            124,
                            title=(p_args[0] if p_args else "Timed-out command")[:128],
                        )
                        webhook_module.send_webhook(
                            webhook, payload, message_type="error"
                        )
                except Exception:
                    LOG.exception("Failed to send timeout webhook")
                return subprocess.CompletedProcess(
                    args=p_args[0] if p_args else "",
                    returncode=124,
                    stdout=out,
                    stderr=err,
                )

        self.run_shell = _run_shell_with_timeout

        # --- Engine initialization ---
        # If an engine is explicitly provided, use it.  Otherwise, build one
        # from the workflow descriptor.  The engine is a hard dependency — if
        # it cannot be constructed the scheduler will raise.
        self._candidate_selector: Optional[CandidateSelector] = None
        self.engine: Optional[Engine] = engine
        if self.engine is None:
            self.engine = self._build_engine()

        # Delegation orchestrator — all delegation-specific orchestration is
        # handled by DelegationOrchestrator (ampa.delegation).
        self._delegation_orchestrator = DelegationOrchestrator(
            store=self.store,
            run_shell=self.run_shell,
            command_cwd=self.command_cwd,
            engine=self.engine,
            candidate_selector=self._candidate_selector,
            webhook_module=webhook_module,
            selection_module=selection,
        )

        LOG.info("Command runner timeout configured: %ss", _default_timeout)
        LOG.info(
            "Scheduler initialized: store=%s poll_interval=%s global_min_interval=%s",
            getattr(self.store, "path", "(unknown)"),
            self.config.poll_interval_seconds,
            self.config.global_min_interval_seconds,
        )
        # Log discovered commands for operator visibility
        try:
            commands = self.store.list_commands()
            if commands:
                for cmd in commands:
                    try:
                        LOG.info(
                            "Discovered scheduled command: id=%s type=%s title=%s requires_llm=%s freq=%dm priority=%s",
                            cmd.command_id,
                            getattr(cmd, "command_type", "(unknown)"),
                            getattr(cmd, "title", None),
                            getattr(cmd, "requires_llm", False),
                            getattr(cmd, "frequency_minutes", 0),
                            getattr(cmd, "priority", 0),
                        )
                    except Exception:
                        LOG.debug(
                            "Failed to log command details for %s",
                            getattr(cmd, "command_id", "(unknown)"),
                        )
            else:
                LOG.info(
                    "No scheduled commands discovered in store=%s",
                    getattr(self.store, "path", "(unknown)"),
                )
        except Exception:
            LOG.exception("Failed to enumerate scheduled commands for logging")
        # Clear any stale 'running' flags left from previous crashes or
        # interrupted runs so commands don't remain permanently blocked.
        try:
            self._clear_stale_running_states()
        except Exception:
            LOG.exception("Failed to clear stale running states")

        # Auto-register the stale delegation watchdog as a scheduled command
        # so it runs on its own cadence (every 30 minutes) independently of
        # delegation timing.
        self._ensure_watchdog_command()

    def _build_engine(self) -> Optional[Engine]:
        """Construct an Engine from the workflow descriptor.

        Also stores the ``CandidateSelector`` on ``self._candidate_selector``
        so that ``_inspect_idle_delegation`` can perform lightweight pre-flight
        checks without invoking the full engine pipeline.

        Returns ``None`` when the descriptor cannot be loaded (e.g. the YAML
        file is missing or malformed).
        """
        try:
            descriptor_path = os.getenv(
                "AMPA_WORKFLOW_DESCRIPTOR",
                os.path.join(
                    os.path.dirname(os.path.dirname(__file__)),
                    "docs",
                    "workflow",
                    "workflow.yaml",
                ),
            )

            descriptor = load_descriptor(descriptor_path)

            # Shell-based adapters for wl CLI calls
            fetcher = ShellWorkItemFetcher(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
            )
            candidate_fetcher = ShellCandidateFetcher(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
            )
            in_progress_querier = ShellInProgressQuerier(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
            )
            selector = CandidateSelector(
                descriptor=descriptor,
                fetcher=candidate_fetcher,
                in_progress_querier=in_progress_querier,
            )
            evaluator = InvariantEvaluator(
                invariants=descriptor.invariants,
                querier=in_progress_querier,
            )
            dispatcher = OpenCodeRunDispatcher(cwd=self.command_cwd)

            # Protocol adapters for external dependencies
            updater = ShellWorkItemUpdater(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
            )
            comment_writer = ShellCommentWriter(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
            )
            recorder = StoreDispatchRecorder(store=self.store)
            notifier = DiscordNotificationSender()

            # Resolve fallback mode at engine init time so it is consistent
            # for the lifetime of this scheduler instance.
            try:
                fb_mode = fallback.resolve_mode(None, require_config=True)
            except Exception:
                fb_mode = None

            engine_config = EngineConfig(
                descriptor_path=descriptor_path,
                fallback_mode=fb_mode,
            )

            engine = Engine(
                descriptor=descriptor,
                dispatcher=dispatcher,
                candidate_selector=selector,
                invariant_evaluator=evaluator,
                work_item_fetcher=fetcher,
                updater=updater,
                comment_writer=comment_writer,
                dispatch_recorder=recorder,
                notifier=notifier,
                config=engine_config,
            )
            LOG.info(
                "Engine initialized with descriptor=%s fallback_mode=%s",
                descriptor_path,
                fb_mode,
            )
            self._candidate_selector = selector
            return engine
        except Exception:
            LOG.exception("Failed to initialize engine")
            return None

    def _clear_stale_running_states(self) -> None:
        """Clear `running` flags for commands whose last_start_ts is older
        than AMPA_STALE_RUNNING_THRESHOLD_SECONDS (default 3600s).

        This prevents commands from remaining marked as running due to a
        previous crash or unhandled exception which would otherwise block
        future scheduling.
        """
        try:
            thresh_raw = os.getenv("AMPA_STALE_RUNNING_THRESHOLD_SECONDS", "3600")
            try:
                threshold = int(thresh_raw)
            except Exception:
                threshold = 3600
            now = _utc_now()
            for cmd in self.store.list_commands():
                try:
                    st = self.store.get_state(cmd.command_id) or {}
                    if st.get("running") is not True:
                        continue
                    last_start_iso = st.get("last_start_ts")
                    last_start = _from_iso(last_start_iso) if last_start_iso else None
                    age = (
                        None
                        if last_start is None
                        else int((now - last_start).total_seconds())
                    )
                    if age is None or age > threshold:
                        st["running"] = False
                        self.store.update_state(cmd.command_id, st)
                        LOG.info(
                            "Cleared stale running flag for %s (age_s=%s)",
                            cmd.command_id,
                            age,
                        )
                except Exception:
                    LOG.exception(
                        "Failed to evaluate/clear running state for %s",
                        getattr(cmd, "command_id", "?"),
                    )
        except Exception:
            LOG.exception("Unexpected error while clearing stale running states")

    # ------------------------------------------------------------------
    # Auto-registration of built-in commands
    # ------------------------------------------------------------------

    _WATCHDOG_COMMAND_ID = "stale-delegation-watchdog"

    def _ensure_watchdog_command(self) -> None:
        """Register the stale-delegation-watchdog command if absent.

        The watchdog runs on its own cadence (default 30 minutes) so that
        stuck delegated items are detected even when the delegation command
        itself is not being selected by the scheduler.
        """
        try:
            existing = self.store.list_commands()
            for cmd in existing:
                if cmd.command_id == self._WATCHDOG_COMMAND_ID:
                    LOG.debug(
                        "Watchdog command already registered: %s",
                        self._WATCHDOG_COMMAND_ID,
                    )
                    return
            watchdog_spec = CommandSpec(
                command_id=self._WATCHDOG_COMMAND_ID,
                command="echo watchdog",  # placeholder; actual work is in start_command
                requires_llm=False,
                frequency_minutes=30,
                priority=0,
                metadata={},
                title="Stale Delegation Watchdog",
                max_runtime_minutes=5,
                command_type="stale-delegation-watchdog",
            )
            self.store.add_command(watchdog_spec)
            LOG.info(
                "Auto-registered watchdog command: %s (every %dm)",
                self._WATCHDOG_COMMAND_ID,
                watchdog_spec.frequency_minutes,
            )
        except Exception:
            LOG.exception("Failed to auto-register watchdog command")

    # ------------------------------------------------------------------
    # Delegation — delegated to DelegationOrchestrator
    # ------------------------------------------------------------------

    def _sync_orchestrator(self) -> None:
        """Keep the delegation orchestrator in sync with mutable scheduler state.

        Callers (including tests) may reassign ``self.run_shell``,
        ``self.engine``, or patch ``webhook_module`` / ``selection``
        after construction.  This method propagates those references to
        the orchestrator so delegation code paths see the current values.
        """
        orch = self._delegation_orchestrator
        orch.run_shell = self.run_shell
        orch.engine = self.engine
        orch._webhook_module = webhook_module
        orch._selection_module = selection

    def _recover_stale_delegations(self) -> List[Dict[str, Any]]:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        self._sync_orchestrator()
        return self._delegation_orchestrator.recover_stale_delegations()

    def _is_delegation_report_changed(self, command_id: str, report_text: str) -> bool:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        return self._delegation_orchestrator._is_delegation_report_changed(
            command_id, report_text
        )

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

    def _inspect_idle_delegation(self) -> Dict[str, Any]:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        self._sync_orchestrator()
        return self._delegation_orchestrator._inspect_idle_delegation()

    def start_command(
        self, spec: CommandSpec, now: Optional[dt.datetime] = None
    ) -> RunResult:
        now = now or _utc_now()
        # Sync mutable scheduler state to the delegation orchestrator so that
        # callers (including tests) that reassign ``sched.run_shell`` after
        # construction see the updated reference in delegation code paths.
        self._sync_orchestrator()
        state = self.store.get_state(spec.command_id)
        state.update({"running": True, "last_start_ts": _to_iso(now)})
        self.store.update_state(spec.command_id, state)
        self.store.update_global_start(now)
        LOG.debug(
            "Executor starting for command_id=%s command=%r",
            spec.command_id,
            spec.command,
        )
        start_exec = _utc_now()
        try:
            run = self.executor(spec)
        except BaseException as exc:
            # Catch BaseException to ensure that signals (KeyboardInterrupt,
            # SystemExit) and other non-Exception subclasses do not leave a
            # command marked as `running`. We still surface a sensible
            # RunResult so the normal post-run recording and cleanup always
            # execute.
            LOG.exception("Executor raised an exception for %s", spec.command_id)
            end_exec = _utc_now()
            # Map common BaseExceptions to conventional exit codes where
            # appropriate (SystemExit may carry an explicit code; SIGINT is
            # typically 130). Default to 1 for other failures.
            if isinstance(exc, SystemExit):
                try:
                    exit_code = int(getattr(exc, "code", 1) or 1)
                except Exception:
                    exit_code = 1
            elif isinstance(exc, KeyboardInterrupt):
                exit_code = 130
            else:
                exit_code = 1
            run = RunResult(start_ts=start_exec, end_ts=end_exec, exit_code=exit_code)
            # continue execution so post-run hooks and state recording run as
            # normal and clear the running flag.
        else:
            end_exec = _utc_now()
        LOG.debug(
            "Executor finished for command_id=%s exit=%s duration=%.3fs",
            spec.command_id,
            getattr(run, "exit_code", None),
            (end_exec - start_exec).total_seconds(),
        )
        output: Optional[str] = None
        exit_code = run.exit_code
        if isinstance(run, CommandRunResult):
            output = run.output
            exit_code = run.exit_code
        if spec.command_type == "delegation":
            run = self._delegation_orchestrator.execute(spec, run, output)
            self._record_run(spec, run, run.exit_code, getattr(run, "output", output))
            return run
        self._record_run(spec, run, exit_code, output)
        # After recording run, perform any command-specific post actions
        if spec.command_id == "wl-triage-audit" or spec.command_type == "triage-audit":
            # delegate triage-audit processing to extracted module
            try:
                from .triage_audit import TriageAuditRunner

                runner = TriageAuditRunner(
                    run_shell=self.run_shell,
                    command_cwd=self.command_cwd,
                    store=self.store,
                    engine=getattr(self, "engine", None),
                )
                try:
                    runner.run(spec, run, output)
                except Exception:
                    LOG.exception("TriageAuditRunner.run() failed")
            except Exception:
                LOG.exception("Failed to import/execute TriageAuditRunner")
            # triage-audit posts its own discord summary; avoid generic post
            return run
        if spec.command_type == "stale-delegation-watchdog":
            try:
                stale_recovered = self._recover_stale_delegations()
                if stale_recovered:
                    LOG.info(
                        "Stale delegation watchdog recovered %d item(s)",
                        len(stale_recovered),
                    )
            except Exception:
                LOG.exception("Stale delegation watchdog failed")
            return run

        # always post the generic discord message afterwards
        self._post_discord(spec, run, output)
        return run

    def _run_triage_audit(
        self, spec: CommandSpec, run: RunResult, output: Optional[str]
    ) -> bool:
        """Execute triage-audit post-processing.

        This method:
        - Calls `wl in_progress --json` to get candidate work items
        - Selects the least-recently-updated work item not audited within cooldown
        - Executes `opencode run audit <work_item_id>` and captures output
        - Posts a short Discord summary and a WL comment containing the full output
        """
        # configuration: allow overrides via metadata
        try:
            default_cooldown_hours = int(spec.metadata.get("audit_cooldown_hours", 6))
        except Exception:
            default_cooldown_hours = 6
        try:
            truncate_chars = int(spec.metadata.get("truncate_chars", 65536))
        except Exception:
            truncate_chars = 65536

        audit_only = _bool_meta(spec.metadata.get("audit_only"))

        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")

        # helper to run shell commands via injectable runner
        # timeout for audit-invoked opencode commands (seconds)
        try:
            _audit_timeout = int(
                os.getenv("AMPA_AUDIT_OPENCODE_TIMEOUT")
                or os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "300")
            )
        except Exception:
            _audit_timeout = 300

        def _call(cmd: str) -> subprocess.CompletedProcess:
            LOG.debug("Running shell (verbose): %s", cmd)
            start = _utc_now()
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
                timeout=_audit_timeout,
            )
            end = _utc_now()
            try:
                stdout_len = len(proc.stdout) if proc.stdout is not None else 0
            except Exception:
                stdout_len = 0
            try:
                stderr_len = len(proc.stderr) if proc.stderr is not None else 0
            except Exception:
                stderr_len = 0
            LOG.info(
                "Shell run finished: cmd=%r returncode=%s duration=%.3fs stdout_len=%d stderr_len=%d",
                cmd,
                getattr(proc, "returncode", None),
                (end - start).total_seconds(),
                stdout_len,
                stderr_len,
            )
            if stdout_len > 0:
                LOG.debug("Shell stdout (truncated 512): %s", (proc.stdout or "")[:512])
            if stderr_len > 0:
                LOG.debug("Shell stderr (truncated 512): %s", (proc.stderr or "")[:512])
            return proc

        # keep a single _call wrapper for shell execution (no retries)

        # 1) list in_progress work items and blocked work items
        try:
            items: List[Dict[str, Any]] = []

            # get in-progress items (existing behaviour)
            proc = _call("wl in_progress --json")
            if proc.returncode != 0:
                LOG.warning("wl in_progress failed: %s", proc.stderr)
            else:
                try:
                    raw = json.loads(proc.stdout or "null")
                except Exception:
                    LOG.exception("Failed to parse wl in_progress output")
                    raw = None
                # normalize different wl outputs into a list
                if isinstance(raw, list):
                    items.extend(raw)
                elif isinstance(raw, dict):
                    for key in ("workItems", "work_items", "items", "data"):
                        val = raw.get(key)
                        if isinstance(val, list):
                            items.extend(val)
                            break
                    if not items:
                        for k, v in raw.items():
                            if isinstance(v, list) and k.lower().endswith("workitems"):
                                items.extend(v)
                                break

            # also include blocked work items so they can be audited/reopened
            # Only attempt blocked listing when explicitly enabled or when
            # certain metadata keys are present. This keeps the earlier
            # behaviour of not always calling the blocked endpoint while
            # allowing tests and configs that expect blocked lookup to opt-in.
            include_blocked = False
            try:
                meta = spec.metadata or {}
                include_blocked = bool(meta.get("include_blocked", False)) or (
                    "truncate_chars" in meta
                )
            except Exception:
                include_blocked = False

            proc_b = None
            if include_blocked:
                proc_b = _call("wl list --status blocked --json")
                if proc_b.returncode != 0:
                    # some WL installations may not support '--status blocked'; try a dedicated command
                    LOG.debug(
                        "wl list --status blocked failed: %s; trying 'wl blocked --json'",
                        proc_b.stderr,
                    )
                    proc_b = _call("wl blocked --json")
                if proc_b.returncode == 0 and proc_b.stdout:
                    try:
                        rawb = json.loads(proc_b.stdout or "null")
                    except Exception:
                        LOG.exception("Failed to parse wl blocked output")
                        rawb = None
                    if isinstance(rawb, list):
                        items.extend(rawb)
                    elif isinstance(rawb, dict):
                        for key in ("workItems", "work_items", "items", "data"):
                            val = rawb.get(key)
                            if isinstance(val, list):
                                items.extend(val)
                                break
                        if not items:
                            for k, v in rawb.items():
                                if isinstance(v, list) and k.lower().endswith(
                                    "workitems"
                                ):
                                    items.extend(v)
                                    break

            # deduplicate by id if same item appears in both lists
            unique: Dict[str, Dict[str, Any]] = {}
            for it in items:
                wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
                if not wid:
                    # some list outputs wrap items in 'data' etc; skip those without IDs
                    continue
                unique[str(wid)] = {**it, "id": wid}
            items = list(unique.values())

            if not items:
                LOG.info("Triage audit found no candidates")
                return False
            LOG.info(
                "Found %d candidate work item(s) (in_progress+blocked)", len(items)
            )

            # find candidate sorted by their updated timestamp (oldest first)
            def _item_updated_ts(it: Dict[str, Any]) -> Optional[dt.datetime]:
                for k in (
                    "updated_at",
                    "last_updated_at",
                    "updated_ts",
                    "updated",
                    "last_update_ts",
                ):
                    v = it.get(k)
                    if v:
                        try:
                            return _from_iso(v)
                        except Exception:
                            try:
                                return dt.datetime.fromisoformat(v)
                            except Exception:
                                continue
                return None

            now = _utc_now()

            # helper to choose per-item cooldown (falls back to default)
            def _get_cooldown_hours_for_item(it: Dict[str, Any]) -> int:
                try:
                    meta = spec.metadata or {}
                except Exception:
                    meta = {}

                def _int_meta(key: str, fallback: int) -> int:
                    try:
                        val = meta.get(key, None)
                        if val is None:
                            return int(fallback)
                        return int(val)
                    except Exception:
                        return int(fallback)

                status = (
                    it.get("status") or it.get("state") or it.get("stage") or ""
                ).lower()
                if status == "in_review":
                    return _int_meta(
                        "audit_cooldown_hours_in_review", default_cooldown_hours
                    )
                if status == "in_progress":
                    return _int_meta(
                        "audit_cooldown_hours_in_progress", default_cooldown_hours
                    )
                if status == "blocked":
                    return _int_meta(
                        "audit_cooldown_hours_blocked", default_cooldown_hours
                    )
                return default_cooldown_hours

            # filter out items audited within cooldown by inspecting WL comments
            candidates: List[Tuple[Optional[dt.datetime], Dict[str, Any]]] = []
            # load persisted per-item audit timestamps (if any) to enforce cooldown
            persisted_state = self.store.get_state(spec.command_id)
            persisted_by_item = (
                persisted_state.get("last_audit_at_by_item", {})
                if isinstance(persisted_state, dict)
                else {}
            )

            for it in items:
                wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
                if not wid:
                    continue

                # inspect WL comments to find the most recent audit comment
                last_audit: Optional[dt.datetime] = None
                try:
                    proc_c = _call(f"wl comment list {wid} --json")
                    if proc_c.returncode == 0 and proc_c.stdout:
                        try:
                            raw_comments = json.loads(proc_c.stdout)
                        except Exception:
                            raw_comments = []
                        # normalize comments list
                        comments = []
                        if isinstance(raw_comments, list):
                            comments = raw_comments
                        elif isinstance(raw_comments, dict):
                            for key in ("comments", "items", "data"):
                                val = raw_comments.get(key)
                                if isinstance(val, list):
                                    comments = val
                                    break
                        for c in comments:
                            body = (
                                c.get("comment") or c.get("body") or c.get("text") or ""
                            )
                            if not body:
                                continue
                            if "# AMPA Audit Result" not in body:
                                continue
                            # try to extract a timestamp from comment metadata
                            cand_ts = None
                            # try several common timestamp keys returned by WL
                            for key in (
                                "createdAt",
                                "created_at",
                                "created_ts",
                                "created",
                                "ts",
                                "timestamp",
                            ):
                                # support both exact key and case-insensitive fallback
                                v = c.get(key)
                                if v is None:
                                    # try a case-insensitive match
                                    for k2, v2 in c.items():
                                        if k2.lower() == key.lower():
                                            v = v2
                                            break
                                if v:
                                    try:
                                        cand_ts = _from_iso(v)
                                    except Exception:
                                        try:
                                            cand_ts = dt.datetime.fromisoformat(v)
                                        except Exception:
                                            cand_ts = None
                                    if cand_ts is not None:
                                        break
                            if cand_ts is None:
                                # no metadata timestamp available; skip
                                continue
                            if last_audit is None or cand_ts > last_audit:
                                last_audit = cand_ts
                except Exception:
                    LOG.exception("Failed to list comments for %s", wid)

                # also consider persisted last-audit timestamp for this item
                try:
                    pst = persisted_by_item.get(wid)
                    pdt = _from_iso(pst) if pst else None
                    if pdt is not None and (last_audit is None or pdt > last_audit):
                        last_audit = pdt
                except Exception:
                    LOG.debug("Failed to parse persisted last_audit for %s", wid)

                # per-item cooldown (defaults to configured default)
                try:
                    cooldown_hours_for_item = _get_cooldown_hours_for_item(it)
                except Exception:
                    cooldown_hours_for_item = default_cooldown_hours
                cooldown_delta = dt.timedelta(hours=cooldown_hours_for_item)

                if last_audit is not None and (now - last_audit) < cooldown_delta:
                    # skip - recently audited for this item's status
                    continue

                updated = _item_updated_ts(it)
                candidates.append((updated, {**it, "id": wid}))

            if not candidates:
                LOG.info("Triage audit found no candidates after cooldown filter")
                return False

            # sort by updated timestamp ascending (oldest first), None treated as oldest
            candidates.sort(
                key=lambda t: (
                    t[0] is not None,
                    t[0] or dt.datetime.fromtimestamp(0, dt.timezone.utc),
                )
            )
            selected = candidates[0][1]
            work_id = str(selected.get("id") or "")
            if not work_id:
                LOG.warning("Triage audit candidate missing id")
                return False
            title = selected.get("title") or selected.get("name") or "(no title)"
            # record selected candidate for easier observability
            LOG.info("Selected triage candidate %s — %s", work_id, title)

            # triage should not directly dispatch work. Remove delegation call
            # to avoid triage automatically starting delegation actions.
            delegation_result: Optional[Dict[str, Any]] = None

            # 2) run the audit command
            # use quoted subcommand so the audit string is passed as one argument
            # changed to use the leading slash form as requested
            audit_cmd = f'opencode run "/audit {work_id}"'
            LOG.info("Running audit command: %s", audit_cmd)
            proc_audit = _call(audit_cmd)
            audit_out = ""
            if proc_audit.stdout:
                audit_out += proc_audit.stdout
            if proc_audit.stderr:
                audit_out += proc_audit.stderr

            exit_code = proc_audit.returncode
            LOG.info(
                "Audit finished for %s exit=%s stdout_len=%d stderr_len=%d",
                work_id,
                exit_code,
                len(proc_audit.stdout or ""),
                len(proc_audit.stderr or ""),
            )

            # Delegation from triage: when enabled, run the idle delegation
            # logic after audit completes so we can include its note in the
            # Discord summary and optionally dispatch intake/plan work.
            try:
                # Suppress engine-level Discord notifications while we run an
                # opportunistic delegation from triage so that the triage
                # audit summary remains the canonical Discord message for this
                # run. Swap the engine notifier to a no-op implementation
                # for the duration of the call.
                old_notifier = None
                try:
                    if (
                        hasattr(self, "engine")
                        and getattr(self.engine, "_notifier", None) is not None
                    ):
                        old_notifier = self.engine._notifier
                        # Use the engine's NullNotificationSender to avoid an import
                        # cycle by referencing the class dynamically from the
                        # engine module.
                        from ampa.engine.core import NullNotificationSender

                        self.engine._notifier = NullNotificationSender()
                except Exception:
                    old_notifier = None

                delegation_result = self._run_idle_delegation(
                    audit_only=audit_only, spec=spec
                )
            except Exception:
                LOG.exception("Delegation run failed during triage audit")
                delegation_result = None
            finally:
                try:
                    if old_notifier is not None and hasattr(self, "engine"):
                        self.engine._notifier = old_notifier
                except Exception:
                    LOG.exception(
                        "Failed to restore engine notifier after triage delegation"
                    )

            # 3) post a short Discord summary (1-3 lines)
            # helper to extract a human-facing 'Summary' section from audit output
            def _extract_summary(text: str) -> str:
                if not text:
                    return ""
                # look for a heading-style or standalone 'Summary' line
                m = re.search(
                    r"^(?:#{1,6}\s*)?Summary\s*:?$", text, re.IGNORECASE | re.MULTILINE
                )
                if m:
                    start = m.end()
                    rest = text[start:]
                    lines = rest.splitlines()
                    collected: List[str] = []
                    for line in lines:
                        # stop on next markdown heading
                        if re.match(r"^\s*#{1,6}\s+", line):
                            break
                        # stop on next section like 'OtherSection:' (Title-case followed by colon)
                        if re.match(r"^[A-Z][A-Za-z0-9 \-]{0,80}\s*:$", line):
                            break
                        collected.append(line)
                    # strip leading/trailing blank lines
                    while collected and collected[0].strip() == "":
                        collected.pop(0)
                    while collected and collected[-1].strip() == "":
                        collected.pop()
                    return "\n".join(collected).strip()
                # fallback: try inline 'Summary:' followed by content on same line or next
                m2 = re.search(r"Summary:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
                if m2:
                    # take up to a reasonable length
                    return m2.group(1).strip().split("\n\n")[0].strip()
                return ""

            if webhook:
                summary_text = _extract_summary(audit_out or "")
                if not summary_text:
                    # fallback simple summary
                    summary_text = f"{work_id} — {title} | exit={exit_code}"
                if delegation_result:
                    try:
                        dn = (
                            delegation_result.get("note")
                            if isinstance(delegation_result, dict)
                            else str(delegation_result)
                        )
                    except Exception:
                        dn = None
                    if dn:
                        summary_text = f"{summary_text}\n{dn}"
                # if Discord will reject long messages, summarize to <=1000 chars
                try:
                    summary_text = _summarize_for_discord(summary_text, max_chars=1000)
                except Exception:
                    LOG.exception("Failed to summarize triage summary_text")

                # If the work item is blocked and in_review, scan the work item
                # description and comments for a PR URL and include it in the
                # Discord payload to request a review.
                pr_url: Optional[str] = None
                try:
                    proc_show_pre = _call(f"wl show {work_id} --json")
                    wi_pre = None
                    if proc_show_pre.returncode == 0 and proc_show_pre.stdout:
                        try:
                            wi_pre = json.loads(proc_show_pre.stdout)
                        except Exception:
                            wi_pre = None
                    status_val = None
                    stage_val = None
                    if isinstance(wi_pre, dict):
                        status_val = (
                            wi_pre.get("status")
                            or wi_pre.get("state")
                            or wi_pre.get("stage")
                        )
                        # description may be under different keys
                        description_text = (
                            wi_pre.get("description") or wi_pre.get("desc") or ""
                        )
                    else:
                        description_text = ""

                    # helper to find a PR URL in text
                    def _find_pr_in_text(text: str) -> Optional[str]:
                        if not text:
                            return None
                        m = re.search(
                            r"https?://github\.com/[^\s']+?/pull/\d+",
                            text,
                            re.I,
                        )
                        if m:
                            return m.group(0)
                        return None

                    # check description first
                    pr_url = _find_pr_in_text(description_text)
                    # if not found, check comments
                    if pr_url is None:
                        proc_comments = _call(f"wl comment list {work_id} --json")
                        if proc_comments.returncode == 0 and proc_comments.stdout:
                            try:
                                raw_comments = json.loads(proc_comments.stdout)
                            except Exception:
                                raw_comments = []
                            comments = []
                            if isinstance(raw_comments, list):
                                comments = raw_comments
                            elif isinstance(raw_comments, dict):
                                for key in ("comments", "items", "data"):
                                    val = raw_comments.get(key)
                                    if isinstance(val, list):
                                        comments = val
                                        break
                            for c in comments:
                                body = (
                                    c.get("comment")
                                    or c.get("body")
                                    or c.get("text")
                                    or ""
                                )
                                if not body:
                                    continue
                                found = _find_pr_in_text(body)
                                if found:
                                    pr_url = found
                                    break
                except Exception:
                    LOG.exception("Failed to discover PR URL for work item %s", work_id)

                try:
                    # Build a human-friendly markdown message: the first line is
                    # a concise heading describing the topic, the body contains
                    # only the human-readable summary. Avoid embedding command
                    # strings, exit codes or other technical fields.
                    heading_title = f"Triage Audit — {title}"
                    extra = [{"name": "Summary", "value": summary_text}]
                    # Optionally include a delegation hint. This is opt-in to
                    # avoid triggering wl next lookups during triage-only runs.
                    include_preview = False
                    try:
                        include_preview = bool(
                            (spec.metadata or {}).get("include_delegation_preview")
                        )
                    except Exception:
                        include_preview = False
                    if include_preview:
                        try:
                            delegation_preview = self._run_delegation_report(spec)
                        except Exception:
                            delegation_preview = None
                        extra.append(
                            {
                                "name": "Delegation",
                                "value": (delegation_preview or "(none)"),
                            }
                        )
                    else:
                        extra.append({"name": "Delegation", "value": "(skipped)"})
                    if pr_url:
                        extra.append({"name": "PR", "value": pr_url})
                    payload = webhook_module.build_payload(
                        hostname=os.uname().nodename,
                        timestamp_iso=_utc_now().isoformat(),
                        work_item_id=None,
                        extra_fields=extra,
                        title=heading_title,
                    )
                    webhook_module.send_webhook(
                        webhook, payload, message_type="command"
                    )
                except Exception:
                    LOG.exception("Failed to send discord summary")

            # 4) post full audit output as WL comment, truncating if necessary
            full_output = audit_out or ""
            # Post the audit result as a WL comment with a standard heading so
            # future runs can discover the last audit via comment timestamps.
            if len(full_output) <= truncate_chars:
                comment_text = full_output or "(no output)"
                try:
                    comment_parts = [
                        "# AMPA Audit Result",
                        "",
                        "Audit output:",
                        "",
                        comment_text,
                    ]
                    comment = "\n".join(comment_parts)
                    # write comment to temp file and use command substitution to avoid shell
                    # quoting issues with embedded quotes/newlines
                    fd, cpath = tempfile.mkstemp(
                        prefix=f"wl-audit-comment-{work_id}-", suffix=".md"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(comment)
                    cmd = f"wl comment add {work_id} --comment \"$(cat '{cpath}')\" --author 'ampa-scheduler' --json"
                    _call(cmd)
                    try:
                        os.remove(cpath)
                    except Exception:
                        LOG.debug("Failed to remove temp comment file %s", cpath)
                except Exception:
                    LOG.exception("Failed to post wl comment")
                # When running verbose/debug, verify the posted WL comment to
                # catch cases where only the heading was posted (no body).
                # Posting a heading-only audit comment is an ERROR-worthy
                # diagnostic because it produces ambiguous triage outputs.
                if LOG.isEnabledFor(logging.DEBUG):
                    try:
                        proc_verify = _call(f"wl comment list {work_id} --json")
                        if proc_verify.returncode == 0 and proc_verify.stdout:
                            try:
                                raw_comments = json.loads(proc_verify.stdout)
                            except Exception:
                                raw_comments = []
                            comments = []
                            if isinstance(raw_comments, list):
                                comments = raw_comments
                            elif isinstance(raw_comments, dict):
                                for key in ("comments", "items", "data"):
                                    val = raw_comments.get(key)
                                    if isinstance(val, list):
                                        comments = val
                                        break
                            # pick the most-recent comment (best-effort)
                            latest = None
                            latest_ts = None
                            for c in comments:
                                # normalize timestamp
                                ts = None
                                for k in (
                                    "createdAt",
                                    "created_at",
                                    "created",
                                    "ts",
                                    "timestamp",
                                ):
                                    v = c.get(k)
                                    if v:
                                        try:
                                            ts = _from_iso(v)
                                        except Exception:
                                            try:
                                                ts = dt.datetime.fromisoformat(v)
                                            except Exception:
                                                ts = None
                                        if ts is not None:
                                            break
                                if latest is None or (
                                    ts is not None
                                    and (latest_ts is None or ts > latest_ts)
                                ):
                                    latest = c
                                    latest_ts = ts
                            if latest:
                                body = (
                                    latest.get("comment")
                                    or latest.get("body")
                                    or latest.get("text")
                                    or ""
                                )
                                # strip the standard heading and the 'Audit output:' label
                                stripped = re.sub(
                                    r"(?i)^\s*#\s*AMPA Audit Result\s*", "", body
                                )
                                stripped = re.sub(
                                    r"(?i)^\s*Audit output:\s*", "", stripped
                                ).strip()
                                if not stripped or stripped == "(no output)":
                                    LOG.error(
                                        "Posted AMPA audit comment for %s appears heading-only or empty; audit_out_len=%d posted_body_len=%d",
                                        work_id,
                                        len(full_output or ""),
                                        len(body or ""),
                                    )
                    except Exception:
                        LOG.exception(
                            "Failed to verify posted WL comment for %s", work_id
                        )
            else:
                # write artifact to temp file and post comment referencing it
                try:
                    fd, path = tempfile.mkstemp(
                        prefix=f"wl-audit-{work_id}-", suffix=".log"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(full_output)
                    comment_parts = [
                        "# AMPA Audit Result",
                        "",
                        f"Audit output too large; full output saved to: {path}",
                    ]
                    comment = "\n".join(comment_parts)
                    fd2, cpath = tempfile.mkstemp(
                        prefix=f"wl-audit-comment-{work_id}-", suffix=".md"
                    )
                    with os.fdopen(fd2, "w", encoding="utf-8") as fh:
                        fh.write(comment)
                    cmd = f"wl comment add {work_id} --comment \"$(cat '{cpath}')\" --author 'ampa-scheduler' --json"
                    _call(cmd)
                    try:
                        os.remove(cpath)
                    except Exception:
                        LOG.debug("Failed to remove temp comment file %s", cpath)
                except Exception:
                    LOG.exception("Failed to write artifact and post comment")
                # also verify posted comment when verbose for the large-artifact path
                if LOG.isEnabledFor(logging.DEBUG):
                    try:
                        proc_verify = _call(f"wl comment list {work_id} --json")
                        if proc_verify.returncode == 0 and proc_verify.stdout:
                            try:
                                raw_comments = json.loads(proc_verify.stdout)
                            except Exception:
                                raw_comments = []
                            comments = []
                            if isinstance(raw_comments, list):
                                comments = raw_comments
                            elif isinstance(raw_comments, dict):
                                for key in ("comments", "items", "data"):
                                    val = raw_comments.get(key)
                                    if isinstance(val, list):
                                        comments = val
                                        break
                            latest = None
                            latest_ts = None
                            for c in comments:
                                ts = None
                                for k in (
                                    "createdAt",
                                    "created_at",
                                    "created",
                                    "ts",
                                    "timestamp",
                                ):
                                    v = c.get(k)
                                    if v:
                                        try:
                                            ts = _from_iso(v)
                                        except Exception:
                                            try:
                                                ts = dt.datetime.fromisoformat(v)
                                            except Exception:
                                                ts = None
                                        if ts is not None:
                                            break
                                if latest is None or (
                                    ts is not None
                                    and (latest_ts is None or ts > latest_ts)
                                ):
                                    latest = c
                                    latest_ts = ts
                            if latest:
                                body = (
                                    latest.get("comment")
                                    or latest.get("body")
                                    or latest.get("text")
                                    or ""
                                )
                                stripped = re.sub(
                                    r"(?i)^\s*#\s*AMPA Audit Result\s*", "", body
                                )
                                stripped = re.sub(
                                    r"(?i)^\s*Audit output:\s*", "", stripped
                                ).strip()
                                if not stripped or stripped == "(no output)":
                                    LOG.error(
                                        "Posted AMPA audit comment (artifact path) for %s appears heading-only or empty; audit_out_len=%d posted_body_len=%d",
                                        work_id,
                                        len(full_output or ""),
                                        len(body or ""),
                                    )
                    except Exception:
                        LOG.exception(
                            "Failed to verify posted WL comment for %s", work_id
                        )
            # After posting the audit comment, persist last-audit timestamp and
            # check whether the work item can be
            # auto-completed. Criteria (both required):
            #  - Evidence of a merged PR (either a GitHub PR URL in the audit output
            #    or a textual 'PR merged' token), and
            #  - No open/in_progress child work items (or the audit explicitly
            #    states the item is ready to close).
            # persist last audit timestamp so future runs can consult it even
            # if Worklog comments are delayed or missing.
            try:
                state = self.store.get_state(spec.command_id)
                if not isinstance(state, dict):
                    state = dict(state or {})
                state.setdefault("last_audit_at_by_item", {})
                state["last_audit_at_by_item"][work_id] = _to_iso(now)
                self.store.update_state(spec.command_id, state)
            except Exception:
                LOG.exception("Failed to persist last_audit_at_by_item for %s", work_id)

            if audit_only:
                return True

            # After completing triage processing, opportunistically attempt to
            # dispatch work when agents are idle. This restores the historical
            # behaviour where triage can kick off delegation when nothing else
            # is in-progress. Guard behind the audit_only check above so tests
            # and configurations that disable auto-delegation still work.
            try:
                delegation_result = self._run_idle_delegation(
                    audit_only=False, spec=spec
                )
                LOG.info("Triage-initiated delegation result: %s", delegation_result)
            except Exception:
                LOG.exception("Failed to run delegation from triage")

            try:
                # fetch latest work item state
                proc_show = _call(f"wl show {work_id} --json")
                if proc_show.returncode == 0 and proc_show.stdout:
                    try:
                        wi_raw = json.loads(proc_show.stdout)
                    except Exception:
                        wi_raw = {}
                else:
                    wi_raw = {}

                # determine if children are open
                def _children_open(wobj: Dict[str, Any]) -> bool:
                    # look for common child containers
                    for key in (
                        "children",
                        "workItems",
                        "work_items",
                        "items",
                        "subtasks",
                    ):
                        val = wobj.get(key)
                        if isinstance(val, list) and val:
                            # if any child has a status that's not closed/completed, consider open
                            for c in val:
                                st = c.get("status") or c.get("state") or c.get("stage")
                                if st and str(st).lower() not in (
                                    "closed",
                                    "done",
                                    "completed",
                                    "resolved",
                                ):
                                    return True
                            # no open children
                            return False
                    return False

                children_open = _children_open(wi_raw)

                # check audit output for PR merged evidence or PR URL
                merged_pr = False

                def _extract_pr_from_text(text: str):
                    if not text:
                        return None, None
                    m = re.search(
                        r"https?://github\.com/(?P<owner_repo>[^/]+/[^/]+)/pull/(?P<number>\d+)",
                        text,
                        re.I,
                    )
                    if m:
                        return m.group("owner_repo"), m.group("number")
                    return None, None

                def _verify_pr_with_gh(owner_repo: str, pr_num: str) -> bool:
                    # Determine whether to verify via gh. Priority (highest -> lowest):
                    # 1) per-command metadata if present,
                    # 2) environment variable AMPA_VERIFY_PR_WITH_GH if set,
                    # 3) default: enabled (True).
                    meta_val = spec.metadata.get("verify_pr_with_gh")
                    if meta_val is not None:
                        try:
                            verify_enabled = bool(meta_val)
                        except Exception:
                            verify_enabled = str(meta_val).lower() in (
                                "1",
                                "true",
                                "yes",
                            )
                    else:
                        env = os.getenv("AMPA_VERIFY_PR_WITH_GH")
                        if env is None or env == "":
                            verify_enabled = True
                        else:
                            verify_enabled = env.lower() in ("1", "true", "yes")
                    if not verify_enabled:
                        # verification explicitly disabled; treat PR URL presence as evidence
                        return True
                    # ensure gh CLI is present
                    if shutil.which("gh") is None:
                        LOG.warning("gh CLI not found; cannot verify PR merged status")
                        return False
                    cmd = f"gh pr view {pr_num} --repo {owner_repo} --json merged"
                    proc = _call(cmd)
                    if proc.returncode != 0 or not proc.stdout:
                        LOG.warning(
                            "gh pr view failed: cmd=%r rc=%s stderr=%r",
                            cmd,
                            getattr(proc, "returncode", None),
                            getattr(proc, "stderr", None),
                        )
                        return False
                    try:
                        data = json.loads(proc.stdout)
                        return bool(data.get("merged")) is True
                    except Exception:
                        LOG.exception("Failed to parse gh pr view output")
                        return False

                # first try to extract a PR URL
                owner_repo, pr_num = _extract_pr_from_text(audit_out or "")
                if owner_repo and pr_num:
                    if _verify_pr_with_gh(owner_repo, pr_num):
                        merged_pr = True
                else:
                    # fallback to textual heuristics
                    if audit_out and re.search(
                        r"pr\s*merged|merged\s+pr|pull request\s+merged",
                        audit_out,
                        re.I,
                    ):
                        merged_pr = True

                # check audit output for explicit ready-to-close tokens
                ready_token = False
                if audit_out and re.search(
                    r"ready to close|can be closed|ready for final|ready for sign-?off",
                    audit_out,
                    re.I,
                ):
                    ready_token = True

                if merged_pr and (not children_open or ready_token):
                    # proceed to mark work item completed -> in_review
                    try:
                        upd_cmd = f"wl update {work_id} --status completed --stage in_review --json"
                        _call(upd_cmd)
                        # send a completion-style discord message (embed-like payload)
                        try:
                            if webhook:
                                # Send a concise completion message using the
                                # simplified markdown header format described
                                # above.
                                # Send a concise, human-readable completion message
                                heading_title = f"Audit Completed — {title}"
                                # extract a short human-facing summary if possible
                                try:
                                    short = _extract_summary(audit_out or "") or (
                                        audit_out or ""
                                    )
                                    short = _summarize_for_discord(
                                        short, max_chars=1000
                                    )
                                except Exception:
                                    short = (audit_out or "")[:1000]
                                payload = webhook_module.build_payload(
                                    hostname=os.uname().nodename,
                                    timestamp_iso=_utc_now().isoformat(),
                                    work_item_id=None,
                                    extra_fields=[{"name": "Result", "value": short}],
                                    title=heading_title,
                                )
                                webhook_module.send_webhook(
                                    webhook, payload, message_type="completion"
                                )
                        except Exception:
                            LOG.exception("Failed to send completion webhook")
                    except Exception:
                        LOG.exception("Failed to auto-update work item %s", work_id)
            except Exception:
                LOG.exception("Auto-complete check failed for %s", work_id)
        except Exception:
            LOG.exception("Error during triage audit processing")
            return False
        return True

    def run_once(self) -> Optional[RunResult]:
        now = _utc_now()
        next_cmd = self.select_next(now)
        if not next_cmd:
            return None
        return self.start_command(next_cmd, now)

    def run_forever(self) -> None:
        LOG.info("Starting scheduler loop")
        self._post_startup_message()
        # periodic health reporting accumulator (seconds)
        _health_accum = 0
        _health_interval = max(1, self.config.global_min_interval_seconds)
        while True:
            try:
                self.run_once()
            except Exception:
                LOG.exception("Scheduler iteration failed")
            # sleep then accumulate for periodic health reporting
            try:
                time.sleep(self.config.poll_interval_seconds)
            except Exception:
                # sleep can be interrupted (signals); continue loop
                pass
            _health_accum += self.config.poll_interval_seconds
            if _health_accum >= _health_interval:
                try:
                    self._log_health()
                except Exception:
                    LOG.exception("Failed to emit periodic health report")
                _health_accum = 0

    def _log_health(self) -> None:
        """Emit a periodic health report about scheduled commands.

        Reports last run timestamp, exit code and running state for each
        discovered command so operators can quickly see recent activity.
        """
        try:
            cmds = self.store.list_commands()
        except Exception:
            LOG.exception("Failed to read commands for health report")
            return
        lines: List[str] = []
        now = _utc_now()
        for cmd in cmds:
            try:
                state = self.store.get_state(cmd.command_id) or {}
                last_run_iso = state.get("last_run_ts")
                last_run_dt = _from_iso(last_run_iso) if last_run_iso else None
                age = (
                    int((now - last_run_dt).total_seconds())
                    if last_run_dt is not None
                    else None
                )
                running = bool(state.get("running"))
                last_exit = state.get("last_exit_code")
                lines.append(
                    f"{cmd.command_id} title={cmd.title!r} last_run={last_run_iso or 'never'} age_s={age if age is not None else 'NA'} exit={last_exit} running={running}"
                )
            except Exception:
                LOG.exception(
                    "Failed to build health line for %s",
                    getattr(cmd, "command_id", "?"),
                )
        LOG.info(
            "Scheduler health report: %d commands\n%s", len(lines), "\n".join(lines)
        )

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
        # ensure Discord-safe summary for output
        try:
            short_output = _summarize_for_discord(output, max_chars=1000)
        except Exception:
            LOG.exception("Failed to summarize output for discord post")
            short_output = output

        payload = webhook_module.build_command_payload(
            hostname,
            ts,
            command_id,
            short_output,
            run.exit_code,
            title=(spec.title or spec.metadata.get("discord_label") or spec.command_id),
        )
        webhook_module.send_webhook(webhook, payload, message_type="command")

    def _run_idle_delegation(
        self, *, audit_only: bool, spec: Optional[CommandSpec] = None
    ) -> Dict[str, Any]:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        self._sync_orchestrator()
        return self._delegation_orchestrator.run_idle_delegation(
            audit_only=audit_only, spec=spec
        )

    @staticmethod
    def _engine_rejections(result: EngineResult) -> List[Dict[str, str]]:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        return DelegationOrchestrator._engine_rejections(result)

    def _run_delegation_report(
        self, spec: Optional[CommandSpec] = None
    ) -> Optional[str]:
        """Thin wrapper — delegates to ``DelegationOrchestrator``."""
        self._sync_orchestrator()
        return self._delegation_orchestrator.run_delegation_report(spec)

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
        # Capture the human-facing output of `wl status` for the startup message
        try:
            proc = self.run_shell(
                "wl status",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            # Some test doubles may return CompletedProcess with stdout only when
            # the command used '--json'. Ensure we try the JSON variant when the
            # plain invocation produced no useful output so tests that stub
            # `run_shell` for `wl status` still exercise the intended path.
            if getattr(proc, "stdout", None) == "":
                json_proc = self.run_shell(
                    "wl status --json",
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                )
                if getattr(json_proc, "stdout", None):
                    proc = json_proc
            status_out = ""
            if getattr(proc, "stdout", None):
                status_out += proc.stdout
            if getattr(proc, "stderr", None):
                # prefer stderr only if stdout is empty to keep message concise
                if not status_out:
                    status_out += proc.stderr
            if not status_out:
                status_out = "(wl status produced no output)"
        except Exception:
            LOG.exception("Failed to run 'wl status' for startup message")
            status_out = "(wl status unavailable)"

        payload = webhook_module.build_command_payload(
            hostname,
            ts,
            "scheduler_start",
            status_out,
            0,
            title="Scheduler Started",
        )
        webhook_module.send_webhook(webhook, payload, message_type="startup")


def load_scheduler(command_cwd: Optional[str] = None) -> Scheduler:
    config = SchedulerConfig.from_env()
    store = SchedulerStore(config.store_path)
    return Scheduler(store, config, command_cwd=command_cwd)


# ---------------------------------------------------------------------------
# CLI entry point has been extracted to ampa/scheduler_cli.py.
# Re-export key CLI symbols so existing callers (tests, scripts) continue to
# work without import changes while they migrate.
# ---------------------------------------------------------------------------
from .scheduler_cli import (  # noqa: F401  -- re-exports for backward compat
    main,
    _build_parser,
    _cli_list,
    _cli_add,
    _cli_update,
    _cli_remove,
    _cli_dry_run,
    _cli_run,
    _cli_run_once,
    _parse_metadata,
    _store_from_env,
    _command_description,
    _build_command_listing,
    _truncate_text,
    _format_command_table,
    _format_run_result_json,
    _format_run_result_human,
    _format_command_detail,
    _format_command_details_table,
    _get_instance_name,
)
