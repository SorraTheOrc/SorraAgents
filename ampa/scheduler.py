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
except ImportError:  # pragma: no cover - allow running as script
    import importlib
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    daemon = importlib.import_module("ampa.daemon")
    webhook_module = importlib.import_module("ampa.webhook")

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
                return {"commands": {}, "state": {}, "last_global_start_ts": None}
        except Exception:
            LOG.exception("Failed to read scheduler store; starting empty")
            return {"commands": {}, "state": {}, "last_global_start_ts": None}

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
        self.run_shell = run_shell or subprocess.run

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
        LOG.debug(
            "Executor starting for command_id=%s command=%r",
            spec.command_id,
            spec.command,
        )
        start_exec = _utc_now()
        run = self.executor(spec)
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
        self._record_run(spec, run, exit_code, output)
        # After recording run, perform any command-specific post actions
        try:
            # special-case triage-audit command id
            if (
                spec.command_id == "wl-triage-audit"
                or spec.command_type == "triage-audit"
            ):
                # run triage-audit handler which posts WL comments and Discord summary
                self._run_triage_audit(spec, run, output)
        except Exception:
            LOG.exception("Triage audit post-processing failed")
        # always post the generic discord message afterwards
        self._post_discord(spec, run, output)
        return run

    def _run_triage_audit(
        self, spec: CommandSpec, run: RunResult, output: Optional[str]
    ) -> None:
        """Execute triage-audit post-processing.

        This method:
        - Calls `wl in_progress --json` to get candidate work items
        - Selects the least-recently-updated work item not audited within cooldown
        - Executes `opencode run audit <work_item_id>` and captures output
        - Posts a short Discord summary and a WL comment containing the full output
        - Persists `last_audit_at` per-work-item in the scheduler store under
          state[spec.command_id]["last_audit_at_by_item"]
        """
        # configuration: allow overrides via metadata
        try:
            cooldown_hours = int(spec.metadata.get("audit_cooldown_hours", 6))
        except Exception:
            cooldown_hours = 6
        try:
            truncate_chars = int(spec.metadata.get("truncate_chars", 65536))
        except Exception:
            truncate_chars = 65536

        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")

        # helper to run shell commands via injectable runner
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

        # 1) list in_progress work items
        try:
            proc = _call("wl in_progress --json")
            if proc.returncode != 0:
                LOG.warning("wl in_progress failed: %s", proc.stderr)
                return
            items = []
            try:
                raw = json.loads(proc.stdout or "null")
            except Exception:
                LOG.exception("Failed to parse wl in_progress output")
                return
            # normalize different wl outputs: either a list or an object with a list under
            # keys like workItems, work_items, items, or data
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict):
                for key in ("workItems", "work_items", "items", "data"):
                    val = raw.get(key)
                    if isinstance(val, list):
                        items = val
                        break
                # some implementations return the list directly under 'workItems' key with
                # different casing; try a case-insensitive match
                if not items:
                    for k, v in raw.items():
                        if isinstance(v, list) and k.lower().endswith("workitems"):
                            items = v
                            break
            if not isinstance(items, list) or not items:
                LOG.debug("No in_progress items returned")
                return
            LOG.info("Found %d in_progress work item(s)", len(items))

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
            cooldown_delta = dt.timedelta(hours=cooldown_hours)

            # filter out items audited within cooldown by inspecting WL comments
            candidates: List[Tuple[Optional[dt.datetime], Dict[str, Any]]] = []
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
                            for key in (
                                "created_at",
                                "created_ts",
                                "created",
                                "ts",
                                "timestamp",
                            ):
                                v = c.get(key)
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

                if last_audit is not None and (now - last_audit) < cooldown_delta:
                    # skip - recently audited
                    continue

                updated = _item_updated_ts(it)
                candidates.append((updated, {**it, "id": wid}))

            if not candidates:
                LOG.debug("No triage candidates after cooldown filter")
                return

            # sort by updated timestamp ascending (oldest first), None treated as oldest
            candidates.sort(
                key=lambda t: (
                    t[0] is not None,
                    t[0] or dt.datetime.fromtimestamp(0, dt.timezone.utc),
                )
            )
            selected = candidates[0][1]
            work_id = selected.get("id")
            title = selected.get("title") or selected.get("name") or "(no title)"
            # record selected candidate for easier observability
            LOG.info("Selected triage candidate %s — %s", work_id, title)

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

            # 3) post a short Discord summary (1-3 lines)
            if webhook:
                # extract the "Summary" section from the audit output if present
                def _extract_summary(text: str) -> str:
                    if not text:
                        return ""
                    # look for a heading-style or standalone 'Summary' line
                    m = re.search(
                        r"^(?:#{1,6}\s*)?Summary\s*:?$",
                        text,
                        re.IGNORECASE | re.MULTILINE,
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

                summary_text = _extract_summary(audit_out or "")
                if not summary_text:
                    # fallback simple summary
                    summary_text = f"{work_id} — {title} | exit={exit_code}"
                # if Discord will reject long messages, summarize to <=1000 chars
                try:
                    summary_text = _summarize_for_discord(summary_text, max_chars=1000)
                except Exception:
                    LOG.exception("Failed to summarize triage summary_text")
                try:
                    # Build a simple markdown-style message for Discord:
                    #
                    # # <command-run> <work-item-title>
                    #
                    # <output>
                    command_run = f"/audit {work_id}"
                    content = f"# {command_run} {title}\n\n{summary_text}"
                    payload = {"content": content}
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
                    comment = f"# AMPA Audit Result\n\nAudit output:\n\n{comment_text}"
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
            else:
                # write artifact to temp file and post comment referencing it
                try:
                    fd, path = tempfile.mkstemp(
                        prefix=f"wl-audit-{work_id}-", suffix=".log"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(full_output)
                    comment = f"# AMPA Audit Result\n\nAudit output too large; full output saved to: {path}"
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
            # After posting the audit comment, check whether the work item can be
            # auto-completed. Criteria (both required):
            #  - Evidence of a merged PR (either a GitHub PR URL in the audit output
            #    or a textual 'PR merged' token), and
            #  - No open/in_progress child work items (or the audit explicitly
            #    states the item is ready to close).
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
                                command_run = f"/audit {work_id}"
                                content = f"# {command_run} {title}\n\n{(audit_out or '')[:1000]}"
                                payload = {"content": content}
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
            title="AMPA Scheduler",
        )
        webhook_module.send_webhook(webhook, payload, message_type="command")

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
        payload = webhook_module.build_command_payload(
            hostname,
            ts,
            "scheduler_start",
            "Scheduler started",
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
