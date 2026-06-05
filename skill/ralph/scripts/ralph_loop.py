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
import signal
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

from skill.ralph.scripts.signal_system import EventType, SignalWriter, resolve_signal_path
from skill.ralph.scripts.structured_response import StructuredResponse, parse_structured_response
from skill.ralph.scripts.webhook_notifier import WebhookNotifier, resolve_webhook_url
from skill.test_runner import canonicalize_quiet_test_command

logger = logging.getLogger("ralph")

ASSET_CONFIG_PATH = Path(__file__).resolve().parent.parent / "assets" / ".ralph.json"
DEFAULT_MODEL = "opencode-go/glm-5.1"
DEFAULT_MODEL_SOURCE = "local"
MODEL_SOURCES = frozenset({"remote", "local"})
DEFAULT_PI_STREAM_TIMEOUT_SECONDS = 60.0
REMOTE_PI_STREAM_TIMEOUT_SECONDS = 900.0
MODEL_PHASES: tuple[str, ...] = ("intake", "planning", "implementation", "audit")
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


class PiInputEchoError(RalphError):
    """Raised when Pi echoes back the input instead of executing."""

    def __init__(self, message: str, input_text: str, output_text: str):
        super().__init__(message)
        self.input_text = input_text
        self.output_text = output_text


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


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_asset_config() -> dict:
    """Load the shipped default config from skill/ralph/assets/.ralph.json."""
    try:
        with open(ASSET_CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        logger.debug("ralph.config: failed to load asset config from %s", ASSET_CONFIG_PATH)
    return {}


def _load_config() -> dict:
    """Load config merging asset defaults with CWD config file.

    Asset defaults from skill/ralph/assets/.ralph.json are the base.
    A .ralph.json (or ralph.config.json) in the current working directory
    overrides those values. CLI flags take highest precedence downstream.
    """
    config = _load_asset_config()

    for path in RALPH_CONFIG_FILES:
        if not path.exists():
            continue
        if path.suffix == ".json":
            try:
                with open(path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    config = _deep_merge(config, data)
            except (json.JSONDecodeError, OSError):
                logger.debug("ralph.config: failed to load %s", path)

    return config


def _resolve_model(cli_model: str | None, config_model: str | None) -> str:
    """Resolve the legacy single model: CLI flag > config file > default."""
    if cli_model:
        return cli_model
    if config_model:
        return config_model
    return DEFAULT_MODEL


def _resolve_stream_timeout(config: dict, model_source: str) -> float:
    """Resolve the pi stream timeout: config > source-specific default > global default.

    Config may specify:
      - "timeout": {"pi_stream": 120}  (global override)
      - "timeout": {"pi_stream": {"remote": 300, "local": 60}}  (source-mapped)
    """
    timeout_config = config.get("timeout", {})
    if not isinstance(timeout_config, dict):
        return DEFAULT_PI_STREAM_TIMEOUT_SECONDS

    pi_stream = timeout_config.get("pi_stream")
    if isinstance(pi_stream, (int, float)):
        return float(pi_stream)
    if isinstance(pi_stream, dict):
        source_value = pi_stream.get(model_source)
        if isinstance(source_value, (int, float)):
            return float(source_value)

    # Fall back to source-specific defaults
    if model_source == "remote":
        return REMOTE_PI_STREAM_TIMEOUT_SECONDS
    return DEFAULT_PI_STREAM_TIMEOUT_SECONDS


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


def _build_implement_prompt(work_item_id: str, remediation: str = "", command: str = "implement") -> str:
    """Build the non-interactive implement prompt for Ralph.

    By default this builds the umbrella "implement" prompt that may traverse
    dependencies. Passing ``command="implement-single"`` will build a
    scoped prompt suitable for per-child runs.

    The implement step must never ask the producer questions during the
    default loop. If the model cannot continue safely, it must return a
    structured no_safe_path response that names the missing producer decision.
    """
    if command == "implement-single":
        return _build_implement_single_prompt(work_item_id, remediation)

    parts = [
        f"implement {work_item_id}",
        "Continue until the work item and all dependencies are completed, but do not merge.",
        "Do not ask the producer questions or pause for interactive input.",
        "If you cannot continue safely without explicit producer input, stop and return a structured no_safe_path response with the missing decision.",
    ]
    if remediation:
        parts.append(remediation)
    return "\n".join(parts)


def _build_implement_single_prompt(work_item_id: str, remediation: str = "") -> str:
    """Build a scoped implement-single prompt for per-child Ralph runs.

    Uses the ``implement-single`` skill which works on exactly the given
    work-item id without traversing dependencies via ``wl next``.  This is
    critical for per-child iteration so that implementing one child cannot
    accidentally pick up or modify sibling work items.

    The implement step must never ask the producer questions during the
    default loop. If the model cannot continue safely, it must return a
    structured no_safe_path response that names the missing producer decision.
    """
    parts = [
        f"implement-single {work_item_id}",
        "Complete only this work item.",
        "Continue until the work item is completed, but do not merge.",
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


# Minimum length for output text to be considered valid (not an echo)
_MIN_VALID_OUTPUT_LENGTH = 50

# Patterns that indicate raw skill file content was returned instead of execution results
_RAW_SKILL_CONTENT_PATTERNS = [
    r'^\s*<skill\s+name=',
    r'^\s*<skill\s+location=',
    r'^\s*#\s+Audit\s*$',
    r'^\s*#\s+Overview\s*$',
    r'^\s*##\s+Overview\s*$',
    r'^\s*References\s+are\s+relative\s+to',
]


def _normalize_text_for_comparison(text: str) -> str:
    """Normalize text for comparison by stripping whitespace and lowercasing."""
    return re.sub(r'\s+', ' ', text.strip().lower())


def _detect_input_echo(input_text: str, output_text: str) -> bool:
    """Detect if the output is an echo of the input.

    Returns True if the output is identical or nearly identical to the input.
    """
    if not input_text or not output_text:
        return False

    normalized_input = _normalize_text_for_comparison(input_text)
    normalized_output = _normalize_text_for_comparison(output_text)

    # Exact match after normalization
    if normalized_input == normalized_output:
        return True

    # Output is a prefix of input (truncated echo)
    if len(normalized_output) > 20 and normalized_input.startswith(normalized_output):
        return True

    # Input is a prefix of output (output includes input plus minimal content)
    # Only if output is very close in length to input
    if len(normalized_output) > 20 and normalized_output.startswith(normalized_input):
        similarity = len(normalized_input) / max(len(normalized_output), 1)
        if similarity > 0.9:
            return True

    return False


def _detect_raw_skill_content(output_text: str) -> bool:
    """Detect if the output contains raw skill file content.

    Returns True if the output looks like raw SKILL.md content instead of
    execution results.
    """
    if not output_text:
        return False

    stripped = output_text.strip()
    if not stripped:
        return False

    # Check for XML skill tags
    for pattern in _RAW_SKILL_CONTENT_PATTERNS:
        if re.search(pattern, stripped, re.MULTILINE):
            return True

    return False


def _validate_pi_output(
    input_text: str,
    output_text: str,
    phase: str,
    structured_response: StructuredResponse | None = None,
) -> tuple[bool, str]:
    """Validate Pi output to detect silent failures.

    Returns (is_valid, reason) where:
    - is_valid: True if output appears valid, False if it looks like an echo or raw content
    - reason: Description of the validation failure (empty if valid)
    """
    # Check for raw skill content
    if _detect_raw_skill_content(output_text):
        return False, "Output contains raw skill file content instead of execution results"

    # Check for input echo
    if _detect_input_echo(input_text, output_text):
        return False, "Output is an echo of the input prompt"

    # For implementation phase, check for very short output with no actions
    if phase == "implementation":
        # Empty output is always invalid for implementation
        if len(output_text.strip()) == 0:
            return False, "No output from implementation phase"
        if structured_response is not None and len(structured_response.actions) == 0:
            if len(output_text.strip()) < _MIN_VALID_OUTPUT_LENGTH:
                return False, f"Output too short ({len(output_text.strip())} chars) with no actions for implementation phase"

    # For audit phase, check for missing audit markers
    if phase == "audit":
        output_lower = output_text.lower()
        has_ready_to_close = "ready to close:" in output_lower
        has_audit_markers = has_ready_to_close or "audit result" in output_lower or "acceptance criteria" in output_lower
        if not has_audit_markers and len(output_text.strip()) < 100:
            return False, "Audit output missing expected markers (Ready to close:)"

    return True, ""


# Default thresholds for auto-plan decision
# If effort t-shirt is in this set AND risk level is in the risk set,
# skip /plan and proceed directly to implement.
DEFAULT_AUTOPLAN_EFFORT_SKIP: frozenset[str] = frozenset({"Extra Small", "Small"})
DEFAULT_AUTOPLAN_RISK_SKIP: frozenset[str] = frozenset({"Low"})

# Maximum consecutive attempts on unchanged code before reporting a stall
DEFAULT_MAX_CYCLES_NO_CHANGE: int = 3




def _comment_hash(audit_text: str) -> str:
    """Return a 16-char hex digest of audit_text for comment deduplication."""
    return hashlib.sha256(audit_text.encode("utf-8")).hexdigest()[:16]


def _has_ready_to_close_marker(text: str) -> bool:
    """Check if text contains a valid 'Ready to close:' marker."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("ready to close:"):
            return True
    return False


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
        max_cycles_no_change: int | None = None,
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
        pi_stream_timeout: float | None = None,
        signal_file_path: str | None = None,
        webhook_url: str | None = None,
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
        if pi_stream_timeout is not None:
            self.pi_stream_timeout_seconds = float(pi_stream_timeout)
        else:
            self.pi_stream_timeout_seconds = _resolve_stream_timeout(self.model_config, self.model_source)
        # Grace period (seconds) for waiting after SIGTERM before escalating to SIGKILL
        self.pi_cleanup_timeout = 5.0
        self._pi_process: subprocess.Popen | None = None
        self._debug_context: dict[str, object] = {}
        self._last_structured_response: StructuredResponse | None = None
        self._last_implement_output: str = ""
        # Cycle detection state
        self._no_change_cycle_count: int = 0
        self._last_known_head: str | None = None
        self._max_cycles_no_change: int = max_cycles_no_change if max_cycles_no_change is not None else DEFAULT_MAX_CYCLES_NO_CHANGE

        # Signal file and webhook notification infrastructure
        self._signal_file_path: str | None = signal_file_path
        if signal_file_path:
            self._signal_writer: SignalWriter | None = SignalWriter(Path(signal_file_path))
        else:
            self._signal_writer = None
        self._webhook_url: str | None = webhook_url
        if webhook_url:
            self._webhook_notifier: WebhookNotifier | None = WebhookNotifier(webhook_url)
        else:
            self._webhook_notifier = None

    def _notify_event(
        self,
        event_type: EventType,
        work_item_ids: list[str] | None = None,
        description: str | None = None,
    ) -> None:
        """Write a signal file and optionally send a webhook notification.

        This is fire-and-forget: errors from either channel are logged at
        WARNING level and never propagated to the caller.
        """
        if self._signal_writer is not None:
            try:
                self._signal_writer.write_event(
                    event_type,
                    work_item_ids=work_item_ids,
                )
            except Exception as exc:
                logger.warning(
                    "ralph.notify.signal_write_failed event=%s error=%s",
                    event_type.value,
                    exc,
                )
        if self._webhook_notifier is not None:
            try:
                self._webhook_notifier.send_event(
                    event_type,
                    work_item_ids=work_item_ids,
                    description=description,
                )
            except Exception as exc:
                logger.warning(
                    "ralph.notify.webhook_failed event=%s error=%s",
                    event_type.value,
                    exc,
                )

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

    def _get_children(self, target_id: str) -> list[dict]:
        """Return direct children for *target_id*.

        Subclasses may override this to avoid calling the `wl` binary during
        tests. The default implementation delegates to _wl_show.
        """
        return self._wl_show(target_id, children=True).get("children", [])

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

    def _wl_audit_show(self, work_item_id: str) -> dict:
        """Call `wl audit-show <id> --json` and return the parsed result.

        Returns a dict with keys: success, workItemId, audit.
        When no audit exists, audit is None.
        When an audit exists, audit contains {workItemId, readyToClose,
        auditedAt, summary, rawOutput, author}.
        """
        cmd = [self.wl_bin, "audit-show", work_item_id, "--json"]
        logger.debug("ralph.cmd.wl.audit_show cmd=%s", cmd)
        result = self._call_with_retry(cmd, category="wl", expect_json=True)
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

    def _wl_update_stage(self, work_item_id: str, stage: str) -> None:
        """Update the work item's stage via wl update."""
        cmd = [self.wl_bin, "update", work_item_id, "--stage", stage]
        logger.info("ralph.cmd.wl.update_stage target=%s stage=%s", work_item_id, stage)
        self._call_with_retry(cmd, category="wl", expect_json=False)

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
            return self._stream_pi(cmd, prompt, model_for_phase, phase)

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
        logger.info(
            "ralph.cmd.pi.non_streaming_end returncode=%d text_len=%d",
            getattr(proc, "returncode", 0),
            len(text),
            extra={"text": text},
        )
        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(text), text[:1000])

        # Validate output to detect silent failures (input echo, raw skill content)
        is_valid, reason = _validate_pi_output(prompt, text, phase, structured)
        if not is_valid:
            logger.warning(
                "ralph.cmd.pi.output_validation_failed phase=%s reason=%s",
                phase,
                reason,
                extra={"reason": reason, "phase": phase, "input_len": len(prompt), "output_len": len(text)},
            )
            if self.fail_open and ("pi" not in self.fatal_cmds):
                logger.warning("ralph.cmd.pi.output_invalid_but_fail_open phase=%s", phase)
                return text
            raise PiInputEchoError(
                f"pi output validation failed ({phase}): {reason}",
                input_text=prompt,
                output_text=text,
            )

        return text


    def _stream_pi(self, cmd: list[str], prompt: str, model_for_phase: str | None = None, phase: str = "implementation") -> str:
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
        self._pi_process = process

        text_parts: list[str] = []
        complete_blocks: list[str] = []
        raw_output_parts: list[str] = []
        stderr_parts: list[str] = []
        json_lines_seen = 0
        text_lines_seen = 0
        needs_newline_sep = False
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
                    if needs_newline_sep:
                        print()
                        needs_newline_sep = False
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
                        if needs_newline_sep:
                            print()
                            needs_newline_sep = False
                        print(stream_text, end="", flush=True)
                        text_parts.append(stream_text)
                        text_lines_seen += 1
                    if complete_text:
                        # Complete content block — capture for return value
                        complete_blocks.append(complete_text)
                        if text_lines_seen > 0:
                            needs_newline_sep = True
                    elif not should_print and text_lines_seen > 0:
                        # Suppressed JSON event (thinking, tool, structural, etc.)
                        # — flag that the next printable text needs a newline separator
                        needs_newline_sep = True
                    # In verbose mode, also log the raw JSON line
                    if self.verbose:
                        logger.debug("ralph.cmd.pi.json_line %s", stripped[:500])

            if stream_error is None and stalled_reason is None:
                try:
                    process.wait(timeout=self.pi_stream_timeout_seconds)
                except subprocess.TimeoutExpired:
                    _mark_stalled("waiting for pi to exit")
        finally:
            self._pi_process = None
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
                    extra={
                        "actions": [str(a) for a in structured_response.actions],
                        "summary": structured_response.summary,
                        "text": structured_response.text,
                    },
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
            # In stream mode, treat no-extracted-text as a fatal validation error
            raise PiInputEchoError("Pi produced invalid output: no user-facing text extracted", input_text=prompt, output_text=raw_output)
        elif json_lines_seen > 0 and len(full_text) < 50 and not complete_blocks and structured_response is None:
            logger.warning(
                "ralph.cmd.pi.very_short_text json_lines=%d text_len=%d — "
                "pi produced very little text content, audit may be incomplete",
                json_lines_seen, len(full_text),
            )

        self._last_structured_response = structured_response

        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(full_text), full_text[:1000])

        logger.info(
            "ralph.cmd.pi.stream_end returncode=%d text_len=%d",
            process.returncode,
            len(full_text),
            extra={"text": full_text},
        )

        # Validate output to detect silent failures (input echo, raw skill content)
        is_valid, reason = _validate_pi_output(prompt, full_text, phase, structured_response)
        if not is_valid:
            logger.warning(
                "ralph.cmd.pi.output_validation_failed phase=%s reason=%s",
                phase,
                reason,
                extra={"reason": reason, "phase": phase, "input_len": len(prompt), "output_len": len(full_text)},
            )
            if self.fail_open and ("pi" not in self.fatal_cmds):
                logger.warning("ralph.cmd.pi.output_invalid_but_fail_open phase=%s", phase)
                return full_text
            raise PiInputEchoError(
                f"Pi produced invalid output: {reason}",
                input_text=prompt,
                output_text=full_text,
            )

        return full_text

    def _cleanup_pi_process(self) -> None:
        """Clean up any lingering Pi subprocess after loop completion.

        Attempts graceful termination first (SIGTERM), then escalates to
        forced termination (SIGKILL) after a configurable grace period.
        Safe to call even if the process has already exited.
        """
        process = self._pi_process
        if process is None:
            return

        pid = process.pid
        if pid is None:
            self._pi_process = None
            return

        poll = process.poll()
        if poll is not None:
            logger.info(
                "ralph.cleanup.pi.already_exited pid=%d returncode=%s",
                pid, poll,
            )
            self._pi_process = None
            return

        logger.info(
            "ralph.cleanup.pi.sending_sigterm pid=%d timeout=%.1f",
            pid, self.pi_cleanup_timeout,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            logger.info("ralph.cleanup.pi.already_gone pid=%d", pid)
            self._pi_process = None
            return
        except OSError as exc:
            logger.warning(
                "ralph.cleanup.pi.sigterm_failed pid=%d error=%s",
                pid, exc,
            )

        try:
            process.wait(timeout=self.pi_cleanup_timeout)
            logger.info(
                "ralph.cleanup.pi.graceful_exit pid=%d returncode=%s",
                pid, process.returncode,
            )
            self._pi_process = None
            return
        except subprocess.TimeoutExpired:
            logger.warning(
                "ralph.cleanup.pi.graceful_timeout pid=%d escalating_to_sigkill",
                pid,
            )

        try:
            process.kill()
            process.wait(timeout=1.0)
            logger.warning(
                "ralph.cleanup.pi.forced_kill pid=%d returncode=%s",
                pid, process.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ralph.cleanup.pi.sigkill_wait_timeout pid=%d", pid,
            )
        except OSError as exc:
            logger.warning(
                "ralph.cleanup.pi.kill_failed pid=%d error=%s",
                pid, exc,
            )

        self._pi_process = None

    def _run_checks(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for cmd in self.check_cmds:
            quiet_cmd = canonicalize_quiet_test_command(cmd)
            logger.debug("ralph.cmd.check cmd=%s quiet_cmd=%s", cmd, quiet_cmd)
            proc = self._call_with_retry(["bash", "-lc", quiet_cmd], category="check", expect_json=False)
            result = {
                "cmd": cmd,
                "stdout": (getattr(proc, 'stdout', '') or '').strip(),
                "stderr": (getattr(proc, 'stderr', '') or '').strip(),
                "returncode": getattr(proc, 'returncode', 0),
            }
            results.append(result)
            if self.verbose:
                logger.debug("ralph.cmd.check stdout=%s", result["stdout"][:1000])
                logger.debug("ralph.cmd.check stderr=%s", result["stderr"][:1000])
            if result["returncode"] != 0:
                if self.fail_open and ("check" not in self.fatal_cmds):
                    logger.warning("ralph.cmd.check.failed_but_fail_open cmd=%s rc=%s stderr=%s", cmd, result["returncode"], result["stderr"])
                    continue
                raise RalphError(f"Check failed ({cmd}): {result['stderr'] or result['stdout']}")
        return results

    def _capture_changed_files(self) -> list[dict[str, str]]:
        try:
            proc = self._call_with_retry(["git", "diff", "--name-status", "HEAD"], category="summary", expect_json=False)
            output = (getattr(proc, 'stdout', '') or '').strip()
            if not output:
                return []
            files: list[dict[str, str]] = []
            for line in output.splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    files.append({"status": parts[0], "file": parts[1]})
            return files
        except Exception:
            logger.warning("ralph.summary.git_diff_failed", exc_info=True)
            return []

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


    def _try_fallback_persist(self, issue_id: str, audit_output: str) -> bool:
        """Attempt to persist an audit report from captured Pi output.

        Checks if ``audit_output`` contains a valid 'Ready to close:' marker.
        If found, persists the entire audit output as the work item's audit
        text via ``wl update --audit-text``.

        Returns True if the audit was successfully persisted, False if the
        output lacks the required marker or persistence fails.
        """
        if not audit_output or not audit_output.strip():
            logger.debug(
                "ralph.fallback_persist.skipped reason=empty_output issue=%s",
                issue_id,
            )
            return False

        if not _has_ready_to_close_marker(audit_output):
            logger.debug(
                "ralph.fallback_persist.skipped reason=no_ready_to_close_marker issue=%s output_len=%d",
                issue_id,
                len(audit_output),
            )
            return False

        logger.info(
            "ralph.fallback_persist.start issue=%s output_len=%d",
            issue_id,
            len(audit_output),
        )

        try:
            cmd = [
                self.wl_bin,
                "update",
                issue_id,
                "--audit-text",
                audit_output,
                "--json",
            ]
            self._call_with_retry(cmd, category="wl", expect_json=True)
            logger.info(
                "ralph.fallback_persist.success issue=%s",
                issue_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "ralph.fallback_persist.failed issue=%s error=%s",
                issue_id,
                exc,
            )
            return False

    def _detect_no_change_cycle(self, attempt: int) -> bool:
        """Detect repeated cycles on unchanged code.

        Compares the current git HEAD with the last known HEAD. If they match
        and we are on at least the Nth attempt, increment the no-change
        counter. Returns True when the counter exceeds the configured maximum.

        The cycle state is reset when HEAD changes (code has moved forward).
        """
        current_head: str | None = None
        try:
            proc = self._call_with_retry(
                ["git", "rev-parse", "HEAD"],
                category="summary",
                expect_json=False,
            )
            output = (getattr(proc, 'stdout', '') or '').strip()
            if output:
                current_head = output
        except Exception:
            logger.debug("ralph.cycle_detection.git_rev_parse_failed", exc_info=True)
            return False

        if current_head is None:
            return False

        if self._last_known_head is not None and self._last_known_head == current_head:
            self._no_change_cycle_count += 1
            logger.info(
                "ralph.cycle_detection.no_change attempt=%d count=%d max=%d head=%s",
                attempt,
                self._no_change_cycle_count,
                self._max_cycles_no_change,
                current_head[:12],
            )
            if self._no_change_cycle_count >= self._max_cycles_no_change:
                logger.warning(
                    "ralph.cycle_detection.stalled attempt=%d count=%d max=%d head=%s",
                    attempt,
                    self._no_change_cycle_count,
                    self._max_cycles_no_change,
                    current_head[:12],
                )
                return True
        else:
            # HEAD changed (or first call) — reset counter
            if self._last_known_head is not None and self._last_known_head != current_head:
                logger.info(
                    "ralph.cycle_detection.head_changed old=%s new=%s resetting_count",
                    self._last_known_head[:12],
                    current_head[:12],
                )
            self._no_change_cycle_count = 0
            self._last_known_head = current_head

        return False

    def _read_persisted_audit_text(self, work_item_id: str) -> str:
        """Read the persisted audit text via `wl audit-show <id> --json`.

        Returns the raw audit text from the audit_results table, or an empty
        string if no audit record exists for the work item.
        """
        try:
            result = self._wl_audit_show(work_item_id)
        except Exception:
            logger.debug("ralph.audit_show_failed target=%s", work_item_id, exc_info=True)
            return ""
        audit = result.get("audit")
        if not audit:
            return ""
        # rawOutput holds the full audit text
        raw = audit.get("rawOutput") or ""
        # Also check legacy text field for backwards compatibility
        if isinstance(audit, dict) and not raw:
            raw = audit.get("text", "") or ""
        return raw

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
        try:
            item = self._wl_show(target_id).get("workItem", {})
        except Exception:
            # If the subclass provides a custom _get_children implementation
            # allow the run to proceed (tests often supply _get_children to
            # avoid calling the real wl binary). Otherwise, propagate.
            if getattr(type(self), "_get_children", None) is not None and getattr(type(self), "_get_children") is not RalphLoop._get_children:
                return
            raise
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

    def run_single_item(self, item_id: str, implement_command: str = "implement", skip_implement: bool = False, remediation: str = "", **kwargs) -> dict:
        """Execute implement and audit for a single work item with retries.

        Attempts implement + audit up to self.max_attempts. Returns:
        - {"status": "success", "attempt": n} on success
        - {"status": "max_attempts", "attempt": n, ...} when all attempts fail

        Additional keyword args are accepted for compatibility (e.g. force_fresh_audit).
        """
        max_attempts = max(1, int(self.max_attempts))
        force_fresh_audit = bool(kwargs.get("force_fresh_audit", False))

        for attempt in range(1, max_attempts + 1):
            # Implement step (unless skipped)
            if not skip_implement and implement_command:
                if implement_command == "implement-single":
                    prompt = _build_implement_single_prompt(item_id, remediation)
                else:
                    prompt = _build_implement_prompt(item_id, remediation, command=implement_command)
                impl_output = self._run_pi(prompt, phase="implementation")
                self._last_implement_output = impl_output

            # Audit step — capture output for fallback persistence if needed
            try:
                audit_output = self._run_pi(f"/skill:audit {item_id}", phase="audit")
            except Exception:
                # Treat audit invocation failures as a failed attempt and retry
                if attempt >= max_attempts:
                    return {"status": "max_attempts", "attempt": attempt}
                remediation = _build_remediation_prompt()
                continue

            # Read persisted audit via wl audit-show
            audit_text = self._read_persisted_audit_text(item_id)

            if not audit_text:
                # Audit model did NOT persist — try fallback from captured output
                fallback_ok = self._try_fallback_persist(item_id, audit_output)
                if fallback_ok:
                    # Fallback persisted successfully — re-read and evaluate
                    audit_text = self._read_persisted_audit_text(item_id)

            if not audit_text:
                # Still no audit text — this is a genuine failure (output lacked
                # marker, or wl update failed). Not the same as "gaps found."
                logger.warning(
                    "ralph.run_single_item.audit_missing_after_fallback target=%s attempt=%d",
                    item_id,
                    attempt,
                )
                # Check for cycle detection (no code changes across attempts)
                if self._detect_no_change_cycle(attempt):
                    logger.error(
                        "ralph.run_single_item.stalled target=%s attempt=%d cycles_no_change=%d",
                        item_id,
                        attempt,
                        self._no_change_cycle_count,
                    )
                    self._notify_event(
                        EventType.ERROR,
                        work_item_ids=[item_id],
                        description="Ralph single item stalled due to audit persistence failure",
                    )
                    return {
                        "status": "stalled",
                        "attempt": attempt,
                        "reason": "audit_persistence_failure_stalled",
                        "no_change_cycles": self._no_change_cycle_count,
                    }
                if attempt >= max_attempts:
                    self._notify_event(
                        EventType.MAX_ATTEMPTS,
                        work_item_ids=[item_id],
                        description="Ralph single item exhausted attempts with no persisted audit",
                    )
                    return {"status": "max_attempts", "attempt": attempt, "reason": "no_persisted_audit"}
                remediation = _build_remediation_prompt()
                continue

            parsed = parse_audit_report(audit_text)
            # Debug logging: capture parsed audit summary for triage
            logger.debug(
                "ralph.run_single_item.audit_parsed target=%s ready=%s criteria=%d unmet=%d",
                item_id,
                getattr(parsed, 'ready_to_close', False),
                len(getattr(parsed, 'criteria', [])),
                len(getattr(parsed, 'unmet_or_partial', [])),
            )
            if parsed.ready_to_close:
                logger.debug("ralph.run_single_item.success target=%s attempt=%d", item_id, attempt)
                self._notify_event(
                    EventType.STATUS_TRANSITION,
                    work_item_ids=[item_id],
                    description="Work item passed audit and is ready for review",
                )
                return {"status": "success", "attempt": attempt}

            # Audit reported unmet criteria — decide whether to retry
            unmet = [c.text for c in parsed.unmet_or_partial]
            logger.debug("ralph.run_single_item.unmet target=%s attempt=%d unmet_count=%d", item_id, attempt, len(unmet))
            # Check for cycle detection (no code changes across attempts)
            if self._detect_no_change_cycle(attempt):
                logger.error(
                    "ralph.run_single_item.stalled target=%s attempt=%d cycles_no_change=%d",
                    item_id,
                    attempt,
                    self._no_change_cycle_count,
                )
                self._notify_event(
                    EventType.ERROR,
                    work_item_ids=[item_id],
                    description="Ralph single item stalled with unmet audit criteria",
                )
                return {
                    "status": "stalled",
                    "attempt": attempt,
                    "reason": "unmet_criteria_stalled",
                    "no_change_cycles": self._no_change_cycle_count,
                    "unmet": unmet,
                }
            if attempt >= max_attempts:
                self._notify_event(
                    EventType.MAX_ATTEMPTS,
                    work_item_ids=[item_id],
                    description="Ralph single item exhausted maximum attempts",
                )
                return {"status": "max_attempts", "attempt": attempt, "unmet": unmet}

            # Build a remediation hint for the next attempt and retry
            remediation = _build_remediation_prompt()
            continue

        # Should not reach here
        self._notify_event(
            EventType.MAX_ATTEMPTS,
            work_item_ids=[item_id],
            description="Ralph single item exhausted attempts (unreachable fallback)",
        )
        return {"status": "max_attempts", "attempt": max_attempts}

    def run(self, target_id: str, child_id: str | None = None) -> dict:
        focus_id = self._resolve_focus_target(target_id, child_id)
        self._assert_precondition(focus_id)
        scope_ids = self._scope_ids_recursive(focus_id)

        # Notify: loop starting
        self._notify_event(EventType.STARTED, work_item_ids=scope_ids)

        # If the target is already in_review, skip the first implement pass and
        # go straight to audit. If a persisted audit comment shows the scope is
        # up-to-date, we can skip invoking the audit skill at the start of the
        # iteration and instead rely on the persisted audit.
        # If this target has direct children and we're focusing on the parent
        # (no child_id), perform per-child implement-single + audit runs. Each
        # child is attempted independently and retried up to self.max_attempts
        # before moving to the next sibling.
        try:
            children = self._get_children(focus_id)
        except Exception:
            children = []

        if children and child_id is None and focus_id == target_id:
            child_ids = [c.get("id") for c in children if c.get("id")]
            child_results: dict[str, dict] = {}
            failed_children: list[str] = []
            attempts_used = 1

            try:
                previous_child_stages = self._child_stage_map(focus_id)
            except Exception as exc:
                logger.warning(
                    "ralph.compact.pre_snapshot_failed target=%s error=%s",
                    focus_id,
                    exc,
                )
                previous_child_stages = {}

            for cid in child_ids:
                # Respect in_review stage reported by the children list
                stage = None
                for c in children:
                    if c.get("id") == cid:
                        stage = c.get("stage")
                        break
                if stage in {"done", "completed", "closed"}:
                    child_results[cid] = {"status": "skipped"}
                    continue
                
                if stage == "in_review":
                    # For in_review, check the most recent persisted audit result.
                    # If it says "Ready to close: Yes", we skip it.
                    # Otherwise (failed or no audit), we treat it as needing work.
                    audit_text = self._read_persisted_audit_text(cid)
                    if audit_text:
                        parsed = parse_audit_report(audit_text)
                        if parsed.ready_to_close:
                            child_results[cid] = {"status": "skipped"}
                            continue
                        logger.info("ralph.run.child_in_review_audit_fail target=%s child=%s", focus_id, cid)
                    else:
                        logger.info("ralph.run.child_in_review_no_audit target=%s child=%s", focus_id, cid)

                # Delegate to run_single_item which performs retries internally
                res = self.run_single_item(cid, implement_command="implement-single")
                used = int(res.get("attempt", 1)) if isinstance(res.get("attempt", 1), int) else 1
                attempts_used = max(attempts_used, used)
                if res.get("status") == "success":
                    child_results[cid] = {"status": "success"}
                else:
                    child_results[cid] = {"status": "failed", "reason": res.get("reason") or res.get("unmet")}
                    failed_children.append(cid)

            # Compact for children that transitioned to in_review during per-child runs
            invocations, failures = self._compact_after_child_transition(focus_id, previous_child_stages, attempts_used)

            if failed_children:
                # All children attempted — report failures
                self._notify_event(
                    EventType.MAX_ATTEMPTS,
                    work_item_ids=scope_ids,
                    description="Ralph child iteration exhausted all attempts",
                )
                return {
                    "status": "child_max_attempts",
                    "attempt": attempts_used,
                    "scope": scope_ids,
                    "failed_children": failed_children,
                    "child_results": child_results,
                    "compact": {"invocations": invocations, "failures": failures},
                }

            # All children succeeded — run parent integration audit
            parent_res = self.run_single_item(focus_id, skip_implement=True)
            parent_attempts = int(parent_res.get("attempt", 1)) if isinstance(parent_res.get("attempt", 1), int) else 1
            total_attempts = max(attempts_used, parent_attempts)
            if parent_res.get("status") == "success":
                self._notify_event(
                    EventType.COMPLETED,
                    work_item_ids=scope_ids,
                    description="Ralph loop completed successfully with all children",
                )
                return {"status": "success", "attempt": total_attempts, "scope": scope_ids, "child_results": child_results, "integration_audit": True, "compact": {"invocations": invocations, "failures": failures}}
            else:
                self._notify_event(
                    EventType.MAX_ATTEMPTS,
                    work_item_ids=scope_ids,
                    description="Ralph integration audit exhausted all attempts",
                )
                return {"status": "integration_max_attempts", "attempt": total_attempts, "scope": scope_ids, "child_results": child_results, "integration_audit": True, "compact": {"invocations": invocations, "failures": failures}}

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
                self._cleanup_pi_process()
                self._notify_event(
                    EventType.CANCELLED,
                    work_item_ids=scope_ids,
                    description="Ralph loop cancelled by operator",
                )
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

            # Per-child iteration: if the target has direct children and we're
            # not focusing on a single child, process each child within this
            # attempt. Running inside the attempt loop allows retries across
            # attempts when remediation is scheduled.
            try:
                children = self._get_children(focus_id)
            except Exception:
                children = []

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
                implement_output = self._run_pi(_build_implement_prompt(focus_id, remediation), phase="implementation")
                self._last_implement_output = implement_output
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
                    self._cleanup_pi_process()
                    self._notify_event(
                        EventType.ERROR,
                        work_item_ids=scope_ids,
                        description=f"Ralph loop requires producer input: {no_safe_path_reason}",
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
                self._last_implement_output = implement_output
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
                    self._cleanup_pi_process()
                    self._notify_event(
                        EventType.ERROR,
                        work_item_ids=scope_ids,
                        description=f"Ralph loop requires producer input: {no_safe_path_reason}",
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
            audit_output = ""
            if use_persisted_audit:
                logger.info("ralph.loop.audit.skipped_using_persisted target=%s attempt=%d", focus_id, attempt)
            else:
                # Run the audit skill; capture output for fallback persistence if needed.
                audit_output = self._run_pi(f"/skill:audit {focus_id}", phase="audit")
            # Read the persisted audit via wl audit-show
            audit_text = self._read_persisted_audit_text(focus_id)

            if not audit_text:
                # Audit model did NOT persist — try fallback from captured output
                fallback_ok = self._try_fallback_persist(focus_id, audit_output)
                if fallback_ok:
                    # Fallback persisted successfully — re-read
                    audit_text = self._read_persisted_audit_text(focus_id)

            if not audit_text:
                raise RalphError(f"No persisted audit found for {focus_id} after running /skill:audit and fallback persistence; expected an audit_results row for the work item.")

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
                check_results = self._run_checks()
                changed_files = self._capture_changed_files()
                logger.info("ralph.loop.merge target=%s confirm=%s", focus_id, self.confirm_merge)
                self._run_merge()
                # Mark the work item as in_review now that the audit passed
                self._wl_update_stage(focus_id, "in_review")
                logger.info("ralph.loop.stage_update target=%s stage=in_review audit=pass", focus_id)
                self._cleanup_pi_process()
                self._notify_event(
                    EventType.COMPLETED,
                    work_item_ids=scope_ids,
                    description="Ralph loop completed successfully after passing audit",
                )
                return {
                    "status": "success",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "merge_offered": True,
                    "merge_executed": self.confirm_merge,
                    "compact": {"invocations": compact_invocations, "failures": compact_failures},
                    "summary": {
                        "changed_files": changed_files,
                        "change_descriptions": self._last_implement_output,
                        "check_results": check_results,
                    },
                }

            # Safeguard: if audit failed, ensure the item is not left in in_review.
            # The implement skill (when running under Ralph) should not mark as
            # in_review, but if it was previously in in_review (e.g., from a
            # prior manual update or a previous run), restore it.
            item_now = self._wl_show(focus_id).get("workItem", {})
            current_stage = item_now.get("stage", "")
            if current_stage == "in_review" and not audit.ready_to_close:
                logger.info(
                    "ralph.loop.audit.failed.stage_restore target=%s current_stage=in_review restoring_to=%s",
                    focus_id, target_stage,
                )
                self._wl_update_stage(focus_id, target_stage)

            # Check for cycle detection (no code changes across attempts)
            if self._detect_no_change_cycle(attempt):
                logger.error(
                    "ralph.loop.stalled target=%s attempt=%d cycles_no_change=%d",
                    focus_id,
                    attempt,
                    self._no_change_cycle_count,
                )
                self._cleanup_pi_process()
                self._notify_event(
                    EventType.ERROR,
                    work_item_ids=scope_ids,
                    description="Ralph loop stalled due to no code changes across attempts",
                )
                return {
                    "status": "stalled",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "reason": "unmet_criteria_stalled",
                    "no_change_cycles": self._no_change_cycle_count,
                    "unmet": [c.text for c in audit.unmet_or_partial],
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
        self._cleanup_pi_process()
        self._notify_event(
            EventType.MAX_ATTEMPTS,
            work_item_ids=scope_ids,
            description="Ralph loop exhausted maximum attempts",
        )
        return {
            "status": "max_attempts",
            "attempt": self.max_attempts,
            "scope": scope_ids,
            "compact": {"invocations": compact_invocations, "failures": compact_failures},
        }


def _preprocess_args(argv: Sequence[str] | None) -> list[str]:
    """Pre-process command-line arguments to support shorthand model source syntax.

    Supports:
      ralph <work_item_id> [remote|local] [options]

    The positional model source argument is converted to --model-source.
    """
    if argv is None:
        return []

    args = list(argv)
    if not args:
        return args

    # Skip if --model-source is already provided
    if "--model-source" in args:
        return args

    # Find the work_item_id (first positional argument that doesn't start with -)
    work_item_idx = None
    for i, arg in enumerate(args):
        if not arg.startswith("-"):
            work_item_idx = i
            break

    if work_item_idx is None:
        return args

    # Check if the next argument after work_item_id is a model source shorthand
    next_idx = work_item_idx + 1
    if next_idx < len(args):
        next_arg = args[next_idx]
        if next_arg in MODEL_SOURCES and not next_arg.startswith("-"):
            # Convert shorthand to --model-source flag
            args = args[:next_idx] + ["--model-source", next_arg] + args[next_idx + 1:]

    return args


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
    parser.add_argument("--model", default=None, help=f"Legacy single model for all phases (default from skill/ralph/assets/.ralph.json, or string 'model' key in .ralph.json)")
    parser.add_argument("--model-source", choices=sorted(MODEL_SOURCES), default=None, help="Model source for phase defaults/config (remote|local). Default is local.")
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
    if argv is None:
        argv = sys.argv[1:]
    preprocessed_args = _preprocess_args(argv)
    args = parser.parse_args(preprocessed_args)

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

    # Resolve signal file path and webhook URL from config
    signal_file_path = str(resolve_signal_path(config))
    webhook_url = resolve_webhook_url(config)

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
        signal_file_path=signal_file_path,
        webhook_url=webhook_url,
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
