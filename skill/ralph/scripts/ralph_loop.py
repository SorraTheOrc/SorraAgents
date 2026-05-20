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
from datetime import datetime, timezone

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
    """Resolve the model to use: CLI flag > config file > default."""
    if cli_model:
        return cli_model
    if config_model:
        return config_model
    return DEFAULT_MODEL


def _build_remediation_prompt() -> str:
    """Build a prompt for the implement step that addresses audit failures."""
    return "The previous audit found issues. Address all the gaps identified in the audit."


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
        check_cmds: list[str] | None = None,
        max_attempts: int = 10,
        confirm_merge: bool = False,
        cancel_file: str | None = None,
        verbose: bool = False,
        stream: bool = True,
        autoplan_effort_skip: frozenset[str] | None = None,
        autoplan_risk_skip: frozenset[str] | None = None,
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
        # Auto-plan thresholds: effort t-shirt sizes and risk levels that
        # allow skipping /plan and proceeding directly to implement.
        self.autoplan_effort_skip = autoplan_effort_skip or DEFAULT_AUTOPLAN_EFFORT_SKIP
        self.autoplan_risk_skip = autoplan_risk_skip or DEFAULT_AUTOPLAN_RISK_SKIP
        # When True, disable the auto-plan step and proceed directly to implement
        # for intake_complete items.
        self.no_autoplan = False

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

        # Use subprocess.run for the default runner (production) with stdin,
        # or self.runner with a trailing payload argument for tests.
        if self.runner == _default_runner:
            proc = subprocess.run(
                cmd,
                input=payload,
                text=True,
                capture_output=True,
            )
        else:
            proc = self.runner(cmd + [payload])

        if proc.returncode != 0:
            logger.warning(
                "ralph.autoplan.effort_risk.failed target=%s rc=%s stderr=%s",
                target_id, proc.returncode, (proc.stderr or "")[:500],
            )
            return None

        try:
            result = json.loads(proc.stdout)
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
                    self._run_pi(f"/skill:plan {target_id}")
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
                self._run_pi(f"/skill:plan {target_id}")
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
        # Accept intake_complete as a valid entrypoint for the auto-plan flow
        if stage not in {"plan_complete", "in_review", "intake_complete"}:
            # Keep legacy phrasing for compatibility with callers/tests but mention intake_complete
            raise RalphError(
                f"Target {target_id} must be stage plan_complete or in_review (or intake_complete for auto-plan) before running ralph; current stage is {stage}."
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
        scope_ids = self._scope_ids_recursive(target_id)

        # If the target is already in_review, skip the first implement pass and
        # go straight to audit. If a persisted audit comment shows the scope is
        # up-to-date, we can skip invoking the audit skill at the start of the
        # iteration and instead rely on the persisted audit.
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

            # Whether we will rely on a persisted audit without invoking the audit skill
            use_persisted_audit = False

            if target_stage == "intake_complete" and attempt == 1 and not self.no_autoplan:
                # Auto-plan step: evaluate effort/risk and decide whether
                # to invoke /plan or proceed directly to implement.
                try:
                    do_plan, new_stage = self._run_autoplan(target_id)
                    if new_stage == "plan_complete":
                        target_stage = "plan_complete"
                except RalphError:
                    raise
                except Exception:
                    logger.exception("ralph.loop.autoplan.unexpected target=%s", target_id)
                # After autoplan (whether plan ran or not), proceed to implement
                prompt_parts = [
                    f"implement {target_id}",
                    "Continue until the work item and all dependencies are completed, but do not merge.",
                ]
                # Add a short remediation instruction if an audit produced unmet criteria previously
                if remediation:
                    prompt_parts.append(remediation)
                self._run_pi("\n".join(prompt_parts))
            elif skip_implement and attempt == 1:
                # Target already in_review — decide whether start-of-iteration audit is needed
                logger.info("ralph.loop.skip_implement target=%s stage=in_review", target_id)
                try:
                    latest_comment_ts = self._latest_audit_comment_ts_for_scope(scope_ids)
                    max_updated_at = self._max_updated_at_for_scope(scope_ids)
                    if latest_comment_ts and max_updated_at and latest_comment_ts >= max_updated_at:
                        logger.info(
                            "ralph.loop.audit.skipping_start target=%s latest_comment_ts=%s max_updated_at=%s",
                            target_id, latest_comment_ts.isoformat(), max_updated_at.isoformat()
                        )
                        use_persisted_audit = True
                except Exception:
                    logger.exception("ralph.loop.pre_audit_check_failed target=%s", target_id)
            else:
                prompt_parts = [
                    f"implement {target_id}",
                    "Continue until the work item and all dependencies are completed, but do not merge.",
                ]
                if remediation:
                    prompt_parts.append(remediation)
                self._run_pi("\n".join(prompt_parts))

            logger.info("ralph.loop.audit.start target=%s attempt=%d", target_id, attempt)
            # Run the audit skill unless we've determined that the persisted audit
            # is up-to-date and can be used without re-running the audit skill.
            if use_persisted_audit:
                logger.info("ralph.loop.audit.skipped_using_persisted target=%s attempt=%d", target_id, attempt)
            else:
                # Run the audit skill; it MUST persist the structured audit to the work item.
                self._run_pi(f"/skill:audit {target_id}")
            # Read the persisted audit from the work item via wl show.
            item = self._wl_show(target_id).get("workItem", {})
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
                raise RalphError(f"No persisted audit found for {target_id} after running /skill:audit; expected workItem.audit to contain the structured report.")
            # Validate presence of the required header
            lines = [l.strip() for l in audit_text.splitlines() if l.strip()]
            if not any(l.lower().startswith("ready to close:") for l in lines):
                excerpt = audit_text.strip().replace("\n", " ")[:200]
                raise RalphError(f"No 'Ready to close:' header found in persisted audit for {target_id}. Excerpt: {excerpt}")
            audit = parse_audit_report(audit_text)
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

            remediation = _build_remediation_prompt()
            logger.info(
                "ralph.loop.remediate target=%s attempt=%d unmet_count=%d",
                target_id, attempt, len(audit.unmet_or_partial),
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
    parser.add_argument("--no-autoplan", action="store_true", help="Disable the auto-plan step for intake_complete items (proceed directly to implement)")
    parser.add_argument("--autoplan-effort-skip", nargs="*", help="Effort t-shirt sizes that skip /plan (default: Extra Small Small)")
    parser.add_argument("--autoplan-risk-skip", nargs="*", help="Risk levels that skip /plan (default: Low)")
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
        # Emit compact JSON lines with timestamp, level, logger, and message
        class JsonLineFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts": int(record.created * 1000),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                return json.dumps(payload, ensure_ascii=False)
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
        autoplan_effort_skip=autoplan_effort_skip,
        autoplan_risk_skip=autoplan_risk_skip,
    )
    loop.no_autoplan = args.no_autoplan
    try:
        result = loop.run(args.work_item_id)
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
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
