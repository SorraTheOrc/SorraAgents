#!/usr/bin/env python3
"""Audit runner – deterministic audit orchestration.

Provides two subcommands:
  issue <id>   – audit a single work item
  project      – audit the overall project

Usage:
  audit_runner.py issue <id> [--persist] [--pi-bin pi] [--model <name>]
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
# Pi integration (duplicated from ralph for now – see OQ-1)
# ---------------------------------------------------------------------------

def _call_pi(prompt: str, model: str = "opencode-go/glm-5.1",
             pi_bin: str = "pi") -> dict:
    """Call Pi via subprocess and parse the JSON-stream response.

    Returns a dict with keys ``verdict`` and ``evidence``.
    Defaults to ``{"verdict": "unmet", "evidence": ""}`` on parse failure.

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
        return {"verdict": "unmet", "evidence": ""}

    # Parse JSON lines looking for the final agent_end message
    text = _extract_pi_text(raw)
    if not text:
        return {"verdict": "unmet", "evidence": ""}

    # Try to parse the text as JSON with verdict/evidence
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {
                "verdict": obj.get("verdict", "unmet").lower(),
                "evidence": obj.get("evidence", ""),
            }
    except json.JSONDecodeError:
        pass

    # If Pi returned free-form text, use it as evidence and default to met
    return {"verdict": "met", "evidence": text.strip()[:200]}


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
    """
    all_met = all(
        r["verdict"] == "met"
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    ready = "Yes" if all_met else "No"

    lines = [f"Ready to close: {ready}", "", "## Summary", ""]

    if all_met:
        lines.append(
            f"All {len(ac_results)} acceptance criteria for work item "
            f"{issue.get('id', '?')} are met."
        )
    else:
        unmet_count = sum(
            1 for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
            if r["verdict"] != "met"
        )
        lines.append(
            f"{unmet_count} of {len(ac_results)} acceptance criteria for "
            f"work item {issue.get('id', '?')} are not met."
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
# Subcommand: issue
# ---------------------------------------------------------------------------

def _build_issue_json(issue: dict, ac_results: list[dict],
                      child_results: list[dict]) -> dict:
    """Build structured JSON payload for issue-mode audit."""
    all_met = all(
        r["verdict"] == "met"
        for r in ac_results + [c for cr in child_results for c in cr.get("ac_results", [])]
    )
    return {
        "ready_to_close": all_met,
        "summary": (
            f"All {len(ac_results)} acceptance criteria met."
            if all_met else
            f"{sum(1 for r in ac_results if r['verdict'] != 'met')} of {len(ac_results)} acceptance criteria not met."
        ),
        "acceptance_criteria": ac_results,
        "children": child_results,
    }


def cmd_issue(issue_id: str, persist: bool = False,
              pi_bin: str = "pi", model: str = "opencode-go/glm-5.1",
              runner: Runner | None = None, json_mode: bool = False) -> int:
    """Audit a single work item."""
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
            result = _call_pi(prompt, model=model, pi_bin=pi_bin)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        # Parse the batched result
        raw_text = result.get("evidence", "") or result.get("text", "")
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
                result = _call_pi(prompt, model=model, pi_bin=pi_bin)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            raw_text = result.get("evidence", "") or result.get("text", "")
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

    # Assemble report
    if json_mode:
        payload = _build_issue_json(work_item, ac_results, child_results)
        print(json.dumps(payload, indent=2))
        report = _assemble_issue_report(work_item, ac_results, child_results)
    else:
        report = _assemble_issue_report(work_item, ac_results, child_results)
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


def cmd_project(pi_bin: str = "pi", model: str = "opencode-go/glm-5.1",
                runner: Runner | None = None, json_mode: bool = False) -> int:
    """Audit the overall project."""
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
        pi_result = _call_pi(prompt, model=model, pi_bin=pi_bin)
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
    p_issue.add_argument("--persist", action="store_true",
                         help="Persist the audit report via wl update")
    p_issue.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_issue.add_argument("--model", default="opencode-go/glm-5.1",
                         help="Pi model to use for review")
    p_issue.add_argument("--json", action="store_true",
                         help="Emit machine-readable JSON output instead of markdown")

    p_project = sub.add_parser("project", help="Audit the overall project")
    p_project.add_argument("--pi-bin", default="pi", help="Path to the pi binary (default: pi)")
    p_project.add_argument("--model", default="opencode-go/glm-5.1",
                           help="Pi model to use for review")
    p_project.add_argument("--json", action="store_true",
                           help="Emit machine-readable JSON output instead of markdown")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2

    if args.command == "issue":
        return cmd_issue(args.issue_id, persist=args.persist,
                         pi_bin=args.pi_bin, model=args.model, json_mode=args.json)
    elif args.command == "project":
        return cmd_project(pi_bin=args.pi_bin, model=args.model, json_mode=args.json)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
