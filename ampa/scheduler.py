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
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    daemon = importlib.import_module("ampa.daemon")
    webhook_module = importlib.import_module("ampa.webhook")
    selection = importlib.import_module("ampa.selection")

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
                data.setdefault("commands", {})
                data.setdefault("state", {})
                data.setdefault("last_global_start_ts", None)
                # append-only dispatch records for delegation actions
                data.setdefault("dispatches", [])
                return data
        except FileNotFoundError:
            # Try to initialize from example file if available
            self._initialize_from_example()
            # Try loading again after initialization
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if not isinstance(data, dict):
                        raise ValueError("store root must be object")
                    return data
            except Exception:
                # Fall back to empty store if initialization or re-load fails
                return {
                    "commands": {},
                    "state": {},
                    "last_global_start_ts": None,
                }
        except Exception:
            LOG.exception("Failed to read scheduler store; starting empty")
            return {
                "commands": {},
                "state": {},
                "last_global_start_ts": None,
                "dispatches": [],
            }

    def _initialize_from_example(self) -> None:
        """Initialize scheduler_store.json from scheduler_store_example.json if available."""
        try:
            # Look for example file in same directory as this module
            example_path = os.path.join(
                os.path.dirname(__file__), "scheduler_store_example.json"
            )
            if not os.path.exists(example_path):
                LOG.debug("No example file found at %s", example_path)
                return

            # Ensure target directory exists
            dir_name = os.path.dirname(self.path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)

            # Copy example to target location
            shutil.copy(example_path, self.path)
            LOG.info(
                "Initialized scheduler_store.json from example at %s", example_path
            )
        except Exception:
            LOG.debug("Failed to initialize scheduler_store from example")
            # Not fatal - we'll use an empty store

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


def _summarize_for_discord(text: Optional[str], max_chars: int = 1000) -> str:
    """If text is longer than max_chars, call `opencode run` to produce a short summary.

    Returns the original text on any failure.
    """
    if not text:
        return ""
    try:
        if len(text) <= max_chars:
            return text
        # avoid passing extremely large blobs to the CLI; cap input size
        cap = 20000
        input_text = text[:cap]
        cmd = [
            "opencode",
            "run",
            f"summarize this content in under {max_chars} characters: {input_text}",
        ]
        LOG.info("Summarizing content for Discord (len=%d) via opencode", len(text))
        proc = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            LOG.warning(
                "opencode summarizer failed rc=%s stderr=%r",
                getattr(proc, "returncode", None),
                getattr(proc, "stderr", None),
            )
            return text
        summary = (proc.stdout or "").strip()
        if not summary:
            return text
        return summary
    except Exception:
        LOG.exception("Failed to summarize content for Discord")
        return text


def _trim_text(value: Optional[str]) -> str:
    return value.strip() if value else ""


def _bool_meta(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _format_in_progress_items(text: str) -> List[str]:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    items: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if "- SA-" not in stripped:
            continue
        cleaned = stripped.lstrip("├└│ ")
        items.append(cleaned)
    if not items:
        for line in lines:
            stripped = line.strip()
            if stripped:
                items.append(stripped)
    return items


def _format_candidate_line(candidate: Dict[str, Any]) -> str:
    work_id = str(candidate.get("id") or "?")
    title = candidate.get("title") or candidate.get("name") or "(no title)"
    status = candidate.get("status") or candidate.get("stage") or ""
    priority = candidate.get("priority")
    parts = [f"{title} - {work_id}"]
    meta: List[str] = []
    if status:
        meta.append(f"status: {status}")
    if priority is not None:
        meta.append(f"priority: {priority}")
    if meta:
        parts.append("(" + ", ".join(meta) + ")")
    return " ".join(parts)


def _build_dry_run_report(
    *,
    in_progress_output: str,
    candidates: List[Dict[str, Any]],
    top_candidate: Optional[Dict[str, Any]],
) -> str:
    # If there are in-progress items, produce a concise, operator-friendly
    # message listing those items and skip the verbose candidate/top-candidate
    # sections. This keeps the operator-facing output short when agents are
    # actively working.
    in_progress_items = _format_in_progress_items(in_progress_output)
    if in_progress_items:
        lines: List[str] = ["Agents are currently busy with:"]
        for item in in_progress_items:
            # match the visual style requested (em dash bullets)
            lines.append(f"── {item}")
        return "\n".join(lines)

    # no in-progress items -> produce full report
    sections: List[str] = []
    sections.append("AMPA Delegation")
    sections.append("In-progress items:")
    sections.append("- (none)")

    sections.append("Candidates:")
    if candidates:
        for cand in candidates:
            sections.append(f"- {_format_candidate_line(cand)}")
    else:
        sections.append("- (none)")

    sections.append("Top candidate:")
    if top_candidate:
        sections.append(f"- {_format_candidate_line(top_candidate)}")
        sections.append("Rationale: selected by wl next (highest priority ready item).")
    else:
        sections.append("- (none)")
        sections.append("Rationale: no candidates returned by wl next.")

    if not candidates and not top_candidate:
        sections.append(
            "Summary: delegation is idle (no in-progress items or candidates)."
        )

    return "\n".join(sections)


def _build_dry_run_discord_message(report: str) -> str:
    summary = _summarize_for_discord(report, max_chars=1000)
    if summary and summary.strip():
        return summary
    LOG.warning("Dry-run discord summary was empty; falling back to raw report content")
    if report and report.strip():
        return report.strip()
    return "(no report details)"


def _build_delegation_report(
    *,
    in_progress_output: str,
    candidates: List[Dict[str, Any]],
    top_candidate: Optional[Dict[str, Any]],
) -> str:
    return _build_dry_run_report(
        in_progress_output=in_progress_output,
        candidates=candidates,
        top_candidate=top_candidate,
    )


def _build_delegation_discord_message(report: str) -> str:
    return _build_dry_run_discord_message(report)


def _normalize_work_item_payload(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    for key in ("workItem", "work_item", "item", "data"):
        val = payload.get(key)
        if isinstance(val, dict):
            return val
    return payload


def _excerpt_text(value: Optional[str], limit: int = 300) -> str:
    if not value:
        return ""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _format_assignee(value: Any) -> str:
    if not value:
        return "(none)"
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or value.get("email") or value)
    if isinstance(value, list):
        names = [
            _format_assignee(item)
            for item in value
            if item and _format_assignee(item) != "(none)"
        ]
        return ", ".join(names) if names else "(none)"
    return str(value)


def _build_work_item_markdown(item: Dict[str, Any]) -> str:
    work_id = str(
        item.get("id")
        or item.get("work_item_id")
        or item.get("workItemId")
        or "(unknown id)"
    )
    title = str(item.get("title") or item.get("name") or "(no title)")
    status = str(item.get("status") or item.get("stage") or item.get("state") or "")
    if not status:
        status = "(unknown)"
    priority = item.get("priority")
    priority_text = str(priority) if priority is not None else "(none)"
    assignee_text = _format_assignee(item.get("assignee") or item.get("owner"))
    description = item.get("description") or item.get("desc") or ""
    excerpt = _excerpt_text(str(description)) or "(none)"
    lines = [
        f"# {title} - {work_id}",
        f"- ID: {work_id}",
        f"- Status/Stage: {status}",
        f"- Priority: {priority_text}",
        f"- Assignee: {assignee_text}",
        "",
        "Description:",
        excerpt,
        "",
        "```json",
        json.dumps(item, indent=2, sort_keys=True),
        "```",
    ]
    return "\n".join(lines)


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
        # timeout (seconds) for delegation-invoked shell commands. Respect a
        # dedicated environment override `AMPA_DELEGATION_OPENCODE_TIMEOUT`; if
        # not set fall back to the general `AMPA_CMD_TIMEOUT_SECONDS` default.
        try:
            _delegate_timeout = int(
                os.getenv("AMPA_DELEGATION_OPENCODE_TIMEOUT")
                or os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "300")
            )
        except Exception:
            _delegate_timeout = 300

        def _call(cmd: str) -> subprocess.CompletedProcess:
            LOG.debug("Running shell (delegation): %s", cmd)
            return self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
                timeout=_delegate_timeout,
            )

        def _parse_in_progress(payload: Any) -> List[Dict[str, Any]]:
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                for key in ("workItems", "work_items", "items", "data"):
                    val = payload.get(key)
                    if isinstance(val, list):
                        return [item for item in val if isinstance(item, dict)]
            return []

        def _load_in_progress() -> Optional[List[Dict[str, Any]]]:
            proc = _call("wl in_progress --json")
            if proc.returncode != 0:
                LOG.warning(
                    "Delegation check failed: wl in_progress rc=%s stderr=%r",
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return None
            try:
                raw = json.loads(proc.stdout or "null")
            except Exception:
                LOG.exception(
                    "Failed to parse wl in_progress output for delegation payload=%r",
                    (proc.stdout or "")[:1024],
                )
                return None
            return _parse_in_progress(raw)

        idle_items = _load_in_progress()
        if idle_items is None:
            idle_items = _load_in_progress()
            if idle_items is None:
                return {"status": "error", "reason": "in_progress_failed"}
        if idle_items:
            return {"status": "in_progress", "items": idle_items}

        # read webhook early so idle/no-candidate branches can post a short
        # notification when appropriate. Defining it once avoids warnings
        # from static analysis and keeps behavior consistent across branches.
        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")

        candidates, payload = selection.fetch_candidates(
            run_shell=self.run_shell, command_cwd=self.command_cwd
        )
        if not candidates:
            return {"status": "idle_no_candidate", "payload": payload}
        return {"status": "idle_with_candidate", "candidate": candidates[0]}

    def _fetch_work_item_markdown(self, work_id: str) -> Optional[str]:
        cmd = f"wl show {work_id} --json"
        proc = self.run_shell(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            cwd=self.command_cwd,
        )
        if proc.returncode != 0:
            LOG.warning(
                "wl show failed for %s rc=%s stderr=%r",
                work_id,
                proc.returncode,
                (proc.stderr or "")[:512],
            )
            return None
        try:
            payload = json.loads(proc.stdout or "null")
        except Exception:
            LOG.exception("Failed to parse wl show output for %s", work_id)
            return None
        item = _normalize_work_item_payload(payload)
        if not item:
            return None
        return _build_work_item_markdown(item)

    def start_command(
        self, spec: CommandSpec, now: Optional[dt.datetime] = None
    ) -> RunResult:
        now = now or _utc_now()
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
            LOG.info(
                "Handling delegation command: %s (audit_only=%s)",
                spec.command_id,
                _bool_meta(spec.metadata.get("audit_only")),
            )
            # Inspect current state first. If there is a candidate that will be
            # dispatched we want to avoid sending the pre-dispatch report to
            # Discord (otherwise operators see two nearly-identical messages).
            audit_only = _bool_meta(spec.metadata.get("audit_only"))
            inspect = self._inspect_idle_delegation()
            status = inspect.get("status")

            # Only generate and send a pre-dispatch report when we are not
            # about to dispatch a candidate. If we will dispatch (status
            # == 'idle_with_candidate' and audit_only is false) skip the
            # pre-report; a post-dispatch report will be sent after the
            # delegation completes.
            report = None
            sent_pre_report = False
            if audit_only or status != "idle_with_candidate":
                try:
                    LOG.info(
                        "Generating pre-dispatch delegation report for %s",
                        spec.command_id,
                    )
                    report = self._run_delegation_report(spec)
                except Exception:
                    LOG.exception("Delegation report generation failed")
                if report:
                    LOG.info(
                        "Pre-dispatch delegation report generated (len=%d)", len(report)
                    )
                    output = report
                    try:
                        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                        if webhook:
                            message = _build_delegation_discord_message(report)
                            payload = webhook_module.build_command_payload(
                                os.uname().nodename,
                                run.end_ts.isoformat(),
                                spec.command_id,
                                message,
                                run.exit_code,
                                title=(
                                    spec.title
                                    or spec.metadata.get("discord_label")
                                    or "Delegation Report"
                                ),
                            )
                            webhook_module.send_webhook(
                                webhook, payload, message_type="command"
                            )
                            sent_pre_report = True
                            LOG.info(
                                "Sent pre-dispatch webhook for %s", spec.command_id
                            )
                    except Exception:
                        LOG.exception("Delegation discord notification failed")
            # if we skipped creating a pre-report, 'report' stays None and
            # 'output' remains as previously (possibly None). Proceed to
            # handling the inspected status below.

            # ensure status variable is available below
            # status may already be set above; if not, extract it
            status = inspect.get("status")
            if status == "in_progress":
                print(
                    "There is work in progress and thus no new work will be delegated."
                )
                LOG.info("Delegation skipped because work is in-progress")
                result = {
                    "note": "Delegation: skipped (in_progress items)",
                    "dispatched": False,
                    "rejected": [],
                    "idle_webhook_sent": False,
                }
            elif status == "idle_no_candidate":
                # More descriptive idle message for operators
                idle_msg = "Agents are idle: no actionable items found"
                print(idle_msg)
                LOG.info("Delegation: idle_no_candidate - %s", idle_msg)
                result = {
                    "note": "Delegation: skipped (no actionable candidates)",
                    "dispatched": False,
                    "rejected": [],
                    "idle_webhook_sent": False,
                }
                # If we did not already send a detailed pre-report, send a
                # short webhook message so Discord reflects the idle state.
                if not sent_pre_report:
                    try:
                        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                        if webhook:
                            payload = webhook_module.build_command_payload(
                                os.uname().nodename,
                                run.end_ts.isoformat(),
                                spec.command_id,
                                idle_msg,
                                0,
                                title=(
                                    spec.title
                                    or spec.metadata.get("discord_label")
                                    or "Delegation Report"
                                ),
                            )
                            webhook_module.send_webhook(
                                webhook, payload, message_type="command"
                            )
                    except Exception:
                        LOG.exception("Failed to send idle-state webhook")
            elif status == "idle_with_candidate":
                candidate = inspect.get("candidate") or {}
                delegate_id = _extract_work_item_id(candidate)
                delegate_title = _extract_work_item_title(candidate)
                markdown = (
                    self._fetch_work_item_markdown(delegate_id) if delegate_id else None
                )
                if markdown:
                    # Print the operator-facing lead line exactly, then the
                    # readable markdown summary (including fenced JSON) on the
                    # following lines without extra blank lines.
                    print("Starting work on")
                    print(markdown)
                else:
                    print(f"Starting work on: {delegate_title} - {delegate_id or '?'}")
                result = self._run_idle_delegation(audit_only=audit_only, spec=spec)
                # If delegation did not dispatch anything, ensure operators see
                # an idle-state message unless a detailed idle webhook was
                # already sent by the delegation routine.
                try:
                    note = (
                        result.get("note") if isinstance(result, dict) else str(result)
                    )
                    dispatched = bool(
                        result.get("dispatched") if isinstance(result, dict) else False
                    )
                    idle_webhook_sent = bool(
                        result.get("idle_webhook_sent")
                        if isinstance(result, dict)
                        else False
                    )
                    if not dispatched:
                        idle_msg = "Agents are idle: no actionable items found"
                        print(idle_msg)
                        # If we didn't already send a detailed pre-report or the
                        # delegation routine didn't post its detailed idle webhook,
                        # send a short idle notification so Discord reflects the
                        # current idle state.
                        if not sent_pre_report and not idle_webhook_sent:
                            webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                            if webhook:
                                try:
                                    payload = webhook_module.build_command_payload(
                                        os.uname().nodename,
                                        run.end_ts.isoformat(),
                                        spec.command_id,
                                        idle_msg,
                                        0,
                                        title=(
                                            spec.title
                                            or spec.metadata.get("discord_label")
                                            or "Delegation Report"
                                        ),
                                    )
                                    webhook_module.send_webhook(
                                        webhook, payload, message_type="command"
                                    )
                                except Exception:
                                    LOG.exception("Failed to send idle-state webhook")
                except Exception:
                    LOG.exception("Failed to handle no-actionable-candidates path")
            else:
                print("There is no candidate to delegate.")
                result = {
                    "note": "Delegation: skipped (in_progress check failed)",
                    "dispatched": False,
                    "rejected": [],
                    "idle_webhook_sent": False,
                }
            # Send a follow-up Discord notification when a delegation action
            # was actually dispatched so the Discord report reflects the
            # resulting state instead of the pre-delegation dry-run.
            try:
                webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                if webhook:
                    # If something was dispatched, re-run the report to capture
                    # the post-dispatch state and post that as an update.
                    dispatched_flag = False
                    if isinstance(result, dict):
                        dispatched_flag = bool(result.get("dispatched"))
                    if dispatched_flag:
                        try:
                            post_report = self._run_delegation_report(spec)
                            if post_report:
                                post_message = _build_delegation_discord_message(
                                    post_report
                                )
                                payload = webhook_module.build_command_payload(
                                    os.uname().nodename,
                                    run.end_ts.isoformat(),
                                    spec.command_id,
                                    post_message,
                                    run.exit_code,
                                    title=(
                                        spec.title
                                        or spec.metadata.get("discord_label")
                                        or "Delegation Report"
                                    ),
                                )
                                webhook_module.send_webhook(
                                    webhook, payload, message_type="command"
                                )
                        except Exception:
                            LOG.exception("Failed to send post-delegation webhook")
            except Exception:
                LOG.exception("Delegation webhook follow-up failed")

            # Use the structured result.note when available
            summary_note = None
            if isinstance(result, dict):
                summary_note = result.get("note")
            else:
                try:
                    summary_note = str(result)
                except Exception:
                    summary_note = None
            LOG.info("Delegation summary: %s", summary_note)
            self._record_run(spec, run, exit_code, output)
            return run
        self._record_run(spec, run, exit_code, output)
        # After recording run, perform any command-specific post actions
        if spec.command_id == "wl-triage-audit" or spec.command_type == "triage-audit":
            triage_audited = False
            try:
                # run triage-audit handler which posts WL comments and Discord summary
                # returns True if an item was audited
                triage_audited = self._run_triage_audit(spec, run, output)
            except Exception:
                LOG.exception("Triage audit post-processing failed")
                triage_audited = False
            # triage-audit posts its own discord summary; avoid generic post
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
                # Notify Discord briefly so operators see the triage run result
                try:
                    if webhook:
                        msg = "Triage found no candidates to audit"
                        payload = webhook_module.build_command_payload(
                            os.uname().nodename,
                            run.end_ts.isoformat(),
                            spec.command_id,
                            msg,
                            0,
                            title=(
                                spec.title
                                or spec.metadata.get("discord_label")
                                or "Triage Audit"
                            ),
                        )
                        webhook_module.send_webhook(
                            webhook, payload, message_type="command"
                        )
                except Exception:
                    LOG.exception("Failed to send triage-no-candidates webhook")
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
                try:
                    if webhook:
                        msg = "Triage found no candidates to audit"
                        payload = webhook_module.build_command_payload(
                            os.uname().nodename,
                            run.end_ts.isoformat(),
                            spec.command_id,
                            msg,
                            0,
                            title=(
                                spec.title
                                or spec.metadata.get("discord_label")
                                or "Triage Audit"
                            ),
                        )
                        webhook_module.send_webhook(
                            webhook, payload, message_type="command"
                        )
                except Exception:
                    LOG.exception("Failed to send triage-no-candidates webhook")
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

            # Delegation from triage disabled: previously this would call
            # `_delegate_when_idle()` which started delegation while triage ran.
            # That coupling was surprising; do not perform delegation here.

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
        """Attempt to dispatch work when agents are idle.

        Returns a dict with at least the following keys:
        - note: human-readable summary
        - dispatched: bool (True if a delegation was dispatched)
        - rejected: list of rejected candidate summaries (may be empty)
        - idle_webhook_sent: bool (True if a detailed idle webhook was posted)
        - delegate_info: optional dict with dispatch details when dispatched
        """
        if audit_only:
            return {
                "note": "Delegation: skipped (audit_only)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }

        # Ensure `webhook` is bound for all branches below to avoid static
        # analysis errors and NameError at runtime when branches reference
        # the variable before a later assignment. Other codepaths may
        # re-read the env as needed, but having a single early definition
        # avoids unbound-variable issues.
        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")

        def _call(cmd: str) -> subprocess.CompletedProcess:
            # Use the same delegation timeout as above; compute lazily in case
            # this method is called independently.
            try:
                _delegate_timeout = int(
                    os.getenv("AMPA_DELEGATION_OPENCODE_TIMEOUT")
                    or os.getenv("AMPA_CMD_TIMEOUT_SECONDS", "300")
                )
            except Exception:
                _delegate_timeout = 300
            LOG.debug("Running shell (delegation): %s", cmd)
            return self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
                timeout=_delegate_timeout,
            )

        def _parse_in_progress(payload: Any) -> List[Dict[str, Any]]:
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                for key in ("workItems", "work_items", "items", "data"):
                    val = payload.get(key)
                    if isinstance(val, list):
                        return [item for item in val if isinstance(item, dict)]
            return []

        def _load_in_progress() -> Optional[List[Dict[str, Any]]]:
            proc = _call("wl in_progress --json")
            if proc.returncode != 0:
                LOG.warning(
                    "Delegation check failed: wl in_progress rc=%s stderr=%r",
                    proc.returncode,
                    (proc.stderr or "")[:512],
                )
                return None
            try:
                raw = json.loads(proc.stdout or "null")
            except Exception:
                LOG.exception(
                    "Failed to parse wl in_progress output for delegation payload=%r",
                    (proc.stdout or "")[:1024],
                )
                return None
            return _parse_in_progress(raw)

        idle_items = _load_in_progress()
        if idle_items is None:
            idle_items = _load_in_progress()
            if idle_items is None:
                return {
                    "note": "Delegation: skipped (in_progress check failed)",
                    "dispatched": False,
                    "rejected": [],
                    "idle_webhook_sent": False,
                }
        if idle_items:
            LOG.info("Delegation skipped: in-progress items exist")
            return {
                "note": "Delegation: skipped (in_progress items)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }

        candidates, payload = selection.fetch_candidates(
            run_shell=self.run_shell, command_cwd=self.command_cwd
        )
        if not candidates:
            if payload is None:
                LOG.warning("Delegation skipped: wl next returned no payload")
            else:
                try:
                    payload_preview = json.dumps(payload)[:1024]
                except Exception:
                    payload_preview = str(payload)[:1024]
                LOG.warning(
                    "Delegation skipped: no wl next candidates payload=%r",
                    payload_preview,
                )
            # Send a short idle notification to Discord so operators see that
            # the delegation run completed but there was nothing to act on.
            idle_webhook_sent = False
            try:
                if webhook:
                    msg = "Agents are idle: nothing to delegate"
                    payload = webhook_module.build_command_payload(
                        os.uname().nodename,
                        _utc_now().isoformat(),
                        "delegation_idle",
                        msg,
                        0,
                        title=(
                            (spec.title if spec is not None else None)
                            or (
                                spec.metadata.get("discord_label")
                                if spec is not None
                                else None
                            )
                            or "Delegation: idle"
                        ),
                    )
                    webhook_module.send_webhook(
                        webhook, payload, message_type="command"
                    )
                    idle_webhook_sent = True
            except Exception:
                LOG.exception("Failed to send no-candidate idle-state webhook")
            return {
                "note": "Delegation: skipped (no wl next candidates)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": idle_webhook_sent,
            }

        # Try candidates in order until we find one with a supported stage.
        command = None
        action = ""
        delegate_id = None
        delegate_title = None
        delegate_stage = None

        # Track rejected candidates for reporting
        rejected: List[Dict[str, str]] = []

        for candidate in candidates:
            cid = _extract_work_item_id(candidate)
            if not cid:
                LOG.warning("Delegation skipping candidate with missing id")
                rejected.append(
                    {"id": "?", "title": "(unknown)", "reason": "missing id"}
                )
                continue
            ctitle = _extract_work_item_title(candidate)
            cstage = _extract_work_item_stage(candidate)

            # Honor per-item "do-not-delegate" signal
            try:
                if _is_do_not_delegate(candidate):
                    LOG.info(
                        "Delegation skipping candidate %s (%s): marked do-not-delegate",
                        cid,
                        ctitle,
                    )
                    rejected.append(
                        {"id": cid, "title": ctitle, "reason": "do-not-delegate"}
                    )
                    continue
            except Exception:
                LOG.exception("Failed to evaluate do-not-delegate for %s", cid)

            if cstage == "idea":
                # Use opencode with an explicit instruction to avoid follow-up
                # questions from the agent when performing intake.
                command = f'opencode run "/intake {cid} do not ask questions"'
                action = "intake"
            elif cstage == "intake_complete":
                command = f'opencode run "/plan {cid}"'
                action = "plan"
            elif cstage == "plan_complete":
                command = f'opencode run "work on {cid} using the implement skill"'
                action = "implement"
            else:
                # Unsupported candidate stage -> log as an error and continue to
                # the next candidate. We do not send per-candidate webhooks here
                # to avoid noisy Discord messages; a single detailed idle message
                # is emitted below if no candidate is actionable.
                LOG.error(
                    'Delegation encountered unsupported stage "%s" for %s (%s); trying next candidate',
                    cstage,
                    cid,
                    ctitle,
                )
                rejected.append(
                    {
                        "id": cid,
                        "title": ctitle,
                        "reason": f"unsupported stage '{cstage}'",
                    }
                )
                continue

            # found actionable candidate
            delegate_id = cid
            delegate_title = ctitle
            delegate_stage = cstage
            break

        if not command or not delegate_id:
            LOG.info("Delegation skipped: no actionable candidates found")
            idle_webhook_sent = False
            # Post a detailed Discord message listing rejected candidates so
            # operators can see why items were skipped (do-not-delegate, wrong
            # stage, missing id, etc.). Keep the message concise.
            try:
                if webhook and rejected:
                    lines = [
                        "Agents are idle: no actionable items found",
                        "Considered candidates:",
                    ]
                    for r in rejected:
                        lines.append(
                            f"- {r.get('id')} — {r.get('title')}: {r.get('reason')}"
                        )
                    message = "\n".join(lines)
                    payload = webhook_module.build_command_payload(
                        os.uname().nodename,
                        _utc_now().isoformat(),
                        "delegation_idle_detailed",
                        message,
                        0,
                        title=("Delegation: idle — candidates considered"),
                    )
                    webhook_module.send_webhook(
                        webhook, payload, message_type="command"
                    )
                    idle_webhook_sent = True
            except Exception:
                LOG.exception("Failed to send detailed idle-state webhook")
            return {
                "note": "Delegation: skipped (no wl next actionable candidates)",
                "dispatched": False,
                "rejected": rejected,
                "idle_webhook_sent": idle_webhook_sent,
            }

        # Before spawning opencode, record a dispatch record and notify
        LOG.info("Delegation dispatch: %s", command)
        dispatch_record = {
            "work_item_id": delegate_id,
            "action": action,
            "command": command,
            "delegate_title": delegate_title,
            "delegate_stage": delegate_stage,
            "ts": _utc_now().isoformat(),
        }
        try:
            # Persist dispatch record atomically in the scheduler store and
            # include the generated id in Discord messages so operators can
            # reference the record even if the opencode child process dies.
            store_id = None
            try:
                store_id = self.store.append_dispatch(dispatch_record)
                dispatch_record["id"] = store_id
            except Exception:
                LOG.exception("Failed to persist delegation dispatch record")
        except Exception:
            LOG.exception("Unexpected error while preparing dispatch record")

        # Send a quick notification to Discord/AMPA before spawning opencode
        try:
            if webhook:
                # Human-friendly pre-dispatch message: include action, title and id
                # Keep the message concise for Discord; do not include internal
                # dispatch record or runner metadata.
                pre_msg_lines = [
                    f"Delegating '{action}' task for '{delegate_title}' ({delegate_id})",
                ]
                pre_msg = "\n".join(pre_msg_lines)
                payload = webhook_module.build_command_payload(
                    os.uname().nodename,
                    _utc_now().isoformat(),
                    dispatch_record.get("id", "delegation_dispatch"),
                    pre_msg,
                    0,
                    title=(
                        (spec.title if spec is not None else None)
                        or (
                            spec.metadata.get("discord_label")
                            if spec is not None
                            else None
                        )
                        or "Delegation Dispatch"
                    ),
                )
                webhook_module.send_webhook(webhook, payload, message_type="dispatch")
        except Exception:
            LOG.exception("Failed to send pre-dispatch webhook")

        proc_delegate = _call(command)
        # Log opencode command response to the console for operator observability.
        try:
            out = (proc_delegate.stdout or "").strip()
            err = (proc_delegate.stderr or "").strip()
        except Exception:
            out = ""
            err = ""

        # Truncate long outputs to keep logs concise
        def _preview(s: str, limit: int = 4000) -> str:
            return s if len(s) <= limit else s[:limit] + "..."

        if out:
            LOG.info("Delegation command stdout: %s", _preview(out))
        if err:
            LOG.info("Delegation command stderr: %s", _preview(err))

        if proc_delegate.returncode != 0:
            LOG.warning(
                "Delegation dispatch failed rc=%s stderr=%s",
                proc_delegate.returncode,
                err,
            )
            return {
                "note": f"Delegation: failed ({action} {delegate_id})",
                "dispatched": False,
                "rejected": rejected,
                "idle_webhook_sent": False,
                "error": err,
            }
        LOG.info(
            "Delegation dispatched %s for %s — %s",
            action,
            delegate_id,
            delegate_title,
        )
        return {
            "note": f"Delegation: dispatched {action} {delegate_id}",
            "dispatched": True,
            "delegate_info": {
                "action": action,
                "id": delegate_id,
                "title": delegate_title,
                "stdout": out,
                "stderr": err,
            },
            "rejected": rejected,
            "idle_webhook_sent": False,
        }

    def _run_delegation_report(
        self, spec: Optional[CommandSpec] = None
    ) -> Optional[str]:
        def _call(cmd: str) -> subprocess.CompletedProcess:
            LOG.debug("Running shell (delegation): %s", cmd)
            return self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )

        in_progress_text = ""
        proc = _call("wl in_progress")
        if proc.stdout:
            in_progress_text += proc.stdout
        if proc.stderr and not in_progress_text:
            in_progress_text += proc.stderr

        candidates, _payload = selection.fetch_candidates(
            run_shell=self.run_shell, command_cwd=self.command_cwd
        )
        top_candidate = candidates[0] if candidates else None

        report = _build_delegation_report(
            in_progress_output=_trim_text(in_progress_text),
            candidates=candidates,
            top_candidate=top_candidate,
        )
        return report

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


def _extract_work_item_id(candidate: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "work_item_id", "workItemId", "workItemID"):
        val = candidate.get(key)
        if val:
            return str(val)
    return None


def _extract_work_item_title(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("title") or candidate.get("name") or "(no title)")


def _extract_work_item_stage(candidate: Dict[str, Any]) -> str:
    # Prefer an explicit 'stage' or 'state' field when determining workflow stage.
    # Do NOT fall back to the work item's 'status' (which is often a lifecycle
    # flag like 'open'/'closed') because that is a different concept and causes
    # misleading messages. Return a clear sentinel when no stage is present.
    val = candidate.get("stage") or candidate.get("state")
    if not val:
        return "undefined"
    return str(val).strip().lower()


def _is_do_not_delegate(candidate: Dict[str, Any]) -> bool:
    """Return True when a candidate declares it should not be delegated.

    Check common places where this signal may appear:
    - a `tags` list containing `do-not-delegate` (case-insensitive)
    - a metadata key `do_not_delegate` or `no_delegation` truthy value
    """
    try:
        tags = candidate.get("tags") or candidate.get("tag") or []
        if isinstance(tags, str):
            tags_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tags_list = [str(t).strip().lower() for t in tags if t]
        else:
            tags_list = []
        if "do-not-delegate" in tags_list or "do_not_delegate" in tags_list:
            return True
    except Exception:
        pass

    try:
        meta = candidate.get("metadata") or candidate.get("meta") or {}
        if isinstance(meta, dict):
            if str(meta.get("do_not_delegate", "")).strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
            ):
                return True
            if str(meta.get("no_delegation", "")).strip().lower() in (
                "1",
                "true",
                "yes",
                "y",
            ):
                return True
    except Exception:
        pass

    # explicit field support
    try:
        if candidate.get("do_not_delegate") in (True, "true", "1", 1):
            return True
    except Exception:
        pass

    return False


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
        title=getattr(args, "title", None),
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
        title=getattr(args, "title", None),
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


def _cli_dry_run(args: argparse.Namespace) -> int:
    scheduler = load_scheduler(command_cwd=os.getcwd())
    spec = CommandSpec(
        command_id="delegation",
        command="",
        requires_llm=False,
        frequency_minutes=1,
        priority=0,
        metadata={},
        title="Delegation Report",
        command_type="delegation",
    )
    if args.discord and not os.getenv("AMPA_DISCORD_WEBHOOK"):
        LOG.warning("AMPA_DISCORD_WEBHOOK not set; discord flag will be ignored")
    report = scheduler._run_delegation_report(spec)
    if report:
        print(report)
        if args.discord:
            try:
                webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                if webhook:
                    message = _build_delegation_discord_message(report)
                    payload = webhook_module.build_command_payload(
                        os.uname().nodename,
                        _utc_now().isoformat(),
                        spec.command_id,
                        message,
                        0,
                        title="Delegation Report",
                    )
                    webhook_module.send_webhook(
                        webhook, payload, message_type="command"
                    )
                else:
                    LOG.warning(
                        "AMPA_DISCORD_WEBHOOK not set; skipping discord notification"
                    )
            except Exception:
                LOG.exception("Failed to send delegation discord notification")
    return 0


def _cli_run_once(args: argparse.Namespace) -> int:
    daemon.load_env()
    scheduler = load_scheduler(command_cwd=os.getcwd())
    spec = scheduler.store.get_command(args.command_id)
    if spec is None:
        print(f"Unknown command id: {args.command_id}")
        return 2
    try:
        run = scheduler.start_command(spec)
    except Exception:
        LOG.exception("Run-once failed for %s", args.command_id)
        print(f"Run-once failed for {args.command_id}")
        return 1
    exit_code = getattr(run, "exit_code", 1)
    print(f"Run-once complete for {args.command_id} exit={exit_code}")
    return int(exit_code)


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
    add.add_argument("--title")

    update = sub.add_parser("update", help="Update a scheduled command")
    update.add_argument("command_id")
    update.add_argument("command")
    update.add_argument("frequency_minutes", type=int)
    update.add_argument("priority", type=int)
    update.add_argument("--requires-llm", action="store_true")
    update.add_argument("--metadata")
    update.add_argument("--max-runtime-minutes", type=int, dest="max_runtime_minutes")
    update.add_argument("--type", dest="command_type", default="shell")
    update.add_argument("--title")

    remove = sub.add_parser("remove", help="Remove a scheduled command")
    remove.add_argument("command_id")

    dry_run = sub.add_parser("delegation", help="Generate a delegation report")
    dry_run.add_argument("--discord", action="store_true")

    run_once = sub.add_parser("run-once", help="Run a command immediately by id")
    run_once.add_argument("command_id")

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
        "delegation": _cli_dry_run,
        "run-once": _cli_run_once,
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise SystemExit(2)
    handler(args)


if __name__ == "__main__":
    main()
