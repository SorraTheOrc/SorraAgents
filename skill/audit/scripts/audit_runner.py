#!/usr/bin/env python3
"""Audit runner – deterministic audit orchestration.

Provides two subcommands:
  issue <id>   – audit a single work item
  project      – audit the overall project

Usage:
  audit_runner.py issue <id> [--do-not-persist] [--pi-bin pi] [--model <name>]
  audit_runner.py project [--pi-bin pi] [--model <name>]

Exit codes:
  0 – success (report printed to stdout)
  1 – Worklog / CLI / Pi failure
  2 – argument error
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.audit.scripts.persist_audit import persist_audit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CHILDREN_CAP = 10

# Model / config constants (following Ralph's pattern)
ASSET_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "ralph" / "assets" / ".ralph.json"
DEFAULT_MODEL = "opencode-go/glm-5.1"
DEFAULT_MODEL_SOURCE = "local"
MODEL_SOURCES = frozenset({"remote", "local"})
RALPH_CONFIG_FILES = [
    Path(".ralph.json"),
    Path("ralph.config.json"),
]
AUDIT_PHASE = "audit"

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _run_wl(runner: Runner, cmd: Sequence[str]) -> dict:
    """Run a ``wl`` command via the injectable *runner* and return parsed JSON."""
    proc = runner(cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wl command failed ({' '.join(cmd)}): {proc.stderr.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from wl: {exc}") from exc
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"Worklog command failed: {data.get('error', 'unknown error')}"
        )
    return data


# ---------------------------------------------------------------------------
# Config loading (following Ralph's _load_config / _deep_merge pattern)
# ---------------------------------------------------------------------------


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
        pass
    return {}


def _load_config() -> dict:
    """Load config merging asset defaults with CWD config file.

    Asset defaults from skill/ralph/assets/.ralph.json are the base.
    A .ralph.json or ralph.config.json in the current working directory
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
                pass

    return config


def _normalize_model_source(source: str | None) -> str:
    """Normalize a model_source value to a valid value (remote|local)."""
    if not source:
        return DEFAULT_MODEL_SOURCE
    normalized = str(source).strip().lower()
    if normalized in MODEL_SOURCES:
        return normalized
    return DEFAULT_MODEL_SOURCE


def _coerce_model_str(value: object) -> str | None:
    """Extract a non-empty trimmed string from *value*, or None."""
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return None


def _resolve_phase_model_value(value: object, model_source: str) -> str | None:
    """Resolve a model value that may be a string or source-mapped dict.

    If *value* is a plain string, return it directly.
    If *value* is a dict with keys matching *model_source* (remote|local),
    return the corresponding value.
    """
    direct = _coerce_model_str(value)
    if direct:
        return direct
    if isinstance(value, dict):
        source_value = _coerce_model_str(value.get(model_source))
        if source_value:
            return source_value
    return None


def _extract_phase_model_config(config: dict) -> dict[str, object]:
    """Extract per-phase model config from the loaded .ralph.json.

    Checks these locations (in order):
      - model.<phase>  (nested key)
      - model.remote.<phase> / model.local.<phase>  (source-mapped)
      - model[phase]   (dict access)
      - model[remote|local][phase]  (source-mapped dict access)
    """
    phase_config: dict[str, object] = {}
    model_root = config.get("model")

    for phase in (AUDIT_PHASE,):
        # Check dotted keys first (model.audit, model.remote.audit, etc.)
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
                source_map = {}
                if isinstance(remote_map, dict) and phase in remote_map:
                    source_map["remote"] = remote_map[phase]
                if isinstance(local_map, dict) and phase in local_map:
                    source_map["local"] = local_map[phase]
                if source_map:
                    phase_config[phase] = source_map

    return phase_config


def _resolve_model_for_phase(phase: str, config: dict,
                              model_source: str,
                              cli_model: str | None = None) -> str:
    """Resolve the model for *phase* with the resolution chain:

    1. --model CLI flag (explicit override, highest priority)
    2. Config-driven: phase model from .ralph.json resolved via model_source
    3. Hardcoded fallback: DEFAULT_MODEL

    This mirrors Ralph's _resolve_model_for_phase pattern.
    """
    # 1. CLI override
    explicit = _coerce_model_str(cli_model)
    if explicit:
        return explicit

    # 2. Config-driven resolution
    phase_config = _extract_phase_model_config(config)
    config_value = phase_config.get(phase)
    resolved = _resolve_phase_model_value(config_value, model_source)
    if resolved:
        return resolved

    # 3. Hardcoded fallback
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Pi integration (duplicated from ralph for now – see OQ-1)
# ---------------------------------------------------------------------------

def _call_pi(prompt: str, model: str = DEFAULT_MODEL,
             pi_bin: str = "pi") -> dict:
    """Call Pi via subprocess and parse the JSON-stream response.

    Returns a dict with keys ``verdict`` and ``evidence``.
    On success, implementations may also include additional diagnostic keys
    such as ``raw_stdout``, ``raw_stderr`` and ``extracted_text`` which are
    useful for debugging. This function returns at minimum
    ``{"verdict": <met|unmet|partial>, "evidence": <text>}``.

    Uses the same JSON-stream protocol as ralph (``pi -p --mode json``).
    Uses ``communicate()`` to avoid pipe-buffer deadlocks.
    """
    cmd = [pi_bin, "-p", "--mode", "json", "--model", model, prompt]
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise RuntimeError(f"pi binary not found: {pi_bin}")

    try:
        stdout, stderr = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()

    raw = stdout or ""
    if not raw:
        return {"verdict": "unmet", "evidence": "", "raw_stdout": stdout, "raw_stderr": stderr}

    # Parse JSON lines looking for the final agent_end message
    text = _extract_pi_text(raw)
    if not text:
        return {"verdict": "unmet", "evidence": "", "raw_stdout": stdout, "raw_stderr": stderr}

    # Try to parse the text as JSON with verdict/evidence
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {
                "verdict": obj.get("verdict", "unmet").lower(),
                "evidence": obj.get("evidence", ""),
                "raw_stdout": stdout,
                "raw_stderr": stderr,
                "extracted_text": text,
            }
    except json.JSONDecodeError:
        pass

    # If Pi returned free-form text, use it as evidence and default to met
    return {"verdict": "met", "evidence": text.strip()[:200], "raw_stdout": stdout, "raw_stderr": stderr, "extracted_text": text}


def _extract_pi_text(raw: str) -> str:
    """Extract user-facing text from pi --mode json output.

    Uses the same parsing logic as ralph_loop._parse_pi_json_line.
    """
    delta_parts: list[str] = []
    complete_blocks: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stream_text, _, complete_text = _parse_pi_json_line(stripped)
        if stream_text is None and complete_text is None:
            continue  # not valid JSON
        if complete_text is not None:
            complete_blocks.append(complete_text)
        elif stream_text:
            delta_parts.append(stream_text)

    # Prefer complete blocks (agent_end, text_end) over accumulated deltas
    if complete_blocks:
        return complete_blocks[-1]
    return "".join(delta_parts)


def _parse_pi_json_line(line: str):
    """Parse a single JSON line from pi --mode json.

    Returns (stream_text, should_print, complete_text), same as ralph.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None, False, None
    if not isinstance(obj, dict):
        return None, False, None

    event_type = obj.get("type", "")

    if event_type == "message_update":
        assistant = obj.get("assistantMessageEvent")
        if isinstance(assistant, dict):
            inner = assistant.get("type", "")
            if inner == "text_delta":
                delta = assistant.get("delta", "")
                return (delta, bool(delta), None) if delta else ("", False, None)
            if inner == "text_end":
                content = assistant.get("content", "")
                return ("", False, content) if content else ("", False, None)
            if inner in ("thinking_start", "thinking_delta", "thinking_end",
                         "toolcall_start", "toolcall_delta", "toolcall_end",
                         "text_start"):
                return "", False, None
            content_text = _extract_text_from_content(assistant.get("content"))
            if content_text:
                return "", False, content_text
            return "", False, None
        return "", False, None

    if event_type in ("message_start", "message_end", "turn_end"):
        message = obj.get("message")
        text = _extract_text_from_assistant_message(message)
        if text:
            return "", False, text
        return "", False, None

    if event_type == "agent_end":
        text = _extract_last_assistant_message_text(obj.get("messages"))
        if text:
            return "", False, text
        return "", False, None

    if event_type in ("session", "agent_start", "turn_start",
                       "tool_execution_start", "tool_execution_update",
                       "tool_execution_end"):
        return "", False, None

    # Fallback
    for key in ("content", "text", "delta"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val, True, None
    return "", False, None


def _extract_text_from_content(content) -> str | None:
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
        return "".join(parts) if parts else None
    return None


def _extract_text_from_assistant_message(message) -> str | None:
    if not isinstance(message, dict):
        return None
    return _extract_text_from_content(message.get("content"))


def _extract_last_assistant_message_text(messages) -> str | None:
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _extract_text_from_content(msg.get("content"))
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# Acceptance-criteria extractor
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list | None:
    """Extract the last JSON array from text that may contain analysis before the array.

    Pi often returns analysis text followed by a JSON array at the end.
    This function finds the last `[` that is NOT inside a string and tries to parse.

    Returns the parsed list if found, otherwise None.
    """
    if not text:
        return None

    # Find positions of `[` that could start a JSON array
    # We need to skip `[` characters that are inside JSON strings
    possible_starts = []
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        # We're not in a string
        if char == '[':
            # Check if this could be the start of a JSON array
            rest = text[i + 1:].lstrip()
            if rest and (rest[0] in ('{', '"', ']', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-')):
                possible_starts.append(i)

    # Try each possible start position from last to first
    for start in reversed(possible_starts):
        candidate = text[start:].strip()

        # Try the full candidate first
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # If that failed, try to find where the JSON array ends
        for end_search in range(len(candidate) - 1, 0, -1):
            if candidate[end_search] == ']':
                try:
                    result = json.loads(candidate[:end_search + 1])
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    continue

    return None


def _extract_acs(description: str) -> list[str]:
    """Extract acceptance criteria lines from a markdown description."""
    pattern = re.compile(
        r"^#{0,3}\s*(?:Acceptance|Success)\s+Criteria\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(description)
    if not match:
        return ["No acceptance criteria defined."]

    start = match.end()
    lines = description[start:].splitlines()
    acs: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            break
        numbered = re.match(r"^\d+\.\s+(.*)", stripped)
        if numbered:
            acs.append(numbered.group(1))
            continue
        bulleted = re.match(r"^[-*]\s+(.*)", stripped)
        if bulleted:
            acs.append(bulleted.group(1))
            continue
        if acs and stripped:
            break

    if not acs:
        return ["No acceptance criteria defined."]
    return acs


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _assemble_issue_report(issue: dict, ac_results: list[dict],
                           child_results: list[dict]) -> str:
    """Assemble the canonical issue-mode audit report.

    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    *child_results* is a list of child review dicts with keys:
      ``title``, ``id``, ``status``, ``stage``, ``ac_results``.

    Ready-to-close logic:
      - All acceptance criteria (parent + children) must be ``met``.
      - All non-deleted children must be in ``in_review`` or ``done`` stage.
        Children with ``status: in_progress`` but ``stage: in_review`` are
        acceptable and do NOT block closure.
    """
    all_ac_met = all(
        r["verdict"] == "met"
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    # Check that all active children are in in_review or done stage
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done")
        for c in active_children
    )
    ready = "Yes" if (all_ac_met and all_children_reviewed) else "No"

    lines = [f"Ready to close: {ready}", "", "## Summary", ""]

    if ready == "Yes":
        lines.append(
            f"All {len(ac_results)} acceptance criteria for work item "
            f"{issue.get('id', '?')} are met. All children are in in_review or done stage."
        )
    else:
        unmet_count = sum(
            1 for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
            if r["verdict"] != "met"
        )
        # Identify children not in in_review/done stage
        not_reviewed = [
            c for c in child_results
            if c.get("stage") not in ("in_review", "done", "")
        ]
        if unmet_count > 0 and not_reviewed:
            lines.append(
                f"{unmet_count} acceptance criteria not met AND "
                f"{len(not_reviewed)} children not yet in in_review/done stage."
            )
        elif unmet_count > 0:
            lines.append(
                f"{unmet_count} of {len(ac_results)} acceptance criteria for "
                f"work item {issue.get('id', '?')} are not met."
            )
        else:
            lines.append(
                f"{len(not_reviewed)} children not yet in in_review/done stage."
            )

    lines.append("")
    lines.append("## Acceptance Criteria Status")
    lines.append("")
    lines.append("| # | Criterion | Verdict | Evidence |")
    lines.append("|---|-----------|---------|----------|")

    if ac_results and ac_results[0].get("text") == "No acceptance criteria defined.":
        lines.append("")
        lines.append("No acceptance criteria defined.")
    else:
        for i, r in enumerate(ac_results, 1):
            evidence = r.get("evidence", "") or ""
            lines.append(
                f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
            )

    lines.append("")
    lines.append("## Children Status")
    lines.append("")

    if not child_results:
        lines.append("No children.")
    else:
        capped = len(child_results) > _CHILDREN_CAP
        reviewed = child_results[:_CHILDREN_CAP]
        for child in reviewed:
            lines.append(
                f"### {child['title']} ({child['id']}) — "
                f"{child['status']}/{child['stage']}"
            )
            lines.append("")
            if child.get("ac_results"):
                lines.append("| # | Criterion | Verdict | Evidence |")
                lines.append("|---|-----------|---------|----------|")
                for i, r in enumerate(child["ac_results"], 1):
                    evidence = r.get("evidence", "") or ""
                    lines.append(
                        f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
                    )
            else:
                lines.append("No acceptance criteria defined.")
            lines.append("")

        if capped:
            remaining = len(child_results) - _CHILDREN_CAP
            lines.append(
                f"*{_CHILDREN_CAP} children reviewed; {remaining} omitted for brevity.*"
            )

    lines.append("")
    return "\n".join(lines)


def _assemble_child_audit_report(child: dict, ac_results: list[dict]) -> str:
    """Assemble an audit report for a single child work item.

    *child* is a dict with keys ``title``, ``id``, ``status``, ``stage``.
    *ac_results* is a list of ``{"text": ..., "verdict": ..., "evidence": ...}``.
    """
    all_met = all(r["verdict"] == "met" for r in ac_results) if ac_results else False
    ready = "Yes" if all_met else "No"

    lines = [
        f"Ready to close: {ready}",
        "",
        "## Summary",
        "",
        f"Child work item audit for {child['title']} ({child['id']}). "
        f"Status: {child['status']}/{child['stage']}.",
        "",
        "## Acceptance Criteria Status",
        "",
        "| # | Criterion | Verdict | Evidence |",
        "|---|-----------|---------|----------|",
    ]

    if not ac_results:
        lines.append("")
        lines.append("No acceptance criteria defined.")
    else:
        for i, r in enumerate(ac_results, 1):
            evidence = r.get("evidence", "") or ""
            lines.append(
                f"| {i} | {r['text']} | {r['verdict']} | {evidence} |"
            )

    lines.append("")
    return "\n".join(lines)


def _persist_child_audit(
    child_id: str,
    child_title: str,
    child_status: str,
    child_stage: str,
    ac_results: list[dict],
    pi_bin: str = "pi",
) -> tuple[bool, str]:
    """Assemble and persist an audit report for a single child work item.

    Returns (success, report_text).
    On failure the report text is still returned so callers can log it.
    """
    child = {
        "title": child_title,
        "id": child_id,
        "status": child_status,
        "stage": child_stage,
    }
    report = _assemble_child_audit_report(child, ac_results)

    rc = persist_audit(child_id, report)
    success = rc == 0
    return success, report


def _assemble_project_report(summary: str, recommendation: str) -> str:
    """Assemble the canonical project-mode audit report."""
    lines = [
        "Ready to close: No",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Recommendation",
        "",
        recommendation,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Debug / debug-log helpers
# ---------------------------------------------------------------------------

def _default_debug_log_path(issue_id: str, context: str) -> Path:
    """Return a sensible default path for debug logs.

    Tests monkeypatch this helper so callers should use it rather than
    hard-coding a path.
    """
    p = REPO_ROOT / ".worklog" / f"audit_debug_{issue_id}.jsonl"
    return p


def _write_debug_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _call_pi_and_maybe_log(issue_id: str, context: str, prompt: str,
                           model: str = DEFAULT_MODEL,
                           pi_bin: str = "pi", debug_log: str | None = None) -> dict:
    """Call _call_pi and optionally write debug information to a log.

    If *debug_log* is provided the entry reason will be "debug_log" and the
    provided path will be used. If *debug_log* is not provided but the pi
    result contains diagnostic fields (``raw_stdout``/``raw_stderr``), a
    default path from ``_default_debug_log_path`` will be used and the reason
    will be "parse_failure".
    """
    result = _call_pi(prompt, model=model, pi_bin=pi_bin)

    # Decide whether to write a debug line
    reason = None
    target = None
    if debug_log:
        reason = "debug_log"
        target = Path(debug_log)
    elif isinstance(result, dict) and (result.get("raw_stdout") or result.get("raw_stderr")):
        reason = "parse_failure"
        target = _default_debug_log_path(issue_id, context)

    if reason and target:
        entry = {
            "issue_id": issue_id,
            "context": context,
            "reason": reason,
            "raw_stdout": result.get("raw_stdout"),
            "raw_stderr": result.get("raw_stderr"),
            "extracted_text": result.get("extracted_text"),
            "evidence": result.get("evidence"),
            "prompt": prompt[:1000],
        }
        try:
            _write_debug_log(target, entry)
        except Exception:
            # Debug logging must not break audit execution
            pass

    return result


# ---------------------------------------------------------------------------
# Subcommand: issue
# ---------------------------------------------------------------------------

def _build_issue_json(issue: dict, ac_results: list[dict],
                      child_results: list[dict]) -> dict:
    """Build structured JSON payload for issue-mode audit."""
    all_ac_met = all(
        r["verdict"] == "met"
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    # Check that all active children are in in_review or done stage
    active_children = [c for c in child_results if c.get("stage") not in ("", None)]
    all_children_reviewed = all(
        c.get("stage") in ("in_review", "done")
        for c in active_children
    )
    ready = all_ac_met and all_children_reviewed
    return {
        "ready_to_close": ready,
        "summary": (
            f"All {len(ac_results)} acceptance criteria met."
            if all_ac_met else
            f"{sum(1 for r in ac_results if r['verdict'] != 'met')} of {len(ac_results)} acceptance criteria not met."
        ),
        "acceptance_criteria": ac_results,
        "children": child_results,
    }


def cmd_issue(issue_id: str, persist: bool = True,
              pi_bin: str = "pi", model: str | None = None,
              model_source: str = DEFAULT_MODEL_SOURCE,
              runner: Runner | None = None, json_mode: bool = False,
              debug_log: str | None = None) -> int:
    """Audit a single work item.

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL
    """
    # Resolve the effective model from config + CLI
    config = _load_config()
    resolved_model = _resolve_model_for_phase(
        AUDIT_PHASE, config, model_source, cli_model=model,
    )

    if runner is None:
        runner = _default_runner

    try:
        data = _run_wl(runner, ["wl", "show", issue_id, "--children", "--json"])
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    work_item = data.get("workItem", {})
    children = data.get("children", [])
    description = work_item.get("description", "")
    acs = _extract_acs(description)

    # Review parent ACs via Pi (batched into a single call for performance)
    ac_results = []
    if acs and acs[0] != "No acceptance criteria defined.":
        ac_list_json = json.dumps([{"index": i, "text": ac} for i, ac in enumerate(acs)])
        prompt = (
            f"[READ-ONLY AUDIT] You are performing a read-only audit. "
            f"Do NOT close, modify, create, or delete any work items. "
            f"Do NOT execute any wl, git, or other state-modifying commands. "
            f"Return ONLY a structured JSON array.\n\n"
            f"Review the following acceptance criteria against the codebase. "
            f"Return ONLY a JSON array of objects, each with keys 'index' (integer), "
            f"'verdict' (one of: met, unmet, partial) and 'evidence' "
            f"(a one-line note with file:line reference).\n\n"
            f"Criteria: {ac_list_json}"
        )
        try:
            result = _call_pi_and_maybe_log(issue_id, "parent", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        # Parse the batched result - try to extract JSON array from text
        # Use extracted_text (full response) instead of evidence (may be truncated)
        raw_text = result.get("extracted_text", "") or result.get("evidence", "") or result.get("text", "")
        batch = _extract_json_array(raw_text)
        if batch is None:
            # Fallback: try direct JSON parse
            try:
                batch = json.loads(raw_text)
            except json.JSONDecodeError:
                batch = []
        if isinstance(batch, list):
            reviewed = {item["index"]: item for item in batch if isinstance(item, dict) and "index" in item}
            for i, ac in enumerate(acs):
                item = reviewed.get(i, {})
                ac_results.append({
                    "text": ac,
                    "verdict": item.get("verdict", "unmet"),
                    "evidence": item.get("evidence", ""),
                })
        else:
            # Fallback: treat single result as covering all ACs equally
            verdict = result.get("verdict", "unmet")
            evidence = result.get("evidence", "")
            for ac in acs:
                ac_results.append({"text": ac, "verdict": verdict, "evidence": evidence})
    else:
        ac_results = [{"text": "No acceptance criteria defined.", "verdict": "unmet", "evidence": ""}]

    # Review children (depth 1 only, skip completed/done, ignore deleted)
    # Pass ALL active children to the assembler; it handles the cap.
    child_results = []
    active_children = [
        c for c in children
        if not c.get("deletedBy") and c.get("status") != "completed"
    ]
    for child in active_children:
        child_desc = child.get("description", "")
        child_acs = _extract_acs(child_desc)
        child_ac_results = []
        if child_acs and child_acs[0] != "No acceptance criteria defined.":
            # Batch child ACs into a single pi call
            child_ac_list = json.dumps([{"index": i, "text": ac} for i, ac in enumerate(child_acs)])
            prompt = (
                f"[READ-ONLY AUDIT] You are performing a read-only audit. "
                f"Do NOT close, modify, create, or delete any work items. "
                f"Do NOT execute any wl, git, or other state-modifying commands. "
                f"Return ONLY a structured JSON array.\n\n"
                f"Review the following acceptance criteria for child work item '{child.get('title', '')}' "
                f"against the codebase. "
                f"Return ONLY a JSON array of objects, each with keys 'index' (integer), "
                f"'verdict' (one of: met, unmet, partial) and 'evidence' "
                f"(a one-line note with file:line reference).\n\n"
                f"Criteria: {child_ac_list}"
            )
            try:
                result = _call_pi_and_maybe_log(issue_id, f"child:{child.get('id', '')}", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            # Use extracted_text (full response) instead of evidence (may be truncated)
            raw_text = result.get("extracted_text", "") or result.get("evidence", "") or result.get("text", "")
            batch = _extract_json_array(raw_text)
            if batch is None:
                try:
                    batch = json.loads(raw_text)
                except json.JSONDecodeError:
                    batch = []
            if isinstance(batch, list):
                reviewed = {item["index"]: item for item in batch if isinstance(item, dict) and "index" in item}
                for i, ac in enumerate(child_acs):
                    item = reviewed.get(i, {})
                    child_ac_results.append({
                        "text": ac,
                        "verdict": item.get("verdict", "unmet"),
                        "evidence": item.get("evidence", ""),
                    })
            else:
                verdict = result.get("verdict", "unmet")
                evidence = result.get("evidence", "")
                for ac in child_acs:
                    child_ac_results.append({"text": ac, "verdict": verdict, "evidence": evidence})
        child_results.append({
            "title": child.get("title", ""),
            "id": child.get("id", ""),
            "status": child.get("status", ""),
            "stage": child.get("stage", ""),
            "ac_results": child_ac_results,
        })

    # Initialize child_persist_results for reporting
    child_persist_results = []

    # Persist child audits to individual child work items (if persist is True)
    if persist:
        for child in child_results:
            child_success, child_report = _persist_child_audit(
                child_id=child["id"],
                child_title=child["title"],
                child_status=child["status"],
                child_stage=child["stage"],
                ac_results=child["ac_results"],
                pi_bin=pi_bin,
            )
            child_persist_results.append({
                "id": child["id"],
                "title": child["title"],
                "success": child_success,
            })
            if not child_success:
                print(
                    f"Warning: Failed to persist audit for child {child['id']} "
                    f"({child['title']}): wl returned exit code {1}",
                    file=sys.stderr,
                )

    # Assemble and output report
    report = _assemble_issue_report(work_item, ac_results, child_results)

    if json_mode:
        payload = _build_issue_json(work_item, ac_results, child_results)
        payload["child_persist_results"] = child_persist_results
        print(json.dumps(payload, indent=2))
    else:
        print(report, end="")

    if persist:
        return persist_audit(issue_id, report)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: project
# ---------------------------------------------------------------------------

def _build_project_json(summary: str, recommendation: str) -> dict:
    """Build structured JSON payload for project-mode audit."""
    return {
        "ready_to_close": False,
        "summary": summary,
        "recommendation": recommendation,
    }


def cmd_project(pi_bin: str = "pi", model: str | None = None,
                model_source: str = DEFAULT_MODEL_SOURCE,
                runner: Runner | None = None, json_mode: bool = False,
                debug_log: str | None = None) -> int:
    """Audit the overall project.

    Model resolution order (highest first):
      1. --model CLI flag (explicit override)
      2. Config-driven: model.audit from .ralph.json resolved via model_source
      3. Hardcoded fallback: DEFAULT_MODEL
    """
    # Resolve the effective model from config + CLI
    config = _load_config()
    resolved_model = _resolve_model_for_phase(
        AUDIT_PHASE, config, model_source, cli_model=model,
    )

    if runner is None:
        runner = _default_runner

    try:
        data = _run_wl(runner, ["wl", "list", "--json"])
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    work_items = data.get("workItems", data) if isinstance(data, dict) else data
    in_progress = [w for w in work_items if w.get("status") == "in_progress"] if isinstance(work_items, list) else []
    blocked = [w for w in work_items if w.get("status") == "blocked"] if isinstance(work_items, list) else []
    completed = [w for w in work_items if w.get("status") == "completed"] if isinstance(work_items, list) else []

    summary = (
        f"Project-level audit: {len(in_progress)} items in progress, "
        f"{len(blocked)} blocked, {len(completed)} completed."
    )

    if blocked:
        blocked_ids = ", ".join(w.get("id", "?") for w in blocked[:5])
        recommendation = (
            f"Review blocked items {blocked_ids} to unblock progress."
        )
    else:
        recommendation = "No specific recommendations at this time."

    # Optional: call Pi for project-level summary
    prompt = (
        f"[READ-ONLY AUDIT] You are performing a read-only audit. "
        f"Do NOT close, modify, create, or delete any work items. "
        f"Do NOT execute any wl, git, or other state-modifying commands. "
        f"Return ONLY a structured JSON object.\n\n"
        f"Provide a brief project status summary based on: {summary}. "
        f"Then provide a recommendation. "
        f"Return ONLY a JSON object with keys 'summary' and 'recommendation'."
    )
    try:
        pi_result = _call_pi_and_maybe_log("project", "project", prompt, model=resolved_model, pi_bin=pi_bin, debug_log=debug_log)
        if pi_result.get("verdict") == "met" and pi_result.get("evidence"):
            # Use Pi's response if parseable
            pass  # Could enhance this in future
    except RuntimeError:
        pass  # Pi failure is non-fatal for project mode

    if json_mode:
        payload = _build_project_json(summary, recommendation)
        print(json.dumps(payload, indent=2))
    else:
        report = _assemble_project_report(summary, recommendation)
        print(report, end="")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audit runner for Worklog work items")
    sub = p.add_subparsers(dest="command")

    p_issue = sub.add_parser("issue", help="Audit a single work item")
    p_issue.add_argument("issue_id", help="Work item id to audit")
    p_issue.add_argument("--do-not-persist", action="store_true",
                         help="Do not persist the audit report via wl update")
    p_issue.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_issue.add_argument("--model", default=None,
                         help="Pi model to use for review (default: resolved from .ralph.json)")
    p_issue.add_argument("--model-source", default=DEFAULT_MODEL_SOURCE,
                         choices=sorted(MODEL_SOURCES),
                         help="Model source: remote or local (default: local)")
    p_issue.add_argument("--json", action="store_true",
                         help="Emit machine-readable JSON output instead of markdown")
    p_issue.add_argument("--debug-log", default=None,
                         help="Append Pi debug output to this file (JSONL)")

    p_project = sub.add_parser("project", help="Audit the overall project")
    p_project.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_project.add_argument("--model", default=None,
                           help="Pi model to use for review (default: resolved from .ralph.json)")
    p_project.add_argument("--model-source", default=DEFAULT_MODEL_SOURCE,
                           choices=sorted(MODEL_SOURCES),
                           help="Model source: remote or local (default: local)")
    p_project.add_argument("--json", action="store_true",
                           help="Emit machine-readable JSON output instead of markdown")
    p_project.add_argument("--debug-log", default=None,
                           help="Append Pi debug output to this file (JSONL)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    if args.command == "issue":
        return cmd_issue(args.issue_id, persist=not args.do_not_persist,
                         pi_bin=args.pi_bin, model=args.model,
                         model_source=args.model_source, json_mode=args.json,
                         debug_log=args.debug_log)
    elif args.command == "project":
        return cmd_project(pi_bin=args.pi_bin, model=args.model,
                           model_source=args.model_source, json_mode=args.json,
                           debug_log=args.debug_log)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
