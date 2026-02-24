"""Extracted triage-audit processing from scheduler.

This module provides TriageAuditRunner which encapsulates the behavior
previously implemented in Scheduler._run_triage_audit(). The implementation
keeps behaviour identical to the original to make the refactor import-only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

try:
    # relative import to allow tests to monkeypatch ampa.notifications
    from . import notifications as notifications_module
except Exception:  # pragma: no cover - defensive
    import ampa.notifications as notifications_module

LOG = logging.getLogger("ampa.triage_audit")

# ---------------------------------------------------------------------------
# Structured audit report extraction
# ---------------------------------------------------------------------------
_AUDIT_REPORT_START = "--- AUDIT REPORT START ---"
_AUDIT_REPORT_END = "--- AUDIT REPORT END ---"


def _extract_audit_report(text: str) -> str:
    """Extract the structured audit report from raw audit output.

    Looks for ``--- AUDIT REPORT START ---`` and ``--- AUDIT REPORT END ---``
    delimiter lines.  Returns the content between these markers (stripped of
    leading/trailing whitespace).

    If the start marker is missing the full *text* is returned and a warning is
    logged.  If the start marker is present but the end marker is missing, all
    content after the start marker is returned (with a warning).  If the
    extracted content is empty, the full *text* is returned with a warning.

    When multiple pairs of markers exist only the **first** pair is used.
    """
    if not text:
        return ""

    start_idx = text.find(_AUDIT_REPORT_START)
    if start_idx == -1:
        LOG.warning(
            "Audit output missing start marker (%s); using full output",
            _AUDIT_REPORT_START,
        )
        return text

    content_start = start_idx + len(_AUDIT_REPORT_START)
    end_idx = text.find(_AUDIT_REPORT_END, content_start)
    if end_idx == -1:
        LOG.warning(
            "Audit output missing end marker (%s); using content after start marker",
            _AUDIT_REPORT_END,
        )
        extracted = text[content_start:].strip()
    else:
        extracted = text[content_start:end_idx].strip()

    if not extracted:
        LOG.warning("Extracted audit report is empty; falling back to full output")
        return text

    return extracted


def _extract_summary_from_report(report: str) -> str:
    """Extract the ``## Summary`` section from a structured audit report.

    Returns the text between the ``## Summary`` heading and the next ``##``
    heading (or end of string), stripped of leading/trailing whitespace.
    Returns an empty string if no ``## Summary`` heading is found.
    """
    if not report:
        return ""
    m = re.search(r"^##\s+Summary\s*$", report, re.IGNORECASE | re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    # Find the next ## heading or end of string
    m2 = re.search(r"^##\s+", report[start:], re.MULTILINE)
    if m2:
        section = report[start : start + m2.start()]
    else:
        section = report[start:]
    return section.strip()


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
        v = value
        if isinstance(v, str) and v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return dt.datetime.fromisoformat(v)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discord formatting helpers — canonical implementations live in ampa.delegation.
# ---------------------------------------------------------------------------
from .delegation import (  # noqa: E402
    _summarize_for_discord,
    _trim_text,
)


class TriageAuditRunner:
    def __init__(
        self,
        run_shell: "Callable[..., subprocess.CompletedProcess]",
        command_cwd: str,
        store: Any,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self.store = store

    def run(self, spec: Any, run: Any, output: Optional[str]) -> bool:
        # This method is intentionally a close copy of the original
        # Scheduler._run_triage_audit implementation. It was preserved to
        # keep behavioural parity during refactor.
        try:
            default_cooldown_hours = int(spec.metadata.get("audit_cooldown_hours", 6))
        except Exception:
            default_cooldown_hours = 6
        try:
            truncate_chars = int(spec.metadata.get("truncate_chars", 65536))
        except Exception:
            truncate_chars = 65536

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

        try:
            items: List[Dict[str, Any]] = []

            proc = _call("wl list --stage in_review --json")
            if proc.returncode != 0:
                LOG.warning("wl list --stage in_review failed: %s", proc.stderr)
            else:
                try:
                    raw = json.loads(proc.stdout or "null")
                except Exception:
                    LOG.exception("Failed to parse wl list --stage in_review output")
                    raw = None
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

            unique: Dict[str, Dict[str, Any]] = {}
            for it in items:
                wid = it.get("id") or it.get("work_item_id") or it.get("work_item")
                if not wid:
                    continue
                unique[str(wid)] = {**it, "id": wid}
            items = list(unique.values())

            if not items:
                LOG.info("Triage audit found no candidates")
                return False

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
                return default_cooldown_hours

            candidates: List[tuple] = []
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

                last_audit: Optional[dt.datetime] = None
                try:
                    proc_c = _call(f"wl comment list {wid} --json")
                    if proc_c.returncode == 0 and proc_c.stdout:
                        try:
                            raw_comments = json.loads(proc_c.stdout)
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
                                c.get("comment") or c.get("body") or c.get("text") or ""
                            )
                            if not body:
                                continue
                            if "# AMPA Audit Result" not in body:
                                continue
                            cand_ts = None
                            for key in (
                                "createdAt",
                                "created_at",
                                "created_ts",
                                "created",
                                "ts",
                                "timestamp",
                            ):
                                v = c.get(key)
                                if v is None:
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
                                continue
                            if last_audit is None or cand_ts > last_audit:
                                last_audit = cand_ts
                except Exception:
                    LOG.exception("Failed to list comments for %s", wid)

                try:
                    pst = persisted_by_item.get(wid)
                    pdt = _from_iso(pst) if pst else None
                    if pdt is not None and (last_audit is None or pdt > last_audit):
                        last_audit = pdt
                except Exception:
                    LOG.debug("Failed to parse persisted last_audit for %s", wid)

                try:
                    cooldown_hours_for_item = _get_cooldown_hours_for_item(it)
                except Exception:
                    cooldown_hours_for_item = default_cooldown_hours
                cooldown_delta = dt.timedelta(hours=cooldown_hours_for_item)

                if last_audit is not None and (now - last_audit) < cooldown_delta:
                    continue

                updated = _item_updated_ts(it)
                candidates.append((updated, {**it, "id": wid}))

            if not candidates:
                LOG.info("Triage audit found no candidates after cooldown filter")
                return False

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
            LOG.info("Selected triage candidate %s — %s", work_id, title)

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

            def _extract_summary(text: str) -> str:
                if not text:
                    return ""
                m = re.search(
                    r"^(?:#{1,6}\s*)?Summary\s*:?$", text, re.IGNORECASE | re.MULTILINE
                )
                if m:
                    start = m.end()
                    rest = text[start:]
                    lines = rest.splitlines()
                    collected: List[str] = []
                    for line in lines:
                        if re.match(r"^\s*#{1,6}\s+", line):
                            break
                        if re.match(r"^[A-Z][A-Za-z0-9 \-]{0,80}\s*:$", line):
                            break
                        collected.append(line)
                    while collected and collected[0].strip() == "":
                        collected.pop(0)
                    while collected and collected[-1].strip() == "":
                        collected.pop()
                    return "\n".join(collected).strip()
                m2 = re.search(r"Summary:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
                if m2:
                    return m2.group(1).strip().split("\n\n")[0].strip()
                return ""

            if True:  # notifications_module handles availability internally
                # Try structured report summary first, then legacy regex fallback
                report_for_summary = _extract_audit_report(audit_out or "")
                summary_text = _extract_summary_from_report(report_for_summary)
                if not summary_text:
                    summary_text = _extract_summary(audit_out or "")
                if not summary_text:
                    summary_text = f"{work_id} — {title} | exit={exit_code}"
                try:
                    summary_text = _summarize_for_discord(summary_text, max_chars=1000)
                except Exception:
                    LOG.exception("Failed to summarize triage summary_text")

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
                        description_text = (
                            wi_pre.get("description") or wi_pre.get("desc") or ""
                        )
                    else:
                        description_text = ""

                    def _find_pr_in_text(text: str) -> Optional[str]:
                        if not text:
                            return None
                        m = re.search(
                            r"https?://github\.com/[^\s']+?/pull/\d+", text, re.I
                        )
                        if m:
                            return m.group(0)
                        return None

                    pr_url = _find_pr_in_text(description_text)
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
                    heading_title = f"Triage Audit — {title}"
                    extra = [{"name": "Summary", "value": summary_text}]
                    if pr_url:
                        extra.append({"name": "PR", "value": pr_url})
                    payload = notifications_module.build_payload(
                        hostname=os.uname().nodename,
                        timestamp_iso=_utc_now().isoformat(),
                        work_item_id=None,
                        extra_fields=extra,
                        title=heading_title,
                    )
                    notifications_module.notify(
                        heading_title,
                        message_type="command",
                        payload=payload,
                    )
                except Exception:
                    LOG.exception("Failed to send discord summary")

            full_output = audit_out or ""
            report = _extract_audit_report(full_output) or "(no output)"
            if len(report) <= truncate_chars:
                comment_text = report
                try:
                    comment_parts = [
                        "# AMPA Audit Result",
                        "",
                        comment_text,
                    ]
                    comment = "\n".join(comment_parts)
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
                                    r"(?i)^\s*#\s*AMPA Audit Result\s*\n*", "", body
                                ).strip()
                                if not stripped or stripped == "(no output)":
                                    LOG.error(
                                        "Posted AMPA audit comment for %s appears heading-only or empty; report_len=%d posted_body_len=%d",
                                        work_id,
                                        len(report or ""),
                                        len(body or ""),
                                    )
                    except Exception:
                        LOG.exception(
                            "Failed to verify posted WL comment for %s", work_id
                        )
            else:
                try:
                    fd, path = tempfile.mkstemp(
                        prefix=f"wl-audit-{work_id}-", suffix=".log"
                    )
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(report)
                    comment_parts = [
                        "# AMPA Audit Result",
                        "",
                        f"Audit report too large; full report saved to: {path}",
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
                                    r"(?i)^\s*#\s*AMPA Audit Result\s*\n*", "", body
                                ).strip()
                                if not stripped or stripped == "(no output)":
                                    LOG.error(
                                        "Posted AMPA audit comment (artifact path) for %s appears heading-only or empty; report_len=%d posted_body_len=%d",
                                        work_id,
                                        len(report or ""),
                                        len(body or ""),
                                    )
                    except Exception:
                        LOG.exception(
                            "Failed to verify posted WL comment for %s", work_id
                        )

            try:
                state = self.store.get_state(spec.command_id)
                if not isinstance(state, dict):
                    state = dict(state or {})
                state.setdefault("last_audit_at_by_item", {})
                state["last_audit_at_by_item"][work_id] = _to_iso(now)
                self.store.update_state(spec.command_id, state)
            except Exception:
                LOG.exception("Failed to persist last_audit_at_by_item for %s", work_id)

            try:
                proc_show = _call(f"wl show {work_id} --json")
                if proc_show.returncode == 0 and proc_show.stdout:
                    try:
                        wi_raw = json.loads(proc_show.stdout)
                    except Exception:
                        wi_raw = {}
                else:
                    wi_raw = {}

                def _children_open(wobj: Dict[str, Any]) -> bool:
                    for key in (
                        "children",
                        "workItems",
                        "work_items",
                        "items",
                        "subtasks",
                    ):
                        val = wobj.get(key)
                        if isinstance(val, list) and val:
                            for c in val:
                                st = c.get("status") or c.get("state") or c.get("stage")
                                if st and str(st).lower() not in (
                                    "closed",
                                    "done",
                                    "completed",
                                    "resolved",
                                ):
                                    return True
                            return False
                    return False

                children_open = _children_open(wi_raw)

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
                        return True
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

                owner_repo, pr_num = _extract_pr_from_text(audit_out or "")
                if owner_repo and pr_num:
                    if _verify_pr_with_gh(owner_repo, pr_num):
                        merged_pr = True
                else:
                    if audit_out and re.search(
                        r"pr\s*merged|merged\s+pr|pull request\s+merged",
                        audit_out,
                        re.I,
                    ):
                        merged_pr = True

                ready_token = False
                if audit_out and re.search(
                    r"ready to close|can be closed|ready for final|ready for sign-?off",
                    audit_out,
                    re.I,
                ):
                    ready_token = True

                if merged_pr and (not children_open or ready_token):
                    try:
                        upd_cmd = f"wl update {work_id} --status completed --stage in_review --needs-producer-review true --json"
                        _call(upd_cmd)
                        try:
                            if True:
                                heading_title = f"Audit Completed — {title}"
                                try:
                                    report_for_short = _extract_audit_report(
                                        audit_out or ""
                                    )
                                    short = _extract_summary_from_report(
                                        report_for_short
                                    )
                                    if not short:
                                        short = _extract_summary(audit_out or "")
                                    if not short:
                                        short = audit_out or ""
                                    short = _summarize_for_discord(
                                        short, max_chars=1000
                                    )
                                except Exception:
                                    short = (audit_out or "")[:1000]
                                payload = notifications_module.build_payload(
                                    hostname=os.uname().nodename,
                                    timestamp_iso=_utc_now().isoformat(),
                                    work_item_id=None,
                                    extra_fields=[{"name": "Result", "value": short}],
                                    title=heading_title,
                                )
                                notifications_module.notify(
                                    heading_title,
                                    message_type="completion",
                                    payload=payload,
                                )
                        except Exception:
                            LOG.exception("Failed to send completion notification")
                    except Exception:
                        LOG.exception("Failed to auto-update work item %s", work_id)
            except Exception:
                LOG.exception("Auto-complete check failed for %s", work_id)
        except Exception:
            LOG.exception("Error during triage audit processing")
            return False
        return True
