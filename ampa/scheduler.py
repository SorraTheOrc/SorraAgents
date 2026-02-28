"""AMPA command scheduler with persistent state."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from . import daemon
    from . import notifications as notifications_module
    from . import selection
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
    notifications_module = importlib.import_module("ampa.notifications")
    selection = importlib.import_module("ampa.selection")
    _er = importlib.import_module("ampa.error_report")
    build_error_report = _er.build_error_report
    render_error_report = _er.render_error_report
    render_error_report_json = _er.render_error_report_json

# Engine imports — only types referenced directly by the Scheduler class.
from .engine.core import Engine, EngineResult
from .engine.candidates import CandidateSelector

# ---------------------------------------------------------------------------
# Shared data classes and utilities — canonical definitions live in
# ampa.scheduler_types.
# ---------------------------------------------------------------------------
from .scheduler_types import (
    CommandSpec,
    SchedulerConfig,
    RunResult,
    CommandRunResult,
    _utc_now,
    _to_iso,
    _from_iso,
    _seconds_between,
)

LOG = logging.getLogger("ampa.scheduler")

from .scheduler_store import SchedulerStore  # noqa: E402

from .scheduler_executor import (  # noqa: E402
    default_llm_probe,
    default_executor,
    score_command,
)

from .bot_supervisor import BotSupervisor  # noqa: E402

from .engine_factory import build_engine  # noqa: E402


# ---------------------------------------------------------------------------
# Delegation helpers — canonical implementations live in ampa.delegation.
# ---------------------------------------------------------------------------
from .delegation import DelegationOrchestrator  # noqa: E402

from .scheduler_helpers import (  # noqa: E402
    clear_stale_running_states as _clear_stale_running_states,
    ensure_watchdog_command as _ensure_watchdog_command,
    ensure_test_button_command as _ensure_test_button_command,
    send_test_button_message as _send_test_button_message,
    log_health as _log_health,
)


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
                    msg = f"Command timed out after {p_kwargs.get('timeout')}s: {p_args[0] if p_args else '(command)'}"
                    notifications_module.notify(
                        title=(p_args[0] if p_args else "Timed-out command")[:128],
                        body=msg,
                        message_type="error",
                    )
                except Exception:
                    LOG.exception("Failed to send timeout notification")
                return subprocess.CompletedProcess(
                    args=p_args[0] if p_args else "",
                    returncode=124,
                    stdout=out,
                    stderr=err,
                )

        self.run_shell = _run_shell_with_timeout

        # --- Engine initialization ---
        # If an engine is explicitly provided, use it. Otherwise, build one
        # from the workflow descriptor via the engine factory. Use the
        # centralized factory here (build_engine) so the Scheduler does not
        # contain adapter construction logic.
        self._candidate_selector: Optional[CandidateSelector] = None
        if engine is not None:
            # Engine explicitly injected by caller (tests/overrides)
            self.engine: Optional[Engine] = engine
        else:
            # Build engine and candidate selector via the factory and assign
            # both to Scheduler state so downstream components (delegation
            # orchestrator, tests) can access the selector instance.
            eng, selector = build_engine(
                run_shell=self.run_shell,
                command_cwd=self.command_cwd,
                store=self.store,
            )
            self.engine = eng
            self._candidate_selector = selector

        # Delegation orchestrator — all delegation-specific orchestration is
        # handled by DelegationOrchestrator (ampa.delegation).
        self._delegation_orchestrator = DelegationOrchestrator(
            store=self.store,
            run_shell=self.run_shell,
            command_cwd=self.command_cwd,
            engine=self.engine,
            candidate_selector=self._candidate_selector,
            notifications_module=notifications_module,
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
            _clear_stale_running_states(self.store)
        except Exception:
            LOG.exception("Failed to clear stale running states")

        # Auto-register the stale delegation watchdog as a scheduled command
        # so it runs on its own cadence (every 30 minutes) independently of
        # delegation timing.
        _ensure_watchdog_command(self.store)

        # Auto-register the interactive test-button command so a periodic
        # "Blue or Red?" message with discord.ui.Button components is sent
        # every 15 minutes (MVP validation for interactive buttons).
        _ensure_test_button_command(self.store)

        # --- Discord bot process supervision ---
        self._bot_supervisor = BotSupervisor(
            run_shell=self.run_shell,
            command_cwd=self.command_cwd,
            notifications_module=notifications_module,
        )

    def _sync_orchestrator(self) -> None:
        """Keep the delegation orchestrator in sync with mutable scheduler state.

        Callers (including tests) may reassign ``self.run_shell``,
        ``self.engine``, or patch ``notifications_module`` / ``selection``
        after construction.  This method propagates those references to
        the orchestrator so delegation code paths see the current values.
        """
        orch = self._delegation_orchestrator
        orch.run_shell = self.run_shell
        orch.engine = self.engine
        # Ensure the orchestrator sees the current candidate selector
        # when tests or callers mutate ``self._candidate_selector`` after
        # Scheduler construction.
        orch._candidate_selector = self._candidate_selector
        orch._notifications_module = notifications_module
        orch._selection_module = selection

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
            # Route triage-audit through the audit poller for candidate
            # detection and cooldown filtering, then delegate audit execution
            # to TriageAuditRunner via the handoff handler protocol.
            try:
                from .audit_poller import PollerOutcome, poll_and_handoff
                from .triage_audit import TriageAuditRunner

                runner = TriageAuditRunner(
                    run_shell=self.run_shell,
                    command_cwd=self.command_cwd,
                    store=self.store,
                )

                def _audit_handler(work_item: dict) -> bool:
                    """Adapter: delegates the pre-selected candidate to
                    TriageAuditRunner for audit execution."""
                    return runner.run(spec, run, output, work_item=work_item)

                result = poll_and_handoff(
                    run_shell=self.run_shell,
                    cwd=self.command_cwd,
                    store=self.store,
                    spec=spec,
                    handler=_audit_handler,
                )

                if result.outcome == PollerOutcome.query_failed:
                    LOG.warning(
                        "Audit poller query failed: %s", result.error or "(no detail)"
                    )
                elif result.outcome == PollerOutcome.no_candidates:
                    LOG.info("Audit poller: no eligible candidates this cycle")
                else:
                    LOG.info(
                        "Audit poller handed off candidate %s",
                        result.selected_item_id,
                    )
            except Exception:
                LOG.exception("Failed to run audit poller / TriageAuditRunner")
            # triage-audit posts its own discord summary; avoid generic post
            return run
        if spec.command_type == "stale-delegation-watchdog":
            try:
                stale_recovered = (
                    self._delegation_orchestrator.recover_stale_delegations()
                )
                if stale_recovered:
                    LOG.info(
                        "Stale delegation watchdog recovered %d item(s)",
                        len(stale_recovered),
                    )
            except Exception:
                LOG.exception("Stale delegation watchdog failed")
            return run
        if spec.command_type == "test-button":
            try:
                _send_test_button_message(notifications_module)
            except Exception:
                LOG.exception("Test-button message failed")
            return run

        # always post the generic discord notification afterwards
        if spec.command_type != "heartbeat":
            try:
                from .delegation import _summarize_for_discord

                short_output = _summarize_for_discord(output, max_chars=1000)
            except Exception:
                LOG.exception("Failed to summarize output for discord post")
                short_output = output
            title = spec.title or spec.metadata.get("discord_label") or spec.command_id
            notifications_module.notify(
                title=title,
                body=short_output or "",
                message_type="command",
            )
        return run

    def run_once(self) -> Optional[RunResult]:
        now = _utc_now()
        next_cmd = self.select_next(now)
        if not next_cmd:
            return None
        return self.start_command(next_cmd, now)

    def run_forever(self) -> None:
        LOG.info("Starting scheduler loop")

        # Start the Discord bot process (no-op if token not configured).
        # Must happen before the startup message so the socket is ready.
        self._bot_supervisor.ensure_running()
        self._bot_supervisor.wait_for_socket()

        self._post_startup_message()

        # Install a shutdown handler so the bot is terminated when the
        # scheduler exits (SIGTERM / SIGINT / normal exit).
        def _shutdown_handler(signum, frame):
            LOG.info("Received signal %s – shutting down bot", signum)
            self._bot_supervisor.shutdown()
            # Re-raise to let the default handler terminate the scheduler.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _shutdown_handler)
            except (OSError, ValueError):
                # ValueError: signal only works in main thread
                pass

        # periodic health reporting accumulator (seconds)
        _health_accum = 0
        _health_interval = max(1, self.config.global_min_interval_seconds)
        try:
            while True:
                # Ensure bot is alive at the top of each cycle.
                self._bot_supervisor.ensure_running()
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
                        _log_health(self.store)
                    except Exception:
                        LOG.exception("Failed to emit periodic health report")
                    _health_accum = 0
        finally:
            # Best-effort cleanup on exit.
            self._bot_supervisor.shutdown()

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

    def _post_startup_message(self) -> None:
        self._bot_supervisor.post_startup_message()


def load_scheduler(command_cwd: Optional[str] = None) -> Scheduler:
    config = SchedulerConfig.from_env()
    store = SchedulerStore(config.store_path)
    return Scheduler(store, config, command_cwd=command_cwd)
