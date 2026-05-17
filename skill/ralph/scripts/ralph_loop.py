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
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

logger = logging.getLogger("ralph")

DEFAULT_MODEL = "opencode-go/glm-5.1"
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
    """Resolve the model to use: CLI flag > config file > default."""
    if cli_model:
        return cli_model
    if config_model:
        return config_model
    return DEFAULT_MODEL


def _build_remediation_prompt(findings: Iterable[CriterionResult], audit_text: str = "") -> str:
    """Build a prompt for the implement step that addresses audit failures.

    Includes the full audit text so the agent can see the detailed evidence
    and reasoning, plus a structured list of unmet/partial criteria.
    """
    items = list(findings)
    if not items and not audit_text:
        return ""
    lines = ["The previous audit found issues that need to be fixed. Address these problems:"]
    if items:
        for idx, finding in enumerate(items, start=1):
            lines.append(f"{idx}. [{finding.verdict}] {finding.text} ({finding.evidence})")
    if audit_text:
        lines.append("")
        lines.append("Full audit report:")
        lines.append(audit_text)
    return "\n".join(lines)


def _parse_pi_json_line(line: str) -> tuple[str, bool, str | None]:
    """Parse a single JSON line from pi --mode json and extract user-facing text.

    Pi's JSON streaming protocol uses typed events:
    - thinking_start/thinking_delta/thinking_end: internal reasoning (suppressed)
    - text_delta: additive user-facing text (shown on console)
    - text_end: complete content block (captured for return value)
    - toolcall_start/delta/end: tool calls (suppressed)
    - tool_execution_*: tool results (suppressed)
    - session/agent_start/agent_end/turn_start/end/message_start/end: metadata

    For streaming, text_delta events are printed additively.
    For the return value, text_end and agent_end events provide complete text
    blocks that replace any accumulated deltas for that content index.

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
            # Other assistant events — suppress
            return "", False, None
        return "", False, None

    # --- Agent end: final message with complete content ---
    # Only extract text from the LAST assistant message — this is the final,
    # authoritative response. Earlier assistant messages may contain tool calls
    # or intermediate text that should not be included in the audit output.
    if event_type == "agent_end":
        messages = obj.get("messages", [])
        if isinstance(messages, list):
            # Find the last assistant message with text content
            last_assistant_text: str | None = None
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", [])
                if isinstance(content, list):
                    parts: list[str] = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                parts.append(text)
                    if parts:
                        last_assistant_text = "\n".join(parts)
                        break
                elif isinstance(content, str) and content:
                    last_assistant_text = content
                    break
            if last_assistant_text:
                return "", False, last_assistant_text
        return "", False, None

    # --- Structural events: suppress all ---
    if event_type in (
        "session", "agent_start", "turn_start", "turn_end",
        "message_start", "message_end",
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


def _comment_hash(audit_text: str) -> str:
    return hashlib.sha256(audit_text.encode("utf-8")).hexdigest()[:16]


class RalphLoop:
    def __init__(
        self,
        runner: Runner | None = None,
        pi_bin: str = "pi",
        wl_bin: str = "wl",
        model: str | None = None,
        check_cmds: list[str] | None = None,
        max_attempts: int = 10,
        confirm_merge: bool = False,
        cancel_file: str | None = None,
        verbose: bool = False,
        stream: bool = True,
    ):
        self.runner = runner or _default_runner
        self.pi_bin = pi_bin
        self.wl_bin = wl_bin
        self.model = model or DEFAULT_MODEL
        self.max_attempts = max_attempts
        self.confirm_merge = confirm_merge
        self.cancel_file = cancel_file
        self.check_cmds = check_cmds or []
        self.verbose = verbose
        # When stream=True (default for production), pi subprocess output is
        # echoed to stdout in real-time. When stream=False (tests), the mock
        # runner is used instead.
        self.stream = stream

    def _wl_show(self, work_item_id: str, children: bool = False) -> dict:
        cmd = [self.wl_bin, "show", work_item_id, "--json"]
        if children:
            cmd.insert(3, "--children")
        logger.debug("ralph.cmd.wl.show cmd=%s", cmd)
        result = _run_json(self.runner, cmd)
        if self.verbose:
            item = result.get("workItem", {})
            logger.debug("ralph.cmd.wl.show id=%s stage=%s status=%s children=%d", item.get("id"), item.get("stage"), item.get("status"), len(result.get("children", [])))
        return result

    def _wl_comment_list(self, work_item_id: str) -> list[dict]:
        cmd = [self.wl_bin, "comment", "list", work_item_id, "--json"]
        logger.debug("ralph.cmd.wl.comment_list cmd=%s", cmd)
        data = _run_json(self.runner, cmd)
        comments = data.get("comments", [])
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
        _run_json(self.runner, cmd)

    def _wl_update_audit(self, work_item_id: str, audit_text: str) -> None:
        if len(audit_text) > self._MAX_ARG_LEN:
            self._wl_update_audit_via_file(work_item_id, audit_text)
        else:
            cmd = [self.wl_bin, "update", work_item_id, "--audit-text", audit_text, "--json"]
            logger.debug("ralph.cmd.wl.update_audit target=%s text_len=%d", work_item_id, len(audit_text))
            _run_json(self.runner, cmd)

    def _wl_update_audit_via_file(self, work_item_id: str, audit_text: str) -> None:
        """Write audit text to a temp file and use --audit-file to avoid arg length limits."""
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(audit_text)
            f.flush()
            tmp_path = f.name
        try:
            cmd = [self.wl_bin, "update", work_item_id, "--audit-file", tmp_path, "--json"]
            logger.debug("ralph.cmd.wl.update_audit target=%s text_len=%d via_file=%s", work_item_id, len(audit_text), tmp_path)
            _run_json(self.runner, cmd)
        finally:
            os.unlink(tmp_path)

    def _run_pi(self, prompt: str) -> str:
        cmd = [self.pi_bin, "-p", "--mode", "json", "--model", self.model, prompt]
        logger.debug("ralph.cmd.pi.run model=%s prompt_len=%d", self.model, len(prompt))
        if self.verbose:
            logger.debug("ralph.cmd.pi.run prompt_full=\n%s", prompt)

        if self.stream:
            return self._stream_pi(cmd, prompt)

        proc = self.runner(cmd)
        if proc.returncode != 0:
            if self.verbose:
                logger.debug("ralph.cmd.pi.run stderr=%s", proc.stderr.strip()[:1000])
            raise RalphError(f"pi run failed: {proc.stderr.strip()}")
        text = _extract_text_from_json_output(proc.stdout)
        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(text), text[:1000])
        return text

    def _stream_pi(self, cmd: list[str], prompt: str) -> str:
        """Run pi with --mode json and stream user-facing text to the console.

        Only text_delta events (the agent's actual response) are printed.
        Thinking, metadata, and structural events are suppressed.
        Non-JSON lines are printed as a fallback.
        In verbose mode, raw JSON lines are also logged at DEBUG level.
        """
        logger.info("ralph.cmd.pi.stream_start model=%s cmd_len=%d", self.model, len(cmd))
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
        json_lines_seen = 0
        text_lines_seen = 0
        for line in process.stdout:
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

        process.wait()
        stderr = process.stderr.read()

        if process.returncode != 0:
            if self.verbose:
                logger.debug("ralph.cmd.pi.run stderr=%s", stderr.strip()[:1000])
            raise RalphError(f"pi run failed: {stderr.strip()}")

        # Prefer complete text blocks from text_end/agent_end events over
        # accumulated deltas — they give us the full, assembled content.
        # Use only the LAST complete block — it's the final, authoritative
        # response (earlier blocks may be intermediate text from tool-use turns).
        if complete_blocks:
            full_text = complete_blocks[-1]
        else:
            full_text = "".join(text_parts)

        # If JSON lines were seen but no text extracted, warn
        if json_lines_seen > 0 and text_lines_seen == 0 and not complete_blocks:
            logger.warning(
                "ralph.cmd.pi.no_text_extracted json_lines=%d — "
                "pi produced JSON but no user-facing text content was found",
                json_lines_seen,
            )
        elif json_lines_seen > 0 and len(full_text) < 50 and not complete_blocks:
            logger.warning(
                "ralph.cmd.pi.very_short_text json_lines=%d text_len=%d — "
                "pi produced very little text content, audit may be incomplete",
                json_lines_seen, len(full_text),
            )

        if self.verbose:
            logger.debug("ralph.cmd.pi.run text_len=%d text_start=%s", len(full_text), full_text[:1000])

        logger.info("ralph.cmd.pi.stream_end returncode=%d text_len=%d", process.returncode, len(full_text))
        return full_text

    def _run_checks(self) -> None:
        for cmd in self.check_cmds:
            logger.debug("ralph.cmd.check cmd=%s", cmd)
            proc = self.runner(["bash", "-lc", cmd])
            if self.verbose:
                logger.debug("ralph.cmd.check stdout=%s", proc.stdout.strip()[:1000])
                logger.debug("ralph.cmd.check stderr=%s", proc.stderr.strip()[:1000])
            if proc.returncode != 0:
                raise RalphError(f"Check failed ({cmd}): {proc.stderr.strip() or proc.stdout.strip()}")

    def _run_merge(self) -> None:
        if not self.confirm_merge:
            return
        for cmd in (
            ["git", "fetch", "origin", "main"],
            ["git", "merge", "--ff-only", "origin/main"],
            ["git", "push", "origin", "HEAD"],
        ):
            logger.debug("ralph.cmd.merge step=%s", shlex.join(cmd))
            proc = self.runner(cmd)
            if self.verbose:
                logger.debug("ralph.cmd.merge stdout=%s", proc.stdout.strip()[:1000])
            if proc.returncode != 0:
                if self.verbose:
                    logger.debug("ralph.cmd.merge stderr=%s", proc.stderr.strip()[:1000])
                raise RalphError(f"Merge step failed ({' '.join(cmd)}): {proc.stderr.strip()}")

    def _append_ampa_comment_once(self, work_item_id: str, audit_text: str) -> None:
        digest = _comment_hash(audit_text)
        marker = f"audit-hash:{digest}"
        for existing in self._wl_comment_list(work_item_id):
            if marker in (existing.get("comment") or ""):
                return
        comment = "\n".join(
            [
                "# AMPA Audit Result",
                f"{marker}",
                "",
                audit_text,
            ]
        )
        self._wl_comment_add(work_item_id, comment)

    def _scope_ids(self, target_id: str) -> list[str]:
        data = self._wl_show(target_id, children=True)
        scope = [target_id]
        scope.extend(child["id"] for child in data.get("children", []))
        return scope

    def _assert_precondition(self, target_id: str) -> None:
        item = self._wl_show(target_id).get("workItem", {})
        stage = item.get("stage", "unknown")
        if stage not in {"plan_complete", "in_review"}:
            raise RalphError(
                f"Target {target_id} must be stage plan_complete or in_review before running ralph; "
                f"current stage is {stage}."
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

    def run(self, target_id: str) -> dict:
        self._assert_precondition(target_id)
        scope_ids = self._scope_ids(target_id)

        # If the target is already in_review, skip the first implement pass and
        # go straight to audit. If audit passes, we're done. If it fails, we
        # fall into the normal implement→audit loop.
        target_item = self._wl_show(target_id).get("workItem", {})
        target_stage = target_item.get("stage", "unknown")
        skip_implement = target_stage == "in_review"
        remediation = ""

        logger.info(
            "ralph.loop.start target=%s scope=%s max_attempts=%d skip_implement=%s",
            target_id, scope_ids, self.max_attempts, skip_implement,
        )

        for attempt in range(1, self.max_attempts + 1):
            if self.cancel_file and os.path.exists(self.cancel_file):
                logger.info("ralph.loop.cancelled target=%s attempt=%d", target_id, attempt)
                return {"status": "cancelled", "attempt": attempt, "scope": scope_ids}

            logger.info("ralph.loop.attempt.start target=%s attempt=%d", target_id, attempt)

            if skip_implement and attempt == 1:
                # Target already in_review — audit first, only implement if audit fails
                logger.info("ralph.loop.skip_implement target=%s stage=in_review", target_id)
            else:
                prompt_parts = [
                    f"implement {target_id}",
                    f"Target scope includes direct children only: {', '.join(scope_ids[1:]) or '(none)'}.",
                    "Continue until scope items are in_review, but do not merge.",
                ]
                if remediation:
                    prompt_parts.append(remediation)
                self._run_pi("\n".join(prompt_parts))

            logger.info("ralph.loop.audit.start target=%s attempt=%d", target_id, attempt)
            audit_output = self._run_pi(f"/audit {target_id}")
            if self.verbose:
                logger.debug("ralph.loop.audit.raw_output target=%s attempt=%d len=%d output_start=%s", target_id, attempt, len(audit_output), audit_output[:1000])
            self._wl_update_audit(target_id, audit_output)
            self._append_ampa_comment_once(target_id, audit_output)
            audit = parse_audit_report(audit_output)
            if self.verbose:
                logger.debug("ralph.loop.audit.parsed target=%s attempt=%d ready=%s criteria_count=%d unmet=%d", target_id, attempt, audit.ready_to_close, len(audit.criteria), len(audit.unmet_or_partial))

            logger.info(
                "ralph.loop.audit.complete target=%s attempt=%d ready=%s unmet=%d criteria=%d",
                target_id, attempt, audit.ready_to_close, len(audit.unmet_or_partial), len(audit.criteria),
            )

            if audit.ready_to_close and self._scope_in_review(scope_ids):
                logger.info("ralph.loop.checks.start target=%s", target_id)
                self._run_checks()
                logger.info("ralph.loop.merge target=%s confirm=%s", target_id, self.confirm_merge)
                self._run_merge()
                return {
                    "status": "success",
                    "attempt": attempt,
                    "scope": scope_ids,
                    "merge_offered": True,
                    "merge_executed": self.confirm_merge,
                }

            remediation = _build_remediation_prompt(audit.unmet_or_partial, audit_text=audit_output)
            logger.info(
                "ralph.loop.remediate target=%s attempt=%d unmet_count=%d remediation_len=%d",
                target_id, attempt, len(audit.unmet_or_partial), len(remediation),
            )
            if not remediation:
                logger.warning(
                    "ralph.loop.no_remediation target=%s attempt=%d — audit found no unmet criteria but ready_to_close is False",
                    target_id, attempt,
                )

        logger.warning("ralph.loop.max_attempts target=%s", target_id)
        return {"status": "max_attempts", "attempt": self.max_attempts, "scope": scope_ids}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Ralph implement→audit orchestration loop")
    parser.add_argument("work_item_id", help="Target Worklog item id")
    parser.add_argument("--max-attempts", type=int, default=10)
    parser.add_argument("--check-cmd", action="append", default=[], help="Build/test command to run on success")
    parser.add_argument("--confirm-merge", action="store_true", help="Execute merge/push steps after successful audit")
    parser.add_argument("--cancel-file", default=None, help="Path checked each attempt; if present, stop loop")
    parser.add_argument("--quiet", action="store_true", help="Suppress console progress output and pi streaming (only print final JSON result)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed delegation commands and subprocess output")
    parser.add_argument("--no-stream", action="store_true", help="Don't stream pi subprocess output to console (use buffered capture instead)")
    parser.add_argument("--model", default=None, help=f"Model to use for pi run (default: {DEFAULT_MODEL}, or 'model' key in .ralph.json)")
    parser.add_argument("--pi-bin", default="pi")
    parser.add_argument("--wl-bin", default="wl")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure console logging based on verbosity.
    #   --quiet    : WARNING only, no progress, no pi streaming
    #   (default)  : INFO — lifecycle progress (attempt, audit, merge) + pi streaming
    #   --verbose  : DEBUG — adds delegated commands, subprocess output, raw audit
    #   --no-stream: disable pi stdout streaming (use buffered capture)
    if not args.quiet:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logging.getLogger("ralph").addHandler(handler)
    if args.verbose:
        logging.getLogger("ralph").setLevel(logging.DEBUG)
    else:
        logging.getLogger("ralph").setLevel(logging.INFO)

    loop = RalphLoop(
        pi_bin=args.pi_bin,
        wl_bin=args.wl_bin,
        model=_resolve_model(args.model, _load_config().get("model")),
        check_cmds=args.check_cmd,
        max_attempts=args.max_attempts,
        confirm_merge=args.confirm_merge,
        cancel_file=args.cancel_file,
        verbose=args.verbose,
        stream=not args.quiet and not args.no_stream,
    )
    try:
        result = loop.run(args.work_item_id)
    except RalphError as exc:
        print(f"ralph: {exc}")
        return 2

    print(json.dumps(result, indent=2))
    if result.get("status") == "success":
        return 0
    if result.get("status") == "cancelled":
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
