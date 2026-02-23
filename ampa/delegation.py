"""Delegation orchestration extracted from ampa.scheduler.

Provides DelegationOrchestrator which encapsulates the delegation-specific
flows: pre/post reports, idle delegation execution, report building and
discord/webhook interactions. The implementation mirrors behavior formerly
contained in ampa/scheduler.py so callers can opt-in by constructing and
invoking this class from Scheduler.

This module intentionally keeps no external side-effects at import time so it
is safe to import from tests and other modules.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional

from .engine.candidates import CandidateSelector
from .engine.core import Engine, EngineResult, EngineStatus
from . import webhook as webhook_module
from . import selection

LOG = logging.getLogger("ampa.delegation")


def _utc_now():
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc)


def _trim_text(value: Optional[str]) -> str:
    return value.strip() if value else ""


def _content_hash(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _summarize_for_discord(text: Optional[str], max_chars: int = 1000) -> str:
    if not text:
        return ""
    try:
        if len(text) <= max_chars:
            return text
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
                proc.returncode,
                proc.stderr,
            )
            return text
        summary = (proc.stdout or "").strip()
        return summary or text
    except Exception:
        LOG.exception("Failed to summarize content for Discord")
        return text or ""


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


def _build_delegation_report(
    *,
    in_progress_output: str,
    candidates: List[Dict[str, Any]],
    top_candidate: Optional[Dict[str, Any]],
) -> str:
    in_progress_items = _format_in_progress_items(in_progress_output)
    if in_progress_items:
        lines: List[str] = ["Agents are currently busy with:"]
        for item in in_progress_items:
            lines.append(f"── {item}")
        return "\n".join(lines)

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


class DelegationOrchestrator:
    def __init__(
        self,
        store,
        run_shell: Callable[..., subprocess.CompletedProcess],
        command_cwd: str,
        engine: Optional[Engine] = None,
        candidate_selector: Optional[CandidateSelector] = None,
    ) -> None:
        self.store = store
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self.engine = engine
        self._candidate_selector = candidate_selector

    def _is_delegation_report_changed(self, command_id: str, report_text: str) -> bool:
        new_hash = _content_hash(report_text)
        state = self.store.get_state(command_id)
        old_hash = state.get("last_delegation_report_hash")
        if old_hash == new_hash:
            LOG.info(
                "Delegation report unchanged (hash=%s); suppressing Discord webhook",
                new_hash[:12],
            )
            return False
        state["last_delegation_report_hash"] = new_hash
        self.store.update_state(command_id, state)
        LOG.info("Delegation report changed (new=%s)", new_hash[:12])
        return True

    def _run_delegation_report(self) -> Optional[str]:
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

    def _run_idle_delegation(self, audit_only: bool) -> Dict[str, Any]:
        if audit_only:
            return {
                "note": "Delegation: skipped (audit_only)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }
        if not self.engine:
            raise RuntimeError("Engine not configured for delegation")
        try:
            result = self.engine.process_delegation()
        except Exception:
            LOG.exception("Engine process_delegation raised an exception")
            return {
                "note": "Delegation: engine error",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
                "error": "engine exception",
            }

        status = result.status
        if status == EngineStatus.SUCCESS:
            action = result.action or "unknown"
            wid = result.work_item_id or "?"
            delegate_title = ""
            if result.candidate_result and result.candidate_result.selected:
                delegate_title = result.candidate_result.selected.title
            delegate_info = {"action": action, "id": wid, "title": delegate_title}
            if result.dispatch_result:
                delegate_info["pid"] = result.dispatch_result.pid
            return {
                "note": f"Delegation: dispatched {action} {wid}",
                "dispatched": True,
                "delegate_info": delegate_info,
                "rejected": self._engine_rejections(result),
                "idle_webhook_sent": False,
            }

        if status == EngineStatus.NO_CANDIDATES:
            return {
                "note": "Delegation: skipped (no wl next candidates)",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_webhook_sent": True,
            }

        if status == EngineStatus.SKIPPED:
            return {
                "note": f"Delegation: skipped ({result.reason})",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }

        if status in (EngineStatus.REJECTED, EngineStatus.INVARIANT_FAILED):
            return {
                "note": f"Delegation: blocked ({result.reason})",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_webhook_sent": False,
            }

        if status == EngineStatus.DISPATCH_FAILED:
            return {
                "note": f"Delegation: failed ({result.reason})",
                "dispatched": False,
                "rejected": self._engine_rejections(result),
                "idle_webhook_sent": False,
                "error": result.reason,
            }

        return {
            "note": f"Delegation: engine error ({result.reason})",
            "dispatched": False,
            "rejected": self._engine_rejections(result),
            "idle_webhook_sent": False,
            "error": result.reason,
        }

    @staticmethod
    def _engine_rejections(result: EngineResult) -> List[Dict[str, str]]:
        rejected: List[Dict[str, str]] = []
        cr = getattr(result, "candidate_result", None)
        if cr is None:
            return rejected
        for rej in getattr(cr, "rejections", ()):
            c = getattr(rej, "candidate", None)
            rejected.append(
                {
                    "id": getattr(c, "id", "?") if c else "?",
                    "title": getattr(c, "title", "(unknown)") if c else "(unknown)",
                    "reason": getattr(rej, "reason", "rejected"),
                }
            )
        return rejected

    def execute(self, spec: Any, audit_only: bool = False) -> Dict[str, Any]:
        """Run delegation flow for a delegation CommandSpec.

        Returns the structured result dict used by Scheduler.start_command.
        """
        # Pre-dispatch report when there is no imminent dispatch (caller can
        # decide when to call execute to match prior behaviour).
        report = None
        try:
            report = self._run_delegation_report()
        except Exception:
            LOG.exception("Delegation report generation failed")

        sent_pre_report = False
        if report:
            LOG.info("Pre-dispatch delegation report generated (len=%d)", len(report))
            sent_pre_report = True
            try:
                webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                if webhook and self._is_delegation_report_changed(
                    spec.command_id, report
                ):
                    message = _summarize_for_discord(report, max_chars=1000)
                    payload = webhook_module.build_command_payload(
                        os.uname().nodename,
                        _utc_now().isoformat(),
                        spec.command_id,
                        message,
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
                LOG.exception("Delegation discord notification failed")

        # run idle delegation
        inspect_status = None
        try:
            # Lightweight pre-flight: use candidate_selector when available
            if self._candidate_selector:
                sel = self._candidate_selector.select()
                if sel.global_rejections:
                    for r in sel.global_rejections:
                        if "in-progress" in r.lower() or "in_progress" in r.lower():
                            inspect_status = "in_progress"
                            break
                if inspect_status is None:
                    if sel.selected is None:
                        inspect_status = "idle_no_candidate"
                    else:
                        inspect_status = "idle_with_candidate"
            else:
                inspect_status = None
        except Exception:
            LOG.exception("CandidateSelector.select() raised during inspect")
            inspect_status = None

        if inspect_status == "in_progress":
            LOG.info("Delegation skipped because work is in-progress")
            return {
                "note": "Delegation: skipped (in_progress items)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }

        if inspect_status == "idle_no_candidate":
            idle_msg = "Agents are idle: no actionable items found"
            LOG.info("Delegation: idle_no_candidate - %s", idle_msg)
            if not sent_pre_report:
                try:
                    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                    if webhook and self._is_delegation_report_changed(
                        spec.command_id, idle_msg
                    ):
                        payload = webhook_module.build_command_payload(
                            os.uname().nodename,
                            _utc_now().isoformat(),
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
                        return {
                            "note": "Delegation: skipped (no actionable candidates)",
                            "dispatched": False,
                            "rejected": [],
                            "idle_webhook_sent": True,
                        }
                except Exception:
                    LOG.exception("Failed to send idle-state webhook")
            return {
                "note": "Delegation: skipped (no actionable candidates)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }

        # otherwise attempt real delegation
        result = self._run_idle_delegation(audit_only=audit_only)

        # if dispatched, send post-delegation report
        try:
            if result.get("dispatched"):
                post_report = self._run_delegation_report()
                if post_report:
                    # update stored hash so next cycle compares against post state
                    self._is_delegation_report_changed(spec.command_id, post_report)
                    post_message = _summarize_for_discord(post_report, max_chars=1000)
                    webhook = os.getenv("AMPA_DISCORD_WEBHOOK")
                    if webhook:
                        payload = webhook_module.build_command_payload(
                            os.uname().nodename,
                            _utc_now().isoformat(),
                            spec.command_id,
                            post_message,
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
            LOG.exception("Failed to send post-delegation webhook")

        return result
