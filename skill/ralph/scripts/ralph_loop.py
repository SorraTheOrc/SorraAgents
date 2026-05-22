#!/usr/bin/env python3
"""Ralph orchestration loop.

Implements an iterative implement->audit->remediate loop for a target work item.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Callable, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.structured_response import StructuredResponse, parse_structured_response
from skill.test_runner import canonicalize_quiet_test_command

logger = logging.getLogger("ralph")

DEFAULT_MODEL = "opencode-go/glm-5.1"
DEFAULT_MODEL_SOURCE = "remote"
MODEL_SOURCES = frozenset({"remote", "local"})
MODEL_PHASES: tuple[str, ...] = ("intake", "planning", "implementation", "audit")
PHASE_MODEL_DEFAULTS: dict[str, dict[str, str]] = {
    "remote": {
        "intake": "Claude Opus 4.7",
        "planning": "GPT 5.5",
        "implementation": "Qwen 3.6 Plus",
        "audit": "Claude Opus 4.7",
    },
    "local": {
        "intake": "Llama-3.1 70B (Q4_K_M)",
        "planning": "Qwen 3.x 32B",
        "implementation": "Qwen 32B",
        "audit": "Llama-3.1 70B (Q4_K_M)",
    },
}
DEBUG_PAYLOAD_DIR_NAME = "ralph-payloads"
DEBUG_PAYLOAD_MAX_BYTES = 1_000_000
DEBUG_PAYLOAD_MAX_FILES = 200
DEBUG_PAYLOAD_TTL_SECONDS = 7 * 24 * 60 * 60
RALPH_CONFIG_FILES = [
    Path(".ralph.toml"),
    Path(".ralph.json"),
    Path("ralph.config.toml"),
    Path("ralph.config.json"),
]


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class RalphError(RuntimeError):
    """Raised for orchestrator failures."""


@dataclass
class CriterionResult:
    text: str
    verdict: str
    evidence: str


@dataclass
class AuditParseResult:
    ready_to_close: bool
    criteria: list[CriterionResult]

    @property
    def unmet_or_partial(self) -> list[CriterionResult]:
        return [c for c in self.criteria if c.verdict in {"unmet", "partial"}]


class JsonLineFormatter(logging.Formatter):
    """Emit log records as JSON lines and preserve structured extras.

    The formatter keeps the canonical `msg` field for human-readable text and
    serializes any extra fields attached to the log record so callers can
    inspect delegated command details machine-readably.
    """

    _STANDARD_KEYS = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._STANDARD_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = str(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _run_json(runner: Runner, cmd: Sequence[str]) -> dict:
    proc = runner(cmd)
    if proc.returncode != 0:
        raise RalphError(f"Command failed ({' '.join(cmd)}): {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RalphError(f"Invalid JSON from {' '.join(cmd)}: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RalphError(f"Worklog command failed ({' '.join(cmd)}): {data.get('error', 'unknown error')}")
    return data


def parse_audit_report(report_text: str) -> AuditParseResult:
    lines = report_text.splitlines()
    ready = any(line.strip().lower().startswith("ready to close: yes") for line in lines)
    criteria: list[CriterionResult] = []
    for line in lines:
        striped = line.strip()
        if not striped.startswith("|"):
            continue
        parts = [p.strip() for p in striped.strip("|").split("|")]
        if len(parts) != 4:
            continue
        if parts[0] in {"#", "---"}:
            continue
        verdict = parts[2].lower()
        if verdict not in {"met", "unmet", "partial"}:
            continue
        criteria.append(CriterionResult(text=parts[1], verdict=verdict, evidence=parts[3]))
    return AuditParseResult(ready_to_close=ready, criteria=criteria)


# Audit sanitization removed: ralph now relies on the audit persisted by the audit skill in the work item (wl show).
# The audit skill is responsible for producing the canonical structured
# report (starting with "Ready to close:") and storing it on the work item.
# Ralph will read that persisted audit and fail if it is missing or invalid.


def _load_config() -> dict:
    """Load config from the first found config file in the list.

    Supports JSON files only. TOML support can be added when tomllib is
    available (Python 3.11+ stdlib). For now, .ralph.json and
    ralph.config.json are the supported config files.
    """
    for path in RALPH_CONFIG_FILES:
        if not path.exists():
            continue
        if path.suffix == ".json":
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                logger.debug("ralph.config: failed to load %s", path)
    return {}


def _resolve_model(cli_model: str | None, config_model: str | None) -> str:
    """Resolve the legacy single model: CLI flag > config file > default."""
    if cli_model:
        return cli_model
    if config_model:
        return config_model
    return DEFAULT_MODEL


def _normalize_model_source(source: str | None) -> str:
    if not source:
        return DEFAULT_MODEL_SOURCE
    normalized = str(source).strip().lower()
    if normalized in MODEL_SOURCES:
        return normalized
    return DEFAULT_MODEL_SOURCE


def _coerce_model_str(value: object) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return None


def _resolve_phase_model_value(value: object, model_source: str) -> str | None:
    """Resolve a model value that may be a string or source-mapped object."""
    direct = _coerce_model_str(value)
    if direct:
        return direct
    if isinstance(value, dict):
        source_value = _coerce_model_str(value.get(model_source))
        if source_value:
            return source_value
    return None


def _extract_phase_model_config(config: dict) -> dict[str, object]:
    """Extract per-phase model config from nested or dotted config keys."""
    phase_config: dict[str, object] = {}
    model_root = config.get("model")

    for phase in MODEL_PHASES:
        dotted_key = config.get(f"model.{phase}")
        if dotted_key is not None:
            phase_config[phase] = dotted_key
            continue

        direct_remote = config.get(f"model.remote.{phase}")
        direct_local = config.get(f"model.local.{phase}")
        if direct_remote is not None or direct_local is not None:
            source_map: dict[str, object] = {}
            if direct_remote is not None:
                source_map["remote"] = direct_remote
            if direct_local is not None:
                source_map["local"] = direct_local
            phase_config[phase] = source_map
            continue

        if isinstance(model_root, dict):
            if phase in model_root:
                phase_config[phase] = model_root[phase]
                continue

            remote_map = model_root.get("remote")
            local_map = model_root.get("local")
            if isinstance(remote_map, dict) or isinstance(local_map, dict):
                source_map: dict[str, object] = {}
                if isinstance(remote_map, dict) and phase in remote_map:
                    source_map["remote"] = remote_map[phase]
                if isinstance(local_map, dict) and phase in local_map:
                    source_map["local"] = local_map[phase]
                if source_map:
                    phase_config[phase] = source_map

    return phase_config


def _extract_legacy_model_from_config(config: dict) -> str | None:
    model_value = config.get("model")
    if isinstance(model_value, str):
        trimmed = model_value.strip()
        return trimmed or None
    return None


def _render_command(cmd: Sequence[str]) -> str:
    return shlex.join(list(cmd))


def _safe_filename_component(value: str | None, fallback: str = "unknown") -> str:
    if not value:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned or fallback


def _extract_pi_session_id(raw_output: str) -> str | None:
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "session":
            continue
        for key in ("id", "session_id", "sessionId"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _prune_debug_payloads(payload_dir: Path) -> None:
    try:
        now = time.time()
        files: list[Path] = []
        for path in payload_dir.glob("*.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > DEBUG_PAYLOAD_TTL_SECONDS:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            files.append(path)
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        while len(files) > DEBUG_PAYLOAD_MAX_FILES:
            path = files.pop(0)
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        logger.debug("ralph.cmd.pi.debug_payload_prune_failed dir=%s", payload_dir)


def _persist_debug_payload(raw_output: str, metadata: dict[str, object]) -> Path | None:
    if not raw_output:
        return None
    payload_dir = Path(tempfile.gettempdir()) / DEBUG_PAYLOAD_DIR_NAME
    payload_dir.mkdir(parents=True, exist_ok=True)
    _prune_debug_payloads(payload_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    child_label = _safe_filename_component(str(metadata.get("child_id") or metadata.get("focus_id") or metadata.get("target_id")))
    session_id = _extract_pi_session_id(raw_output)
    payload = {
        "metadata": {
            **metadata,
            "session_id": session_id,
            "persisted_at": datetime.now(timezone.utc).isoformat(),
        },
        "raw_output": raw_output[:DEBUG_PAYLOAD_MAX_BYTES],
        "truncated": len(raw_output) > DEBUG_PAYLOAD_MAX_BYTES,
    }
    filename = f"{timestamp}-{child_label}.json"
    tmp_path = payload_dir / f".{filename}.{os.getpid()}.tmp"
    final_path = payload_dir / filename
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, final_path)
    return final_path


def _build_remediation_prompt() -> str:
    """Build a prompt for the implement step that addresses audit failures."""
    return "The previous audit found issues. Address all the gaps identified in the audit."


def _build_implement_prompt(work_item_id: str, remediation: str = "") -> str:
    """Build the non-interactive implement prompt for Ralph.

    The implement step must never ask the producer questions during the
    default loop. If the model cannot continue safely, it must return a
    structured no_safe_path response that names the missing producer decision.
    """
    parts = [
        f"implement {work_item_id}",
        "Continue until the work item and all dependencies are completed, but do not merge.",
        "Do not ask the producer questions or pause for interactive input.",
        "If you cannot continue safely without explicit producer input, stop and return a structured no_safe_path response with the missing decision.",
    ]
    if remediation:
        parts.append(remediation)
    return "\n".join(parts)


def _extract_text_from_content(content: object) -> str | None:
    """Recursively extract user-facing text from a JSON content payload."""
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = _extract_text_from_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    if isinstance(content, dict):
        if content.get("type") == "text":
            text = content.get("text")
            if isinstance(text, str) and text:
                return text
        for key in ("text", "content", "delta"):
            value = content.get(key)
            if value is not None:
                text = _extract_text_from_content(value)
                if text:
                    return text
    return None


def _extract_text_from_assistant_message(message: object) -> str | None:
    """Extract text from an assistant message payload when present."""
    if not isinstance(message, dict):
        return None
    if message.get("role") != "assistant":
        return None
    return _extract_text_from_content(message.get("content"))


def _extract_last_assistant_message_text(messages: object) -> str | None:
    """Return the last assistant message text from a message list."""
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        text = _extract_text_from_assistant_message(message)
        if text:
            return text
    return None


def _parse_pi_json_line(line: str) -> tuple[str, bool, str | None]:
    """Parse a single JSON line from pi --mode json and extract user-facing text.

    Pi's JSON streaming protocol uses typed events:
    - thinking_start/thinking_delta/thinking_end: internal reasoning (suppressed)
    - text_delta: additive user-facing text (shown on console)
    - text_end: complete content block (captured for return value)
    - toolcall_start/delta/end: tool calls (suppressed)
    - tool_execution_*: tool results (suppressed)
    - message_start/message_end/turn_end/agent_end: may include final assistant
      message content in structured JSON form
    - session/agent_start/turn_start: structural metadata

    For streaming, text_delta events are printed additively.
    For the return value, text_end, message_end, turn_end, and agent_end
    events can provide complete text blocks that replace any accumulated
    deltas for that content index.

    Returns a tuple of (stream_text, should_print, complete_text):
    - stream_text: text to print to console (additive delta for streaming)
    - should_print: True if stream_text should be shown on console
    - complete_text: if not None, a COMPLETE content block to use for the
      final return value (replaces accumulated deltas for this content)
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None, False, None
    if not isinstance(obj, dict):
        return None, False, None

    event_type = obj.get("type", "")

    # --- Streaming message_update events (primary streaming path) ---
    if event_type == "message_update":
        assistant_event = obj.get("assistantMessageEvent")
        if isinstance(assistant_event, dict):
            inner_type = assistant_event.get("type", "")
            # Text delta: additive user-facing text — stream to console
            if inner_type == "text_delta":
                delta = assistant_event.get("delta", "")
                if isinstance(delta, str) and delta:
                    return delta, True, None
                return "", False, None
            # Text end: complete content block — capture for return value
            if inner_type == "text_end":
                content = assistant_event.get("content", "")
                if isinstance(content, str) and content:
                    return "", False, content
                return "", False, None
            # Thinking events: suppress entirely
            if inner_type in ("thinking_start", "thinking_delta", "thinking_end"):
                return "", False, None
            # Tool call events: suppress
            if inner_type in ("toolcall_start", "toolcall_delta", "toolcall_end"):
                return "", False, None
            # text_start: structural — suppress (would duplicate delta)
            if inner_type == "text_start":
                return "", False, None
            # Some pi versions place the assistant's completed response inside
            # a structured payload rather than a dedicated text_end event.
            content_text = _extract_text_from_content(assistant_event.get("content"))
            if content_text:
                return "", False, content_text
            # Other assistant events — suppress
            return "", False, None
        return "", False, None

    # --- Message events: final assistant message content ---
    if event_type in {"message_start", "message_end", "turn_end"}:
        message = obj.get("message")
        text = _extract_text_from_assistant_message(message)
        if text:
            return "", False, text
        return "", False, None

    # --- Agent end: final message with complete content ---
    # Only extract text from the LAST assistant message — this is the final,
    # authoritative response. Earlier assistant messages may contain tool calls
    # or intermediate text that should not be included in the audit output.
    if event_type == "agent_end":
        text = _extract_last_assistant_message_text(obj.get("messages"))
        if text:
            return "", False, text
        return "", False, None

    # --- Structural events: suppress all ---
    if event_type in (
        "session", "agent_start", "turn_start",
    ):
        return "", False, None

    # --- Tool execution events: suppress ---
    if event_type in ("tool_execution_start", "tool_execution_update", "tool_execution_end"):
        return "", False, None

    # --- Fallback: unknown JSON event types ---
    for key in ("content", "text", "delta"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val, True, None
        if isinstance(val, dict):
            for inner_key in ("text", "content"):
                inner_val = val.get(inner_key)
                if isinstance(inner_val, str) and inner_val:
                    return inner_val, True, None
    # Valid JSON but no extractable user text
    return "", False, None


def _extract_text_from_json_output(raw: str) -> str:
    """Extract the full user-facing text from pi --mode json -p output.

    Uses text_end and agent_end events (which contain complete content blocks)
    as the primary source. Falls back to text_delta accumulation. If neither is
    found, returns the raw output.
    """
    delta_parts: list[str] = []
    complete_blocks: list[str] = []
    found_json = False
    has_agent_end = False

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stream_text, should_print, complete_text = _parse_pi_json_line(stripped)
        if stream_text is None and complete_text is None:
            # Not valid JSON — skip during JSON parsing
            continue
        found_json = True
        if complete_text is not None:
            complete_blocks.append(complete_text)
            # Check if this came from an agent_end event — it contains the
            # complete final messages and is the most authoritative source
            try:
                obj = json.loads(stripped)
                if obj.get("type") == "agent_end":
                    has_agent_end = True
            except (json.JSONDecodeError, ValueError):
                pass
        elif should_print and stream_text:
            delta_parts.append(stream_text)

    # If we got an agent_end with complete text, use it — it's the final
    # authoritative response containing only the last assistant message's text.
    if has_agent_end and complete_blocks:
        # agent_end returns the last assistant message, which is the audit report
        return complete_blocks[-1]

    # If we got text_end events with complete blocks, use only the LAST one —
    # it's the final response. Earlier blocks may be intermediate text.
    if complete_blocks:
        return complete_blocks[-1]

    # Fall back to accumulated deltas
    if delta_parts:
        return "".join(delta_parts)

    # No text content extracted from JSON
    if found_json:
        return ""
    return raw


def _extract_text_and_structured_response_from_json_output(raw: str) -> tuple[str, StructuredResponse | None]:
    """Return plain text if available, otherwise a structured fallback.

    The structured fallback preserves the user-facing summary text and the
    extracted action list so Ralph can reuse them when constructing the next
    remediation prompt.
    """
    text = _extract_text_from_json_output(raw)
    if text:
        return text, None
    structured = parse_structured_response(raw)
    if structured:
        return structured.render(), structured
    return text, None


# Default thresholds for auto-plan decision
# If effort t-shirt is in this set AND risk level is in the risk set,
# skip /plan and proceed directly to implement.
DEFAULT_AUTOPLAN_EFFORT_SKIP: frozenset[str] = frozenset({"Extra Small", "Small"})
DEFAULT_AUTOPLAN_RISK_SKIP: frozenset[str] = frozenset({"Low"})




class RalphLoop:
    def __init__(
        self,
        runner: Runner | None = None,
        pi_bin: str = "pi",
        wl_bin: str = "wl",
        model: str | None = None,
        model_source: str | None = None,
        model_intake: str | None = None,
        model_planning: str | None = None,
        model_implementation: str | None = None,
        model_audit: str | None = None,
        model_config: dict[str, object] | None = None,
        model_source_explicit: bool | None = None,
        legacy_model_explicit: bool | None = None,
        check_cmds: list[str] | None = None,
        max_attempts: int = 10,
        confirm_merge: bool = False,
        cancel_file: str | None = None,
        verbose: bool = False,
        stream: bool = True,
        autoplan_effort_skip: frozenset[str] | None = None,
        autoplan_risk_skip: frozenset[str] | None = None,
        fail_open: bool = False,
        retry: int = 0,
        retry_delay: float = 0.0,
        fatal_cmds: list[str] | None = None,
        debug_persist: bool = False,
    ):
        self.runner = runner or _default_runner
        self.pi_bin = pi_bin
        self.wl_bin = wl_bin
        self.model = model or DEFAULT_MODEL
        self.legacy_model_explicit = (model is not None) if legacy_model_explicit is None else legacy_model_explicit
        self.model_source = _normalize_model_source(model_source)
        self.model_source_explicit = (model_source is not None) if model_source_explicit is None else model_source_explicit
        self.model_overrides: dict[str, str | None] = {
            "intake": model_intake,
            "planning": model_planning,
            "implementation": model_implementation,
            "audit": model_audit,
        }
        self.model_config = model_config or {}
        self.phase_model_mode_enabled = self.model_source_explicit or any(
            _coerce_model_str(value) is not None for value in self.model_overrides.values()
        ) or any(phase in self.model_config for phase in MODEL_PHASES)
        self.max_attempts = max_attempts
        self.confirm_merge = confirm_merge
        self.cancel_file = cancel_file
        self.check_cmds = check_cmds or []
        self.verbose = verbose
        # When stream=True (default for production), pi subprocess output is
        # echoed to stdout in real-time. When stream=False (tests), the mock
        # runner is used instead.
        self.stream = stream
        # Auto-plan thresholds: effort t-shirt sizes and risk levels that
        # allow skipping /plan and proceeding directly to implement.
        self.autoplan_effort_skip = autoplan_effort_skip or DEFAULT_AUTOPLAN_EFFORT_SKIP
        self.autoplan_risk_skip = autoplan_risk_skip or DEFAULT_AUTOPLAN_RISK_SKIP
        # When True, disable the auto-plan step and proceed directly to implement
        # for intake_complete items.
        self.no_autoplan = False
        # Fail-open and retry configuration for delegated subprocess calls
        self.fail_open = fail_open
        # Number of additional attempts (in addition to initial run) on failure
        self.retry = int(retry or 0)
        # Delay between retries (seconds)
        self.retry_delay = float(retry_delay or 0.0)
        # Command categories that are always treated as fatal even when fail-open
        # By default, treat merge, checks, and pi runs as fatal. Worklog (wl)
        # and other orchestration helpers are non-fatal by default but can be
        # marked fatal via --fatal-cmd.
        self.fatal_cmds = set(fatal_cmds) if fatal_cmds else {"merge", "check", "pi"}
        # Certain categories are intentionally non-fatal by default (e.g.
        # the effort-and-risk orchestrator returns None on failure rather than
        # raising an exception).
        self.non_fatal_by_default = {"effort_and_risk"}
        self.debug_persist = debug_persist
        # Watchdog used by streamed pi runs so a stuck stdout pipe fails fast
        # instead of blocking the orchestration loop forever.
        self.pi_stream_timeout_seconds = 60.0
        self._debug_context: dict[str, object] = {}
        self._last_structured_response: StructuredResponse | None = None

    def _resolve_model_for_phase(self, phase: str) -> str:
        if phase not in MODEL_PHASES:
            raise RalphError(f"Unknown model phase: {phase}")

        override_value = _coerce_model_str(self.model_overrides.get(phase))
        if override_value:
            return override_value

        config_value = self.model_config.get(phase)
        configured_phase_model = _resolve_phase_model_value(config_value, self.model_source)
        if configured_phase_model:
            return configured_phase_model

        if self.legacy_model_explicit:
            return self.model

        if self.phase_model_mode_enabled:
            return PHASE_MODEL_DEFAULTS[self.model_source][phase]

        return DEFAULT_MODEL

    def _call_runner(self, cmd: Sequence[str], input_data: str | None = None) -> subprocess.CompletedProcess:
        """Invoke the configured runner in a consistent way.

        - If the default runner is in use, call subprocess.run with text/capture.
        - If a custom runner is provided, call it with the command list. If
          input_data is provided and a custom runner is used, append the input
          as a trailing argument to preserve legacy behavior used by tests.
        """
        if self.runner == _default_runner:
            return subprocess.run(cmd, input=input_data, text=True, capture_output=True)
        # Custom runner: if input_data provided, append it to the args to match
        # previous convention where tests passed payload as a trailing argument.
        if input_data is not None:
            return self.runner(list(cmd) + [input_data])
        return self.runner(list(cmd))

    def _call_with_retry(self, cmd: Sequence[str], category: str | None = None, expect_json: bool = False, input_data: str | None = None):
        """Call the given command with retry and fail-open semantics.

        Returns:
        - If expect_json=True: a parsed JSON dict on success, or an empty dict on
          fail-open fallback.
        - If expect_json=False: a subprocess.CompletedProcess on success, or the
          last CompletedProcess on fail-open fallback.

        Raises RalphError on fatal failures.
        """
        attempts = 0
        last_proc: subprocess.CompletedProcess | None = None
        while True:
            if category in {"wl", "pi"}:
                rendered = _render_command(cmd)
                logger.info(
                    "ralph.cmd.%s.execute cmd=%s",
                    category,
                    rendered,
                    extra={"category": category, "cmd": rendered, "argv": list(cmd), "attempt": attempts + 1},
                )
            proc = self._call_runner(list(cmd), input_data=input_data)
            last_proc = proc
            # Basic success check
            ok = getattr(proc, "returncode", 0) == 0
            parsed: dict | None = None
            if expect_json and ok:
                try:
                    parsed = json.loads(proc.stdout)
                except Exception:
                    ok = False
            # If JSON response indicates failure (worklog style), treat as error
            if expect_json and parsed is not None and isinstance(parsed, dict) and parsed.get("success") is False:
                ok = False
            if ok:
                if expect_json:
                    return parsed if parsed is not None else {}
                return proc
            # Failure case
            attempts += 1
            if attempts <= self.retry:
                logger.info("ralph.cmd.retry attempt=%d cmd=%s", attempts, cmd)
                time.sleep(self.retry_delay)
                continue
            # Exhausted retries
            break

        # If the category is explicitly non-fatal by default, return a
        # non-raising fallback to allow the loop to continue. This covers
        # orchestrators like effort-and-risk which are handled specially.
        if category and category in self.non_fatal_by_default:
            if expect_json:
                logger.warning(
                    "ralph.cmd.nonfatal_by_default category=%s cmd=%s rc=%s stderr=%s",
                    category, cmd, getattr(last_proc, "returncode", None), (getattr(last_proc, "stderr", "") or getattr(last_proc, "stdout", ""))[:1000],
                )
                return {}
            logger.warning(
                "ralph.cmd.nonfatal_by_default category=%s cmd=%s rc=%s stderr=%s",
                category, cmd, getattr(last_proc, "returncode", None), (getattr(last_proc, "stderr", "") or getattr(last_proc, "stdout", ""))[:1000],
            )
            return last_proc

        # If fail-open is enabled and this category is not fatal, return a
        # non-raising fallback to allow the loop to continue.
        if self.fail_open and (not category or category not in self.fatal_cmds):
            if expect_json:
                logger.warning(
                    "ralph.cmd.failed_but_fail_open category=%s cmd=%s rc=%s stderr=%s",
                    category, cmd, getattr(last_proc, "returncode", None), (getattr(last_proc, "stderr", "") or getattr(last_proc, "stdout", ""))[:1000],
                )
                return {}
            logger.warning(
                "ralph.cmd.failed_but_fail_open category=%s cmd=%s rc=%s stderr=%s",
                category, cmd, getattr(last_proc, "returncode", None), (getattr(last_proc, "stderr", "") or getattr(last_proc, "stdout", ""))[:1000],
            )
            return last_proc

        # Fatal: raise an error to preserve previous default behaviour
        stderr = (getattr(last_proc, "stderr", None) or getattr(last_proc, "stdout", ""))
        # Construct category-specific messages to preserve backwards-compatible
        # error text expected by tests and callers.
        cat = (category or "").lower()
        if cat == "merge":
            raise RalphError(f"Merge step failed ({' '.join(cmd)}): {str(stderr).strip()}")
        if cat == "check":
            raise RalphError(f"Check failed ({' '.join(cmd)}): {str(stderr).strip()}")
        if cat == "wl":
            raise RalphError(f"Worklog command failed ({' '.join(cmd)}): {str(stderr).strip()}")
        if cat == "pi":
            raise RalphError(f"pi run failed: {str(stderr).strip()}")
        # Fallback generic message
        raise RalphError(f"Command failed ({' '.join(cmd)}): {str(stderr).strip()}")

    def _wl_show(self, work_item_id: str, children: bool = False) -> dict:
        cmd = [self.wl_bin, "show", work_item_id, "--json"]
        if children:
            cmd.insert(3, "--children")
        logger.debug("ralph.cmd.wl.show cmd=%s", cmd)
        result = self._call_with_retry(cmd, category="wl", expect_json=True)
        if self.verbose:
            item = (result or {}).get("workItem", {}) if isinstance(result, dict) else {}
            logger.debug("ralph.cmd.wl.show id=%s stage=%s status=%s children=%d", item.get("id"), item.get("stage"), item.get("status"), len((result or {}).get("children", [])))
        return result or {}

    def _wl_comment_list(self, work_item_id: str) -> list[dict]:
        cmd = [self.wl_bin, "comment", "list", work_item_id, "--json"]
        logger.debug("ralph.cmd.wl.comment_list cmd=%s", cmd)
        data = self._call_with_retry(cmd, category="wl", expect_json=True)
        comments = data.get("comments", []) if isinstance(data, dict) else []
        if self.verbose:
            logger.debug("ralph.cmd.wl.comment_list count=%d", len(comments))
        return comments

    # Maximum argument size before we switch to temp-file or truncation (bytes)
    # Linux ARG_MAX is typically 2MB, but we use a conservative threshold
    # to leave headroom for other args, env vars, etc.
    _MAX_ARG_LEN = 100_000  # 100 KB

    def _wl_comment_add(self, work_item_id: str, comment: str) -> None:
        if len(comment) > self._MAX_ARG_LEN:
            logger.info(
                "ralph.cmd.wl.comment_add target=%s comment_len=%d truncating_to=%d",
                work_item_id, len(comment), self._MAX_ARG_LEN,
            )
            comment = comment[: self._MAX_ARG_LEN] + "\n\n... [comment truncated; full audit text is stored in the work item audit field]"
        cmd = [
            self.wl_bin,
            "comment",
            "add",
            work_item_id,
            "--author",
            "ralph",
            "--comment",
            comment,
            "--json",
        ]
        logger.debug("ralph.cmd.wl.comment_add target=%s comment_len=%d", work_item_id, len(comment))
        if self.verbose:
            logger.debug("ralph.cmd.wl.comment_add comment_start=%s", comment[:500])
        # Use wrapper to allow fail-open/retry behaviour
        self._call_with_retry(cmd, category="wl", expect_json=True)


    def _run_pi(self, prompt: str, phase: str = "implementation") -> str:
        # Use an ephemeral session for each orchestration call so nested or
        # retried runs never attempt to continue a previous assistant turn.
        model_for_phase = self._resolve_model_for_phase(phase)
        cmd = [self.pi_bin, "-p", "--no-session", "--mode", "json", "--model", model_for_phase, prompt]
        logger.debug("ralph.cmd.pi.run phase=%s model=%s prompt_len=%d", phase, model_for_phase, len(prompt))
        if self.verbose:
            logger.debug("ralph.cmd.pi.run prompt_full=\n%s", prompt)

        self._last_structured_response = None
        if self.stream:
            return self._stream_pi(cmd, prompt, model_for_phase)

        proc = self._call_with_retry(cmd, category="pi", expect_json=False)
        if getattr(proc, "returncode", 0) != 0:
            if self.fail_open and ("pi" not in self.fatal_cmds):
                logger.warning("ralph.cmd.pi.failed_but_fail_open cmd=%s rc=%s stderr=%s", cmd, getattr(proc, "returncode", None), (getattr(proc, "stderr", "") or "").strip())
                return ""
            if self.verbose:
                logger.debug("ralph.cmd.pi.run stderr=%s", (getattr(proc, "stderr", "") or "").strip()[:1000])
            raise RalphError(f"pi run failed: {(getattr(proc, 'stderr', '') or '').strip()}")
        self._last_structured_response = None
        text, structured = _extract_text_and_structured_response_from_json_output(getattr(proc, "stdout", "") or "")
        self._last_structured_response = structured
        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(text), text[:1000])
        return text


    def _stream_pi(self, cmd: list[str], prompt: str, model_for_phase: str | None = None) -> str:
        """Run pi with --mode json and stream user-facing text to the console.

        Only text_delta events (the agent's actual response) are printed.
        Thinking, metadata, and structural events are suppressed.
        Non-JSON lines are printed as a fallback.
        In verbose mode, raw JSON lines are also logged at DEBUG level.
        A stdout inactivity watchdog prevents a delegated pi process from
        hanging Ralph forever when it keeps the pipe open without finishing.
        """
        self._last_structured_response = None
        if model_for_phase is None:
            model_for_phase = self._resolve_model_for_phase("implementation")
        rendered = _render_command(cmd)
        logger.info(
            "ralph.cmd.pi.execute cmd=%s",
            rendered,
            extra={"category": "pi", "cmd": rendered, "argv": list(cmd), "attempt": 1},
        )
        logger.info("ralph.cmd.pi.stream_start model=%s cmd_len=%d", model_for_phase, len(cmd))
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise RalphError(f"pi binary not found: {self.pi_bin}")

        text_parts: list[str] = []
        complete_blocks: list[str] = []
        raw_output_parts: list[str] = []
        stderr_parts: list[str] = []
        json_lines_seen = 0
        text_lines_seen = 0
        stdout_queue: Queue[object] = Queue()
        eof_marker = object()
        stream_error: BaseException | None = None
        stalled_reason: str | None = None

        def _mark_stalled(reason: str) -> None:
            nonlocal stalled_reason
            stalled_reason = reason
            stderr_snapshot = "".join(stderr_parts).strip()
            logger.warning(
                "ralph.cmd.pi.stream_stalled timeout=%.1f reason=%s cmd=%s stderr=%s",
                self.pi_stream_timeout_seconds,
                reason,
                rendered,
                stderr_snapshot[:1000],
            )
            try:
                process.kill()
            except Exception:
                pass

        def _read_stdout() -> None:
            try:
                while True:
                    line = process.stdout.readline()
                    if line == "":
                        break
                    stdout_queue.put(line)
            except BaseException as exc:
                stdout_queue.put(exc)
            finally:
                stdout_queue.put(eof_marker)

        def _read_stderr() -> None:
            try:
                while True:
                    chunk = process.stderr.read(1024)
                    if not chunk:
                        break
                    stderr_parts.append(chunk)
            except BaseException as exc:
                logger.debug("ralph.cmd.pi.stderr_reader_error error=%s", exc)

        stdout_thread = Thread(target=_read_stdout, name="ralph-pi-stdout", daemon=True)
        stderr_thread = Thread(target=_read_stderr, name="ralph-pi-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            while True:
                try:
                    item = stdout_queue.get(timeout=self.pi_stream_timeout_seconds)
                except Empty:
                    poll = getattr(process, "poll", None)
                    if callable(poll) and poll() is None:
                        _mark_stalled("waiting for stdout to close")
                        break
                    continue

                if item is eof_marker:
                    break
                if isinstance(item, BaseException):
                    stream_error = item
                    break

                line = item
                raw_output_parts.append(line)
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                # Parse the JSON line using pi's streaming protocol
                stream_text, should_print, complete_text = _parse_pi_json_line(stripped)
                if stream_text is None and complete_text is None:
                    # Not valid JSON — show raw line as fallback
                    print(line, end="", flush=True)
                    text_parts.append(line)
                    text_lines_seen += 1
                    if self.verbose:
                        logger.debug("ralph.cmd.pi.raw_line %s", stripped[:500])
                elif stream_text is not None or complete_text is not None:
                    # Successfully parsed as JSON
                    json_lines_seen += 1
                    if should_print and stream_text:
                        # User-facing additive text delta — show to operator
                        print(stream_text, end="", flush=True)
                        text_parts.append(stream_text)
                        text_lines_seen += 1
                    if complete_text:
                        # Complete content block — capture for return value
                        complete_blocks.append(complete_text)
                    # In verbose mode, also log the raw JSON line
                    if self.verbose:
                        logger.debug("ralph.cmd.pi.json_line %s", stripped[:500])

            if stream_error is None and stalled_reason is None:
                try:
                    process.wait(timeout=self.pi_stream_timeout_seconds)
                except subprocess.TimeoutExpired:
                    _mark_stalled("waiting for pi to exit")
        finally:
            try:
                process.wait(timeout=1)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=1)
                except Exception:
                    pass
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

        stderr = "".join(stderr_parts)
        if stream_error is not None:
            raise RalphError(f"pi stdout reader failed: {stream_error}") from stream_error
        if stalled_reason is not None:
            stderr_text = stderr.strip()
            if stderr_text:
                raise RalphError(
                    f"pi stream stalled after {self.pi_stream_timeout_seconds:.0f}s {stalled_reason}: {stderr_text}"
                )
            raise RalphError(f"pi stream stalled after {self.pi_stream_timeout_seconds:.0f}s {stalled_reason}")

        if process.returncode != 0:
            if self.fail_open and ("pi" not in self.fatal_cmds):
                if self.verbose:
                    logger.debug("ralph.cmd.pi.run stderr=%s", stderr.strip()[:1000])
                logger.warning("ralph.cmd.pi.failed_but_fail_open cmd=%s rc=%s stderr=%s", cmd, process.returncode, stderr.strip())
                # Return whatever text we accumulated so far
                full_text = complete_blocks[-1] if complete_blocks else "".join(text_parts)
                return full_text
            if self.verbose:
                logger.debug("ralph.cmd.pi.run stderr=%s", stderr.strip()[:1000])
            raise RalphError(f"pi run failed: {stderr.strip()}")

        raw_output = "".join(raw_output_parts)
        self._last_structured_response = None

        # Prefer complete text blocks from text_end/agent_end events over
        # accumulated deltas — they give us the full, assembled content.
        # Use only the LAST complete block — it's the final, authoritative
        # response (earlier blocks may be intermediate text from tool-use turns).
        if complete_blocks:
            full_text = complete_blocks[-1]
        else:
            full_text = "".join(text_parts)

        structured_response: StructuredResponse | None = None
        if json_lines_seen > 0 and text_lines_seen == 0 and not complete_blocks:
            structured_response = parse_structured_response(raw_output)
            if structured_response:
                full_text = structured_response.render()
                logger.info(
                    "ralph.cmd.pi.structured_response actions=%d summary_len=%d",
                    len(structured_response.actions),
                    len(structured_response.summary),
                )

        # If JSON lines were seen but no text extracted, warn
        if structured_response is None and json_lines_seen > 0 and text_lines_seen == 0 and not complete_blocks:
            logger.warning(
                "ralph.cmd.pi.no_text_extracted json_lines=%d — "
                "pi produced JSON but no user-facing text content was found",
                json_lines_seen,
            )
            if self.debug_persist:
                persisted_path = _persist_debug_payload(
                    raw_output,
                    {
                        **self._debug_context,
                        "reason": "no_text_extracted",
                        "model": model_for_phase,
                        "command": list(cmd),
                        "returncode": process.returncode,
                        "json_lines_seen": json_lines_seen,
                        "text_lines_seen": text_lines_seen,
                        "stream": True,
                    },
                )
                if persisted_path:
                    logger.info("ralph.cmd.pi.debug_payload_saved path=%s", persisted_path)
        elif json_lines_seen > 0 and len(full_text) < 50 and not complete_blocks and structured_response is None:
            logger.warning(
                "ralph.cmd.pi.very_short_text json_lines=%d text_len=%d — "
                "pi produced very little text content, audit may be incomplete",
                json_lines_seen, len(full_text),
            )

        self._last_structured_response = structured_response

        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(full_text), full_text[:1000])

        logger.info("ralph.cmd.pi.stream_end returncode=%d text_len=%d", process.returncode, len(full_text))
        return full_text

    def _run_checks(self) -> None:
        for cmd in self.check_cmds:
            quiet_cmd = canonicalize_quiet_test_command(cmd)
            logger.debug("ralph.cmd.check cmd=%s quiet_cmd=%s", cmd, quiet_cmd)
            proc = self._call_with_retry(["bash", "-lc", quiet_cmd], category="check", expect_json=False)
            if self.verbose:
                logger.debug("ralph.cmd.check stdout=%s", (getattr(proc, 'stdout', '') or '').strip()[:1000])
                logger.debug("ralph.cmd.check stderr=%s", (getattr(proc, 'stderr', '') or '').strip()[:1000])
            if getattr(proc, 'returncode', 0) != 0:
                if self.fail_open and ("check" not in self.fatal_cmds):
                    logger.warning("ralph.cmd.check.failed_but_fail_open cmd=%s rc=%s stderr=%s", cmd, getattr(proc, 'returncode', None), (getattr(proc, 'stderr', '') or '').strip())
                    continue
                raise RalphError(f"Check failed ({cmd}): {(getattr(proc, 'stderr', '') or '').strip() or (getattr(proc, 'stdout', '') or '').strip()}")

    def _run_merge(self) -> None:
        if not self.confirm_merge:
            return
        for cmd in (
            ["git", "fetch", "origin", "main"],
            ["git", "merge", "--ff-only", "origin/main"],
            ["git", "push", "origin", "HEAD"],
        ):
            logger.debug("ralph.cmd.merge step=%s", shlex.join(cmd))
            proc = self._call_with_retry(cmd, category="merge", expect_json=False)
            if self.verbose:
                logger.debug("ralph.cmd.merge stdout=%s", (getattr(proc, 'stdout', '') or '').strip()[:1000])
            if getattr(proc, 'returncode', 0) != 0:
                if self.verbose:
                    logger.debug("ralph.cmd.merge stderr=%s", (getattr(proc, 'stderr', '') or '').strip()[:1000])
                # Merge is considered fatal by default
                raise RalphError(f"Merge step failed ({' '.join(cmd)}): {(getattr(proc, 'stderr', '') or '').strip()}")


    def _is_effort_risk_computed(self, target_id: str) -> bool:
        """Check whether effort and risk have already been set on the work item."""
        item = self._wl_show(target_id).get("workItem", {})
        effort = (item.get("effort") or "").strip()
        risk = (item.get("risk") or "").strip()
        if effort and risk:
            logger.info("ralph.autoplan.already_computed target=%s effort=%s risk=%s", target_id, effort, risk)
            return True
        # Also check for an existing autoplan decision comment
        for comment in self._wl_comment_list(target_id):
            comment_text = comment.get("comment") or ""
            if "autoplan-decision-hash:" in comment_text:
                logger.info("ralph.autoplan.decision_comment_exists target=%s", target_id)
                return True
        return False

    def _run_effort_and_risk(self, target_id: str) -> dict | None:
        """Run the effort-and-risk skill for the target work item.

        Returns the parsed JSON result, or None on failure/ambiguity.
        """
        payload = json.dumps({
            "issue_id": target_id,
            "o": 0, "m": 0, "p": 0,
            "certainty": 100,
            "assumptions": ["Auto-generated by ralph autoplan"],
            "unknowns": [],
        })

                # Resolve the effort-and-risk orchestrator relative to the skills root so the
        # script works even when invoked from a different repository CWD.
        skill_root = Path(__file__).resolve().parents[2]
        orchestrate_script = skill_root / "effort-and-risk" / "scripts" / "orchestrate_estimate.py"
        py = "python3"
        cmd = [py, str(orchestrate_script)]
        logger.info("ralph.autoplan.effort_risk.start target=%s", target_id)

        # Use wrapper to support retries and fail-open behaviour.
        if self.runner == _default_runner:
            proc = self._call_with_retry(cmd, category="effort_and_risk", expect_json=False, input_data=payload)
        else:
            proc = self._call_with_retry(cmd + [payload], category="effort_and_risk", expect_json=False)

        if getattr(proc, 'returncode', 0) != 0:
            logger.warning(
                "ralph.autoplan.effort_risk.failed target=%s rc=%s stderr=%s",
                target_id, getattr(proc, 'returncode', None), (getattr(proc, 'stderr', '') or "")[:500],
            )
            return None

        try:
            result = json.loads(getattr(proc, 'stdout', '') or "")
            if not isinstance(result, dict):
                logger.warning(
                    "ralph.autoplan.effort_risk.unexpected_type target=%s type=%s",
                    target_id, type(result).__name__,
                )
                return None
            # Check for error key in result
            if "error" in result:
                logger.warning(
                    "ralph.autoplan.effort_risk.error target=%s error=%s",
                    target_id, result["error"][:200],
                )
                return None
            logger.info(
                "ralph.autoplan.effort_risk.complete target=%s tshirt=%s risk=%s",
                target_id,
                result.get("effort", {}).get("tshirt", "unknown"),
                result.get("risk", {}).get("level", "unknown"),
            )
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "ralph.autoplan.effort_risk.parse_error target=%s exc=%s",
                target_id, exc,
            )
            return None

    def _append_autoplan_comment_once(self, work_item_id: str, tshirt: str, risk_level: str, risk_score: int | float, do_plan: bool) -> None:
        """Post an auto-plan decision comment, idempotently."""
        # Build a deterministic marker from the decision values
        marker_key = f"autoplan-decision:{tshirt}:{risk_level}:{risk_score}"
        marker_hash = hashlib.sha256(marker_key.encode("utf-8")).hexdigest()[:16]
        marker = f"autoplan-decision-hash:{marker_hash}"

        # Check for existing comment with this marker
        for existing in self._wl_comment_list(work_item_id):
            if marker in (existing.get("comment") or ""):
                logger.debug(
                    "ralph.autoplan.comment_exists target=%s marker=%s",
                    work_item_id, marker,
                )
                return

        decision = (
            "run /plan (effort or risk above threshold)"
            if do_plan
            else "proceed to implement (effort and risk below threshold)"
        )
        comment_parts = [
            "# Ralph Auto-Plan Decision",
            marker,
            "",
            f"Effort: {tshirt}",
            f"Risk: {risk_level} (score: {risk_score})",
            f"Decision: {decision}",
        ]
        comment = "\n".join(comment_parts)
        self._wl_comment_add(work_item_id, comment)

    def _run_autoplan(self, target_id: str) -> tuple[bool, str]:
        """Run the auto-plan decision for a work item at intake_complete.

        Returns (do_plan, updated_stage):
        - do_plan: True if /plan should be invoked
        - updated_stage: the effective stage after autoplan
        """
        logger.info("ralph.autoplan.start target=%s", target_id)

        # Idempotence check: if effort/risk already computed, skip re-computation
        if self._is_effort_risk_computed(target_id):
            logger.info("ralph.autoplan.already_computed_skipping target=%s", target_id)
            # Use the existing values to determine the decision
            item = self._wl_show(target_id).get("workItem", {})
            effort = (item.get("effort") or "").strip()
            risk = (item.get("risk") or "").strip()
            do_plan = not (effort in self.autoplan_effort_skip and risk in self.autoplan_risk_skip)
            logger.info(
                "ralph.autoplan.cached_decision target=%s effort=%s risk=%s do_plan=%s",
                target_id, effort, risk, do_plan,
            )
            if do_plan:
                stage = item.get("stage", "unknown")
                if stage == "plan_complete":
                    # Plan was already completed
                    return False, "plan_complete"
                # Need to run plan
                logger.info("ralph.autoplan.plan_invoked target=%s", target_id)
                # Use pi to run the plan skill so the configured model is used.
                try:
                    self._run_pi(f"/skill:plan {target_id}", phase="planning")
                except RalphError as e:
                    raise RalphError(f"plan command failed: {e}") from e
                logger.info("ralph.autoplan.plan_complete target=%s", target_id)
                return True, "plan_complete"
            return False, "intake_complete"

        # Run effort-and-risk skill
        er_result = self._run_effort_and_risk(target_id)

        if er_result is None:
            # Failure or ambiguity: default to running /plan (safety-first)
            logger.info("ralph.autoplan.effort_risk_failed_defaults_to_plan target=%s", target_id)
            do_plan = True
            tshirt = "unknown"
            risk_level = "unknown"
            risk_score = 0
        else:
            tshirt = er_result.get("effort", {}).get("tshirt", "")
            risk_level = er_result.get("risk", {}).get("level", "")
            risk_score = er_result.get("risk", {}).get("score", 0)
            do_plan = not (tshirt in self.autoplan_effort_skip and risk_level in self.autoplan_risk_skip)
            logger.info(
                "ralph.autoplan.result target=%s tshirt=%s risk=%s do_plan=%s",
                target_id, tshirt, risk_level, do_plan,
            )

        # Post the decision comment idempotently
        self._append_autoplan_comment_once(target_id, tshirt, risk_level, risk_score, do_plan)

        if do_plan:
            # Invoke plan and wait for completion
            logger.info("ralph.autoplan.plan_invoked target=%s", target_id)
            # Use pi to run the plan skill so the configured model is used.
            try:
                self._run_pi(f"/skill:plan {target_id}", phase="planning")
            except RalphError as e:
                raise RalphError(f"plan command failed: {e}") from e
            logger.info("ralph.autoplan.plan_complete target=%s", target_id)
            return True, "plan_complete"

        logger.info("ralph.autoplan.skip_plan target=%s", target_id)
        return False, "intake_complete"

    def _scope_ids(self, target_id: str) -> list[str]:
        data = self._wl_show(target_id, children=True)
        scope = [target_id]
        scope.extend(child["id"] for child in data.get("children", []))
        return scope

    def _scope_ids_recursive(self, target_id: str) -> list[str]:
        """Return the target id and all recursive descendant ids.

        Uses a BFS-like traversal and calls `wl show --children` for each
        discovered item. On errors, traversal stops for that branch but the
        already discovered ids are still returned.
        """
        scope: list[str] = []
        seen: set[str] = set()
        queue: list[str] = [target_id]
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            scope.append(current)
            try:
                data = self._wl_show(current, children=True)
                children = data.get("children", [])
                for child in children:
                    cid = child.get("id")
                    if cid and cid not in seen:
                        queue.append(cid)
            except Exception:
                logger.exception("ralph.scope_recursive_failed id=%s", current)
                # Continue with whatever we've discovered so far
                continue
        return scope

    def _resolve_focus_target(self, target_id: str, child_id: str | None = None) -> str:
        """Resolve the effective work item to run.

        When `child_id` is provided, validate that it is a direct child of the
        supplied target and then focus the loop on that child only.
        """
        if not child_id:
            return target_id
        if child_id == target_id:
            return child_id

        children = self._wl_show(target_id, children=True).get("children", [])
        child_ids = {child.get("id") for child in children if isinstance(child, dict) and child.get("id")}
        if child_id not in child_ids:
            raise RalphError(f"Child {child_id} is not a direct child of {target_id}")
        logger.info("ralph.loop.child_focus parent=%s child=%s", target_id, child_id)
        return child_id

    def _parse_iso_ts(self, ts: str):
        """Parse an ISO-8601-ish timestamp into a timezone-aware datetime.

        Handles strings with a trailing 'Z' by normalizing to '+00:00'. Also
        accepts numeric epoch seconds as int/float.
        Returns None on parse failure.
        """
        if not ts:
            return None
        # Numeric epoch seconds
        if isinstance(ts, (int, float)):
            try:
                return datetime.fromtimestamp(float(ts), tz=timezone.utc)
            except Exception:
                return None
        if isinstance(ts, str):
            s = ts
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(s)
            except Exception:
                # Fallback: try to parse as float epoch string
                try:
                    return datetime.fromtimestamp(float(ts), tz=timezone.utc)
                except Exception:
                    return None
        return None

    def _latest_audit_comment_ts(self, work_item_id: str):
        """Return the most-recent createdAt timestamp (as datetime) for comments
        on work_item_id whose first non-empty line starts with
        '# AMPA Audit Result'. Returns None if no such comment exists.
        """
        comments = self._wl_comment_list(work_item_id)
        latest = None
        for c in comments:
            comment_text = (c.get("comment") or "")
            # first non-empty line
            first = None
            for line in comment_text.splitlines():
                if line.strip():
                    first = line.strip()
                    break
            if not first:
                continue
            if not first.startswith("# AMPA Audit Result"):
                continue
            created = c.get("createdAt") or c.get("created_at") or c.get("created") or c.get("postedAt")
            ts = self._parse_iso_ts(created)
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    def _latest_audit_comment_ts_for_scope(self, scope_ids):
        latest = None
        for wid in scope_ids:
            ts = self._latest_audit_comment_ts(wid)
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    def _max_updated_at_for_scope(self, scope_ids):
        """Return the most-recent updatedAt timestamp (as datetime) across the scope.
        Returns None if no updatedAt values are available.
        """
        latest = None
        for wid in scope_ids:
            try:
                item = self._wl_show(wid).get("workItem", {})
            except Exception:
                continue
            updated = item.get("updatedAt") or item.get("updated_at") or item.get("updated")
            ts = self._parse_iso_ts(updated)
            if ts and (latest is None or ts > latest):
                latest = ts
        return latest

    def _assert_precondition(self, target_id: str) -> None:
        item = self._wl_show(target_id).get("workItem", {})
        stage = item.get("stage", "unknown")
        # Accept intake_complete and in_progress as valid entrypoints.
        # - intake_complete: triggers the auto-plan decision before the first implement pass
        # - in_progress: resume or continue an already-started implement→audit loop
        if stage not in {"plan_complete", "in_review", "intake_complete", "in_progress"}:
            # Updated phrasing to include in_progress and intake_complete for clarity
            raise RalphError(
                f"Target {target_id} must be stage plan_complete, in_review, or in_progress (or intake_complete for auto-plan) before running ralph; current stage is {stage}."
            )

    def _scope_in_review(self, scope_ids: Iterable[str]) -> bool:
        allowed = {"in_review", "done", "completed", "closed"}
        for item_id in scope_ids:
            item = self._wl_show(item_id).get("workItem", {})
            stage = item.get("stage", "")
            status = item.get("status", "")
            if stage not in allowed and status not in {"closed", "completed"}:
                return False
        return True

    def _child_stage_map(self, target_id: str) -> dict[str, str]:
        """Return direct child stage map for a target item.

        If child stage is not present in `wl show --children`, fall back to
        reading the child item directly.
        """
        data = self._wl_show(target_id, children=True)
        children = data.get("children", [])
        stages: dict[str, str] = {}
        for child in children:
            child_id = child.get("id")
            if not child_id:
                continue
            child_stage = child.get("stage")
            if not child_stage:
                child_stage = self._wl_show(child_id).get("workItem", {}).get("stage", "")
            stages[child_id] = child_stage or ""
        return stages

    def _structured_remediation_hint(self) -> str:
        """Return a concise remediation hint from the last structured response."""
        response = self._last_structured_response
        if not response:
            return ""
        return response.remediation_hint()

    def _extract_no_safe_path_reason(self, implement_output: str = "") -> str:
        """Return the missing producer decision when the model cannot proceed safely."""
        response = self._last_structured_response
        if response:
            for action in response.actions:
                if action.command == "no_safe_path":
                    reason = " ".join(part for part in action.args if part).strip()
                    if reason:
                        return reason
                    if response.summary:
                        return response.summary.strip()
                    if response.text:
                        return response.text.strip()
            for candidate in (response.summary, response.text):
                if not candidate:
                    continue
                lowered = candidate.strip().lower()
                if lowered.startswith("no safe path"):
                    return candidate.strip()

        text = implement_output.strip()
        if not text:
            return ""
        lowered = text.lower()
        for prefix in ("no safe path:", "no_safe_path:", "producer_input_required:"):
            if lowered.startswith(prefix):
                reason = text[len(prefix):].strip()
                if reason:
                    return reason
        if lowered.startswith("no safe path"):
            return text
        return ""

    def _compact_after_child_transition(
        self,
        target_id: str,
        previous_child_stages: dict[str, str],
        attempt: int,
    ) -> tuple[int, int]:
        """Run `/compact` for children that newly transitioned to in_review.

        Returns a tuple of (invocation_count, failure_count).
        """
        try:
            current_child_stages = self._child_stage_map(target_id)
        except Exception as exc:
            logger.warning(
                "ralph.compact.stage_snapshot_failed target=%s attempt=%d error=%s",
                target_id,
                attempt,
                exc,
            )
            return 0, 0

        invocations = 0
        failures = 0
        for child_id, new_stage in current_child_stages.items():
            previous_stage = previous_child_stages.get(child_id, "")
            if previous_stage == "in_review" or new_stage != "in_review":
                continue

            invocations += 1
            logger.info(
                "ralph.compact.transition target=%s child=%s attempt=%d compact.invocations=%d",
                target_id,
                child_id,
                attempt,
                invocations,
            )
            try:
                self._run_pi("/compact", phase="implementation")
            except Exception as exc:
                failures += 1
                logger.warning(
                    "ralph.compact.failed target=%s child=%s attempt=%d compact.failures=%d error=%s",
                    target_id,
                    child_id,
                    attempt,
                    failures,
                    exc,
                )

        return invocations, failures

    def run(self, target_id: str, child_id: str | None = None) -> dict:
        focus_id = self._resolve_focus_target(target_id, child_id)
        self._assert_precondition(focus_id)
        scope_ids = self._scope_ids_recursive(focus_id)

        # If the target is already in_review, skip the first implement pass and
        # go straight to audit. If a persisted audit comment shows the scope is
        # up-to-date, we can skip invoking the audit skill at the start of the
        # iteration and instead rely on the persisted audit.
        target_item = self._wl_show(focus_id).get("workItem", {})
        target_stage = target_item.get("stage", "unknown")
        skip_implement = target_stage == "in_review"
        remediation = ""
        compact_invocations = 0
        compact_failures = 0

        logger.info(
            "ralph.loop.start target=%s focus=%s scope=%s max_attempts=%d skip_implement=%s",
            target_id, focus_id, scope_ids, self.max_attempts, skip_implement,
        )

        for attempt in range(1, self.max_attempts + 1):
            if self.cancel_file and os.path.exists(self.cancel_file):
                logger.info("ralph.loop.cancelled target=%s attempt=%d", focus_id, attempt)
                return {
                    "status": "cancelled",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "compact": {"invocations": compact_invocations, "failures": compact_failures},
                }

            logger.info("ralph.loop.attempt.start target=%s attempt=%d", focus_id, attempt)
            self._debug_context = {
                "target_id": target_id,
                "focus_id": focus_id,
                "child_id": child_id,
                "attempt": attempt,
            }

            # Whether we will rely on a persisted audit without invoking the audit skill
            use_persisted_audit = False

            if target_stage == "intake_complete" and attempt == 1 and not self.no_autoplan:
                # Auto-plan step: evaluate effort/risk and decide whether
                # to invoke /plan or proceed directly to implement.
                try:
                    do_plan, new_stage = self._run_autoplan(focus_id)
                    if new_stage == "plan_complete":
                        target_stage = "plan_complete"
                except RalphError:
                    raise
                except Exception:
                    logger.exception("ralph.loop.autoplan.unexpected target=%s", focus_id)
                # After autoplan (whether plan ran or not), proceed to implement
                try:
                    previous_child_stages = self._child_stage_map(focus_id)
                except Exception as exc:
                    logger.warning(
                        "ralph.compact.pre_snapshot_failed target=%s attempt=%d error=%s",
                        focus_id,
                        attempt,
                        exc,
                    )
                    previous_child_stages = {}
                implement_output = self._run_pi(_build_implement_prompt(focus_id, remediation), phase="intake")
                no_safe_path_reason = self._extract_no_safe_path_reason(implement_output)
                invocations, failures = self._compact_after_child_transition(focus_id, previous_child_stages, attempt)
                compact_invocations += invocations
                compact_failures += failures
                if invocations or failures:
                    logger.info(
                        "ralph.compact.metrics target=%s attempt=%d compact.invocations=%d compact.failures=%d",
                        focus_id,
                        attempt,
                        compact_invocations,
                        compact_failures,
                    )
                if no_safe_path_reason:
                    logger.warning(
                        "ralph.loop.no_safe_path target=%s attempt=%d reason=%s",
                        focus_id,
                        attempt,
                        no_safe_path_reason,
                    )
                    return {
                        "status": "producer_input_required",
                        "attempt": attempt,
                        "scope": scope_ids,
                        "reason": no_safe_path_reason,
                        "compact": {"invocations": compact_invocations, "failures": compact_failures},
                    }
            elif skip_implement and attempt == 1:
                # Target already in_review — decide whether start-of-iteration audit is needed
                logger.info("ralph.loop.skip_implement target=%s stage=in_review", focus_id)
                try:
                    latest_comment_ts = self._latest_audit_comment_ts_for_scope(scope_ids)
                    max_updated_at = self._max_updated_at_for_scope(scope_ids)
                    if latest_comment_ts and max_updated_at and latest_comment_ts >= max_updated_at:
                        logger.info(
                            "ralph.loop.audit.skipping_start target=%s latest_comment_ts=%s max_updated_at=%s",
                            focus_id, latest_comment_ts.isoformat(), max_updated_at.isoformat()
                        )
                        use_persisted_audit = True
                except Exception:
                    logger.exception("ralph.loop.pre_audit_check_failed target=%s", focus_id)
            else:
                try:
                    previous_child_stages = self._child_stage_map(focus_id)
                except Exception as exc:
                    logger.warning(
                        "ralph.compact.pre_snapshot_failed target=%s attempt=%d error=%s",
                        focus_id,
                        attempt,
                        exc,
                    )
                    previous_child_stages = {}
                implement_output = self._run_pi(_build_implement_prompt(focus_id, remediation), phase="implementation")
                no_safe_path_reason = self._extract_no_safe_path_reason(implement_output)
                invocations, failures = self._compact_after_child_transition(focus_id, previous_child_stages, attempt)
                compact_invocations += invocations
                compact_failures += failures
                if invocations or failures:
                    logger.info(
                        "ralph.compact.metrics target=%s attempt=%d compact.invocations=%d compact.failures=%d",
                        focus_id,
                        attempt,
                        compact_invocations,
                        compact_failures,
                    )
                if no_safe_path_reason:
                    logger.warning(
                        "ralph.loop.no_safe_path target=%s attempt=%d reason=%s",
                        focus_id,
                        attempt,
                        no_safe_path_reason,
                    )
                    return {
                        "status": "producer_input_required",
                        "attempt": attempt,
                        "scope": scope_ids,
                        "reason": no_safe_path_reason,
                        "compact": {"invocations": compact_invocations, "failures": compact_failures},
                    }

            logger.info("ralph.loop.audit.start target=%s attempt=%d", focus_id, attempt)
            # Run the audit skill unless we've determined that the persisted audit
            # is up-to-date and can be used without re-running the audit skill.
            if use_persisted_audit:
                logger.info("ralph.loop.audit.skipped_using_persisted target=%s attempt=%d", focus_id, attempt)
            else:
                # Run the audit skill; it MUST persist the structured audit to the work item.
                self._run_pi(f"/skill:audit {focus_id}", phase="audit")
            # Read the persisted audit from the work item via wl show.
            item = self._wl_show(focus_id).get("workItem", {})
            # Normalize persisted audit extraction to handle both object and string shapes.
            # - If workItem.audit is an object (dict), prefer audit.get("text")
            # - If it's a string, use it directly
            # - Otherwise fall back to workItem.auditText
            audit_field = item.get("audit")
            if isinstance(audit_field, dict):
                audit_text = audit_field.get("text", "") or item.get("auditText", "") or ""
            elif isinstance(audit_field, str):
                audit_text = audit_field
            else:
                audit_text = item.get("auditText", "") or ""

            if not audit_text:
                raise RalphError(f"No persisted audit found for {focus_id} after running /skill:audit; expected workItem.audit to contain the structured report.")

            structured_audit = None
            lines = [l.strip() for l in audit_text.splitlines() if l.strip()]
            if not any(l.lower().startswith("ready to close:") for l in lines):
                structured_audit = parse_structured_response(audit_text)
                if structured_audit:
                    audit_text = structured_audit.render()
                    self._last_structured_response = structured_audit
                    lines = [l.strip() for l in audit_text.splitlines() if l.strip()]

            if not any(l.lower().startswith("ready to close:") for l in lines):
                excerpt = audit_text.strip().replace("\n", " ")[:200]
                raise RalphError(f"No 'Ready to close:' header found in persisted audit for {focus_id}. Excerpt: {excerpt}")
            audit = parse_audit_report(audit_text)
            if self.verbose:
                logger.debug("ralph.loop.audit.parsed target=%s attempt=%d ready=%s criteria_count=%d unmet=%d", focus_id, attempt, audit.ready_to_close, len(audit.criteria), len(audit.unmet_or_partial))

            logger.info(
                "ralph.loop.audit.complete target=%s attempt=%d ready=%s unmet=%d criteria=%d",
                focus_id, attempt, audit.ready_to_close, len(audit.unmet_or_partial), len(audit.criteria),
            )

            if audit.ready_to_close and self._scope_in_review(scope_ids):
                logger.info("ralph.loop.checks.start target=%s", focus_id)
                self._run_checks()
                logger.info("ralph.loop.merge target=%s confirm=%s", focus_id, self.confirm_merge)
                self._run_merge()
                return {
                    "status": "success",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "merge_offered": True,
                    "merge_executed": self.confirm_merge,
                    "compact": {"invocations": compact_invocations, "failures": compact_failures},
                }

            remediation = _build_remediation_prompt()
            structured_hint = self._structured_remediation_hint()
            if structured_hint:
                remediation = "\n\n".join([remediation, structured_hint])
                logger.info(
                    "ralph.loop.remediate.structured target=%s attempt=%d actions=%d",
                    focus_id,
                    attempt,
                    len(self._last_structured_response.actions) if self._last_structured_response else 0,
                )
            logger.info(
                "ralph.loop.remediate target=%s attempt=%d unmet_count=%d",
                focus_id, attempt, len(audit.unmet_or_partial),
            )

        logger.warning("ralph.loop.max_attempts target=%s", focus_id)
        return {
            "status": "max_attempts",
            "attempt": self.max_attempts,
            "scope": scope_ids,
            "compact": {"invocations": compact_invocations, "failures": compact_failures},
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ralph implement→audit orchestration loop")
    parser.add_argument("work_item_id", help="Target Worklog item id")
    parser.add_argument("--max-attempts", type=int, default=10)
    parser.add_argument("--check-cmd", action="append", default=[], help="Build/test command to run on success; test commands are normalized to quiet mode")
    parser.add_argument("--confirm-merge", action="store_true", help="Execute merge/push steps after successful audit")
    parser.add_argument("--cancel-file", default=None, help="Path checked each attempt; if present, stop loop")
    parser.add_argument("--child", default=None, help="Run the loop focused on a direct child work-item instead of the positional target")
    parser.add_argument("--debug-persist", action="store_true", help="Persist raw Pi payloads when a streamed run produces no user-facing text")
    parser.add_argument("--quiet", action="store_true", help="Suppress console progress output and pi streaming (only print final JSON result)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed delegation commands and subprocess output")
    parser.add_argument("--no-stream", action="store_true", help="Don't stream pi subprocess output to console (use buffered capture instead)")
    parser.add_argument("--model", default=None, help=f"Legacy single model for all phases (default: {DEFAULT_MODEL}, or string 'model' key in .ralph.json)")
    parser.add_argument("--model-source", choices=sorted(MODEL_SOURCES), default=None, help="Model source for phase defaults/config (remote|local). Default is remote.")
    parser.add_argument("--model-intake", default=None, help="Override intake phase model")
    parser.add_argument("--model-planning", default=None, help="Override planning phase model")
    parser.add_argument("--model-implementation", default=None, help="Override implementation phase model")
    parser.add_argument("--model-audit", default=None, help="Override audit phase model")
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--wl-bin", default="wl")
    parser.add_argument("--no-autoplan", action="store_true", help="Disable the auto-plan step for intake_complete items (proceed directly to implement)")
    parser.add_argument("--autoplan-effort-skip", nargs="*", help="Effort t-shirt sizes that skip /plan (default: Extra Small Small)")
    parser.add_argument("--autoplan-risk-skip", nargs="*", help="Risk levels that skip /plan (default: Low)")
    parser.add_argument("--fail-open", action="store_true", help="Continue on delegated command failures (non-fatal) when possible")
    parser.add_argument("--retry", type=int, default=0, help="Number of additional retries for delegated commands (default: 0)")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Delay in seconds between retries")
    parser.add_argument("--fatal-cmd", action="append", default=[], help="""Command categories to treat as fatal even when --fail-open is set. Example categories: merge, pi, wl, check, effort_and_risk""")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON Lines (jsonl) for lifecycle events and final result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure console logging based on verbosity or json mode.
    #   --quiet    : WARNING only, no progress, no pi streaming
    #   (default)  : INFO — lifecycle progress (attempt, audit, merge) + pi streaming
    #   --verbose  : DEBUG — adds delegated commands, subprocess output, raw audit
    #   --no-stream: disable pi stdout streaming (use buffered capture)
    if args.json:
        # Emit compact JSON lines with timestamp, level, logger, message, and
        # any structured extras attached to the record (for example, delegated
        # command details such as `cmd` and `argv`).
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLineFormatter())
        logging.getLogger("ralph").addHandler(handler)
        logging.getLogger("ralph").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    else:
        if not args.quiet:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
            logging.getLogger("ralph").addHandler(handler)
        if args.verbose:
            logging.getLogger("ralph").setLevel(logging.DEBUG)
        else:
            logging.getLogger("ralph").setLevel(logging.INFO)

    autoplan_effort_skip = frozenset(args.autoplan_effort_skip) if args.autoplan_effort_skip else None
    autoplan_risk_skip = frozenset(args.autoplan_risk_skip) if args.autoplan_risk_skip else None

    config = _load_config()
    legacy_config_model = _extract_legacy_model_from_config(config)
    phase_model_config = _extract_phase_model_config(config)
    config_model_source_raw = config.get("model_source") if isinstance(config, dict) else None

    model_source_explicit = args.model_source is not None or config_model_source_raw is not None
    legacy_model_explicit = args.model is not None or legacy_config_model is not None
    effective_model_source = _normalize_model_source(args.model_source or config_model_source_raw)

    loop = RalphLoop(
        pi_bin=args.pi_bin,
        wl_bin=args.wl_bin,
        model=_resolve_model(args.model, legacy_config_model),
        model_source=effective_model_source,
        model_intake=args.model_intake,
        model_planning=args.model_planning,
        model_implementation=args.model_implementation,
        model_audit=args.model_audit,
        model_config=phase_model_config,
        model_source_explicit=model_source_explicit,
        legacy_model_explicit=legacy_model_explicit,
        check_cmds=args.check_cmd,
        max_attempts=args.max_attempts,
        confirm_merge=args.confirm_merge,
        cancel_file=args.cancel_file,
        verbose=args.verbose,
        stream=not args.quiet and not args.no_stream,
        autoplan_effort_skip=autoplan_effort_skip,
        autoplan_risk_skip=autoplan_risk_skip,
        fail_open=args.fail_open,
        retry=args.retry,
        retry_delay=args.retry_delay,
        fatal_cmds=args.fatal_cmd,
        debug_persist=args.debug_persist,
    )
    loop.no_autoplan = args.no_autoplan
    try:
        result = loop.run(args.work_item_id, child_id=args.child)
    except RalphError as exc:
        if args.json:
            # emit error as single JSON line
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"ralph: {exc}")
        return 2

    if args.json:
        # final result as a single JSON line
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2))

    if result.get("status") == "success":
        return 0
    if result.get("status") == "cancelled":
        return 3
    if result.get("status") == "producer_input_required":
        return 2
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
