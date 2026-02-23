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
    # relative import to allow tests to monkeypatch ampa.webhook
    from . import webhook as webhook_module
except Exception:  # pragma: no cover - defensive
    import ampa.webhook as webhook_module

LOG = logging.getLogger("ampa.triage_audit")


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


class TriageAuditRunner:
    def __init__(
        self,
        run_shell: "Callable[..., subprocess.CompletedProcess]",
        command_cwd: str,
        store: Any,
        engine: Optional[Any] = None,
    ) -> None:
        self.run_shell = run_shell
        self.command_cwd = command_cwd
        self.store = store
        self.engine = engine

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

        audit_only = _bool_meta(spec.metadata.get("audit_only"))

        webhook = os.getenv("AMPA_DISCORD_WEBHOOK")

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

            proc = _call("wl in_progress --json")
            if proc.returncode != 0:
                LOG.warning("wl in_progress failed: %s", proc.stderr)
            else:
                try:
                    raw = json.loads(proc.stdout or "null")
                except Exception:
                    LOG.exception("Failed to parse wl in_progress output")
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
                if status == "in_progress":
                    return _int_meta(
                        "audit_cooldown_hours_in_progress", default_cooldown_hours
                    )
                if status == "blocked":
                    return _int_meta(
                        "audit_cooldown_hours_blocked", default_cooldown_hours
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

            delegation_result: Optional[Dict[str, Any]] = None

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

            try:
                old_notifier = None
                try:
                    if (
                        hasattr(self, "engine")
                        and getattr(self.engine, "_notifier", None) is not None
                    ):
                        old_notifier = self.engine._notifier
                        from ampa.engine.core import NullNotificationSender

                        self.engine._notifier = NullNotificationSender()
                except Exception:
                    old_notifier = None

                delegation_result = self._run_delegation_from_runner(
                    audit_only=audit_only, spec=spec
                )
            except Exception:
                LOG.exception("Delegation run failed during triage audit")
                delegation_result = None
            finally:
                try:
                    if old_notifier is not None and hasattr(self, "engine"):
                        self.engine._notifier = old_notifier
                except Exception:
                    LOG.exception(
                        "Failed to restore engine notifier after triage delegation"
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

            if webhook:
                summary_text = _extract_summary(audit_out or "")
                if not summary_text:
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
                    include_preview = False
                    try:
                        include_preview = bool(
                            (spec.metadata or {}).get("include_delegation_preview")
                        )
                    except Exception:
                        include_preview = False
                    if include_preview:
                        try:
                            delegation_preview = (
                                self._run_delegation_report_for_preview(spec)
                            )
                        except Exception:
                            delegation_preview = None
                        extra.append(
                            {
                                "name": "Delegation",
                                "value": (delegation_preview or "(none)"),
                            }
                        )
                    else:
                        extra.append({"name": "Delegation", "value": "(skipped)"})
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

            full_output = audit_out or ""
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
                delegation_result = self._run_delegation_from_runner(
                    audit_only=False, spec=spec
                )
                LOG.info("Triage-initiated delegation result: %s", delegation_result)
            except Exception:
                LOG.exception("Failed to run delegation from triage")

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
                        upd_cmd = f"wl update {work_id} --status completed --stage in_review --json"
                        _call(upd_cmd)
                        try:
                            if webhook:
                                heading_title = f"Audit Completed — {title}"
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

    # helper to delegate to existing scheduler delegation API when available
    def _run_delegation_from_runner(
        self, *, audit_only: bool, spec: Any
    ) -> Optional[Dict[str, Any]]:
        # If an engine with the expected helper exists, call similar logic to scheduler._run_idle_delegation
        if audit_only:
            return {
                "note": "Delegation: skipped (audit_only)",
                "dispatched": False,
                "rejected": [],
                "idle_webhook_sent": False,
            }
        try:
            if self.engine is None:
                return None
            # try to call engine-based delegation flow if present
            if hasattr(self.engine, "process_delegation"):
                try:
                    result = self.engine.process_delegation()
                except Exception:
                    LOG.exception("Engine process_delegation raised an exception")
                    return {
                        "note": "Delegation: engine error",
                        "dispatched": False,
                        "rejected": [],
                        "idle_webhook_sent": False,
                    }
                # Mirror the scheduler conversion minimally
                status = getattr(result, "status", None)
                from ampa.engine.core import EngineStatus

                if status == EngineStatus.SUCCESS:
                    action = result.action or "unknown"
                    wid = result.work_item_id or "?"
                    delegate_title = ""
                    if result.candidate_result and result.candidate_result.selected:
                        delegate_title = result.candidate_result.selected.title
                    delegate_info = {
                        "action": action,
                        "id": wid,
                        "title": delegate_title,
                    }
                    if result.dispatch_result:
                        delegate_info["pid"] = result.dispatch_result.pid
                    return {
                        "note": f"Delegation: dispatched {action} {wid}",
                        "dispatched": True,
                        "delegate_info": delegate_info,
                        "rejected": [],
                        "idle_webhook_sent": False,
                    }
                if status == EngineStatus.NO_CANDIDATES:
                    return {
                        "note": "Delegation: skipped (no wl next candidates)",
                        "dispatched": False,
                        "rejected": [],
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
                        "rejected": [],
                        "idle_webhook_sent": False,
                    }
                if status == EngineStatus.DISPATCH_FAILED:
                    return {
                        "note": f"Delegation: failed ({result.reason})",
                        "dispatched": False,
                        "rejected": [],
                        "idle_webhook_sent": False,
                        "error": result.reason,
                    }
                return {
                    "note": f"Delegation: engine error ({result.reason})",
                    "dispatched": False,
                    "rejected": [],
                    "idle_webhook_sent": False,
                    "error": result.reason,
                }
            return None
        except Exception:
            LOG.exception("_run_delegation_from_runner failed")
            return None

    def _run_delegation_report_for_preview(self, spec: Any) -> Optional[str]:
        # Lightweight replication of scheduler._run_delegation_report used for preview embedding
        try:
            proc = self.run_shell(
                "wl in_progress",
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
            )
            in_progress_text = ""
            if getattr(proc, "stdout", None):
                in_progress_text += proc.stdout
            if getattr(proc, "stderr", None) and not in_progress_text:
                in_progress_text += proc.stderr
            # import selection lazily to avoid cycles
            try:
                from . import selection as _selection
            except Exception:
                import ampa.selection as _selection
            candidates, _payload = _selection.fetch_candidates(
                run_shell=self.run_shell, command_cwd=self.command_cwd
            )
            top_candidate = candidates[0] if candidates else None
            report = "".join(
                ["AMPA Delegation\n"]
            )  # minimal fallback if selection unavailable
            # Try to build a simple delegation report similar to scheduler
            try:
                from .scheduler import _build_delegation_report

                report = _build_delegation_report(
                    in_progress_output=_trim_text(in_progress_text),
                    candidates=candidates,
                    top_candidate=top_candidate,
                )
            except Exception:
                try:
                    # fallback minimal formatting
                    lines = ["AMPA Delegation", "In-progress items:", "- (none)"]
                    report = "\n".join(lines)
                except Exception:
                    report = "(no report)"
            return report
        except Exception:
            LOG.exception("Failed to build delegation preview")
            return None
