"""Shared webhook and payload utilities for AMPA.

This module contains low-level helpers for building Discord payloads, sending
webhook POSTs with retries/backoff, and dead-letter handling. It is intended to
be the single source of truth for notification behaviour so scheduler and
daemon can import from here.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    requests = None

LOG = logging.getLogger("ampa.webhook")


def _truncate_output(output: str, limit: int = 900) -> str:
    if len(output) <= limit:
        return output
    return output[:limit] + "\n... (truncated)"


def build_payload(
    hostname: str,
    timestamp_iso: str,
    work_item_id: Optional[str] = None,
    extra_fields: Optional[List[Dict[str, Any]]] = None,
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    # Build a simple markdown message where the first line is a heading and
    # the body contains only human-facing informational fields. Do not include
    # technical metadata like hostnames, timestamps or internal ids by
    # default; callers should pass any user-facing fields via `extra_fields`.
    heading = f"# {title}"
    body: List[str] = []
    if extra_fields:
        for field in extra_fields:
            name = field.get("name")
            value = field.get("value")
            if name and value is not None:
                body.append(f"{name}: {value}")
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


def build_command_payload(
    hostname: str,
    timestamp_iso: str,
    command_id: Optional[str],
    output: Optional[str],
    exit_code: Optional[int],
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    # Command-oriented payloads should still present a clear heading and
    # a short human-readable summary. Avoid including technical identifiers
    # (command names, exit codes) in the message body. Use `title` to
    # describe the message topic; `output` is treated as the human-facing
    # summary text and will be truncated if necessary.
    heading = f"# {title}" if title else "# AMPA Notification"
    body: List[str] = []
    if output:
        body.append(_truncate_output(output, limit=1000))
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


def _read_state(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_state(path: str, data: Dict[str, str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        LOG.exception("Failed to write state file %s", path)


def dead_letter(payload: Dict[str, Any], reason: Optional[str] = None) -> None:
    try:
        # Log at ERROR level immediately when dead_letter is invoked so that
        # final-failure attempts are visible in logs and include reason and
        # the original payload (truncated to avoid extremely large entries).
        try:
            payload_str = json.dumps(payload)
        except Exception:
            payload_str = str(payload)
        LOG.error(
            "dead_letter invoked: reason=%s payload=%s",
            reason,
            _truncate_output(payload_str, limit=1000),
        )
        dd_wh = os.getenv("AMPA_DEADLETTER_WEBHOOK")
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
        }
        if dd_wh:
            if requests is None:
                LOG.error(
                    "dead_letter: requests missing; cannot POST to deadletter webhook"
                )
            else:
                try:
                    sess = requests.Session()
                    sess.trust_env = False
                    resp = sess.post(dd_wh, json=record, timeout=10)
                    try:
                        resp.raise_for_status()
                        LOG.info(
                            "dead_letter: posted to deadletter webhook status=%s",
                            resp.status_code,
                        )
                        return
                    except Exception:
                        LOG.error(
                            "dead_letter: deadletter webhook POST failed: %s %s",
                            getattr(resp, "status_code", None),
                            getattr(resp, "text", ""),
                        )
                except Exception as exc:
                    LOG.exception(
                        "dead_letter: exception posting to deadletter webhook: %s", exc
                    )
        dl_file = os.getenv("AMPA_DEADLETTER_FILE", "/var/log/ampa_deadletter.log")
        try:
            parent = os.path.dirname(dl_file)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception:
                    pass
            with open(dl_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            LOG.info("dead_letter: appended failure record to %s", dl_file)
        except Exception:
            LOG.exception("dead_letter: failed to write dead-letter file %s", dl_file)
    except Exception:
        LOG.exception("dead_letter: unexpected error while handling dead letter")


def send_webhook(
    url: str, payload: Dict[str, Any], timeout: int = 10, message_type: str = "other"
) -> int:
    import time

    if requests is None:
        LOG.error("requests package is required to send webhook")
        raise RuntimeError("requests missing")

    state_file = os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )

    def _mask_url(u: str) -> str:
        try:
            parts = u.split("/")
            if len(parts) > 0:
                parts[-1] = "<token>"
                return "/".join(parts)
        except Exception:
            pass
        return "<redacted>"

    try:
        max_retries = int(os.getenv("AMPA_MAX_RETRIES", "10"))
        if max_retries < 1:
            raise ValueError()
    except Exception:
        LOG.warning("Invalid AMPA_MAX_RETRIES, falling back to 10")
        max_retries = 10

    try:
        backoff_base = float(os.getenv("AMPA_BACKOFF_BASE_SECONDS", "2"))
        if backoff_base <= 0:
            raise ValueError()
    except Exception:
        LOG.warning("Invalid AMPA_BACKOFF_BASE_SECONDS, falling back to 2")
        backoff_base = 2.0

    session = requests.Session()
    session.trust_env = False
    LOG.debug(
        "Sending webhook to %s (masked=%s); trust_env=%s",
        url,
        _mask_url(url),
        session.trust_env,
    )

    last_exc: Optional[BaseException] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(url, json=payload, timeout=timeout)
            try:
                resp.raise_for_status()
            except Exception:
                LOG.warning(
                    "Webhook POST attempt %d/%d failed: %s %s",
                    attempt,
                    max_retries,
                    getattr(resp, "status_code", None),
                    getattr(resp, "text", ""),
                )
                try:
                    req = getattr(resp, "request", None)
                    if req is not None:
                        masked = _mask_url(getattr(req, "url", None) or "")
                        headers = {
                            k: v
                            for k, v in getattr(req, "headers", {}).items()
                            if k.lower() not in ("authorization",)
                        }
                        body_len = (
                            len(getattr(req, "body", b""))
                            if getattr(req, "body", None)
                            else 0
                        )
                        LOG.info(
                            "Request made to %s; headers=%s; body_len=%s",
                            masked,
                            headers,
                            body_len,
                        )
                        parsed = urllib.parse.urlparse(getattr(req, "url", ""))
                        host = parsed.hostname
                        if host:
                            try:
                                addrs = [
                                    a[4][0]
                                    for a in __import__("socket").getaddrinfo(
                                        host, parsed.port or 443
                                    )
                                ]
                                LOG.info("Resolved %s -> %s", host, addrs)
                            except Exception as _e:
                                LOG.info("Failed to resolve host %s: %s", host, _e)
                except Exception:
                    LOG.info("Failed to log request debug info")

                try:
                    _write_state(
                        state_file,
                        {
                            "last_message_ts": datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat(),
                            "last_message_type": message_type,
                        },
                    )
                except Exception:
                    LOG.debug("Failed to record state after exception")

                if attempt >= max_retries:
                    LOG.error(
                        "Webhook POST final failure after %d attempts: %s",
                        attempt,
                        getattr(resp, "status_code", None),
                    )
                    try:
                        dead_letter(
                            payload, reason=f"HTTP {getattr(resp, 'status_code', None)}"
                        )
                    except Exception:
                        LOG.exception("dead_letter failed on final HTTP error")
                    return getattr(resp, "status_code", 0)

                backoff = backoff_base * (2 ** (attempt - 1))
                LOG.info("Backing off for %.1fs before next attempt", backoff)
                time.sleep(backoff)
                continue

            LOG.info("Webhook POST succeeded: %s", resp.status_code)
            _write_state(
                state_file,
                {
                    "last_message_ts": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "last_message_type": message_type,
                },
            )
            return resp.status_code

        except Exception as exc:
            last_exc = exc
            LOG.warning(
                "Webhook POST attempt %d/%d exception: %s",
                attempt,
                max_retries,
                exc,
            )
            try:
                _write_state(
                    state_file,
                    {
                        "last_message_ts": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        "last_message_type": message_type,
                    },
                )
            except Exception:
                LOG.debug("Failed to record state after exception")

            if attempt >= max_retries:
                LOG.error(
                    "Webhook POST final exception after %d attempts: %s",
                    attempt,
                    exc,
                )
                try:
                    dead_letter(payload, reason=str(exc))
                except Exception:
                    LOG.exception("dead_letter failed on final exception")
                raise

            backoff = backoff_base * (2 ** (attempt - 1))
            LOG.info("Backing off for %.1fs before next attempt", backoff)
            time.sleep(backoff)

    return 0
