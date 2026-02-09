"""Candidate selection service for WL work items."""

from __future__ import annotations
import json
import logging
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional

LOG = logging.getLogger("ampa.selection")

# Number of candidates to request from `wl next` by default. Can be overridden
# via the environment variable AMPA_WL_NEXT_COUNT. This ensures the scheduler
# sees multiple candidates (not just the single top candidate) and can try
# fallbacks if the top candidate is unsupported.
WL_NEXT_DEFAULT_COUNT = int(os.getenv("AMPA_WL_NEXT_COUNT", "3"))


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
        # Request multiple candidates so the delegation code can iterate past
        # the top candidate if it's not actionable. Use the configurable
        # AMPA_WL_NEXT_COUNT env var to control how many to request.
        count = WL_NEXT_DEFAULT_COUNT
        cmd = f"wl next -n {count} --json"

        def _run(cmd_str: str) -> Optional[subprocess.CompletedProcess]:
            try:
                LOG.debug("Running wl next command: %s", cmd_str)
                proc = self.run_shell(
                    cmd_str,
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    cwd=self.command_cwd,
                    timeout=self.timeout_seconds,
                )
                return proc
            except Exception:
                LOG.exception("Failed running wl next")
                return None

        proc = _run(cmd)

        # Compatibility fallback: some WL installations do not accept '-n'. If
        # the initial invocation failed, try the simpler form `wl next --json`.
        if proc is None or getattr(proc, "returncode", 1) != 0:
            if proc is not None:
                LOG.debug(
                    "wl next (with -n) failed rc=%s stderr=%r",
                    getattr(proc, "returncode", None),
                    (getattr(proc, "stderr", None) or "")[:512],
                )
            # try without -n
            cmd2 = "wl next --json"
            proc2 = _run(cmd2)
            if proc2 is None or getattr(proc2, "returncode", 1) != 0:
                if proc2 is not None:
                    LOG.warning(
                        "wl next fallback failed rc=%s stderr=%r",
                        getattr(proc2, "returncode", None),
                        (getattr(proc2, "stderr", None) or "")[:512],
                    )
                return None
            proc = proc2

        stdout = getattr(proc, "stdout", None) or ""
        if not stdout.strip():
            LOG.warning("wl next returned empty output")
            return None
        try:
            payload = json.loads(stdout)
        except Exception:
            LOG.warning("wl next returned invalid JSON payload=%r", stdout[:1024])
            # If parsing failed for the -n invocation, try the no- -n form once
            # more as a last resort (handles implementations that emit slightly
            # different JSON shapes).
            if cmd.endswith("-n %d --json" % count):
                proc2 = _run("wl next --json")
                if (
                    proc2
                    and getattr(proc2, "returncode", 1) == 0
                    and getattr(proc2, "stdout", "").strip()
                ):
                    try:
                        payload = json.loads(proc2.stdout)
                    except Exception:
                        LOG.warning(
                            "wl next fallback returned invalid JSON payload=%r",
                            (proc2.stdout or "")[:1024],
                        )
                        return None
                    return payload if isinstance(payload, dict) else {"items": payload}
            return None
        return payload if isinstance(payload, dict) else {"items": payload}


def _normalize_candidates(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        # Normalize a plain list of work-item-like dicts. Also accept lists where
        # each element wraps a work item under keys like 'workItem'.
        out: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            # unwrap common wrapper key
            inner = None
            for k in ("workItem", "work_item", "item"):
                v = item.get(k)
                if isinstance(v, dict):
                    inner = v
                    break
            out.append(inner or item)
        return out
    if not isinstance(payload, dict):
        return []
    # Some WL implementations return a top-level 'results' list where each
    # entry may wrap the actual work item under 'workItem'. Handle that first.
    if isinstance(payload.get("results"), list):
        out: List[Dict[str, Any]] = []
        for entry in payload.get("results", []):
            if not isinstance(entry, dict):
                continue
            # prefer an explicit wrapped workItem when present
            inner = None
            for k in ("workItem", "work_item", "item"):
                v = entry.get(k)
                if isinstance(v, dict):
                    inner = v
                    break
            out.append(inner or entry)
        return out

    for key in ("candidates", "workItems", "work_items", "items", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            # unwrap elements that are wrapper objects
            out: List[Dict[str, Any]] = []
            for item in val:
                if not isinstance(item, dict):
                    continue
                inner = None
                for k in ("workItem", "work_item", "item"):
                    v = item.get(k)
                    if isinstance(v, dict):
                        inner = v
                        break
                out.append(inner or item)
            return out

    # Single work-item at top-level
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
