"""Candidate selection service for WL work items."""

from __future__ import annotations
import json
import logging
import subprocess
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("ampa.selection")

WL_NEXT_COMMAND = "wl next --json"


class WLNextClient:
    def __init__(
        self,
        run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
        command_cwd: Optional[str] = None,
        timeout_seconds: int = 10,
    ) -> None:
        self.run_shell = run_shell or subprocess.run
        self.command_cwd = command_cwd
        self.timeout_seconds = timeout_seconds

    def fetch_payload(self) -> Optional[Dict[str, Any]]:
        cmd = WL_NEXT_COMMAND
        try:
            proc = self.run_shell(
                cmd,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                cwd=self.command_cwd,
                timeout=self.timeout_seconds,
            )
        except Exception:
            LOG.exception("Failed running wl next")
            return None
        if getattr(proc, "returncode", 1) != 0:
            LOG.warning("wl next failed rc=%s", getattr(proc, "returncode", None))
            return None
        stdout = getattr(proc, "stdout", None) or ""
        if not stdout.strip():
            return None
        try:
            payload = json.loads(stdout)
        except Exception:
            LOG.warning("wl next returned invalid JSON")
            return None
        return payload if isinstance(payload, dict) else {"items": payload}


def _normalize_candidates(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("candidates", "workItems", "work_items", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]

    for key in ("workItem", "work_item", "item"):
        val = payload.get(key)
        if isinstance(val, dict):
            return [val]

    return []


def normalize_candidates(payload: Any) -> List[Dict[str, Any]]:
    return _normalize_candidates(payload)


def select_candidate(
    *,
    run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    command_cwd: Optional[str] = None,
    timeout_seconds: int = 10,
) -> Optional[Dict[str, Any]]:
    client = WLNextClient(
        run_shell=run_shell,
        command_cwd=command_cwd,
        timeout_seconds=timeout_seconds,
    )
    payload = client.fetch_payload()
    candidates = _normalize_candidates(payload)
    if not candidates:
        return None

    return candidates[0]


def fetch_candidates(
    *,
    run_shell: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    command_cwd: Optional[str] = None,
    timeout_seconds: int = 10,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    client = WLNextClient(
        run_shell=run_shell,
        command_cwd=command_cwd,
        timeout_seconds=timeout_seconds,
    )
    payload = client.fetch_payload()
    return _normalize_candidates(payload), payload
