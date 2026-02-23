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
