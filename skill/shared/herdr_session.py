"""Session-level Herdr boundary: panes, agents, and their lifecycle.

``herdr_pane`` (SA-0MTFLQEQQ0083EW1) covers the *pane-title* surface only.
Driving an epic child inside herdr needs one level more: split a sibling pane,
start an interactive agent in it, prompt it, wait for its lifecycle state, and
close or rename the pane when the child is done (SA-0MVPHXNTJ006TNFB).

This module is that single injectable client. Every call returns a parsed
result dict or a fail-open ``{"success": False, "reason": ...}`` — it **never
raises**, mirroring :mod:`shared.herdr_pane`. Callers decide what a failure
means for their workflow; the epic chain must stop cleanly, never hang.

CLI surface (verified against ``herdr`` 0.9.5)::

    herdr pane split --current --direction right --cwd <path> --env K=V --no-focus
    herdr agent start <name> --kind pi --pane <id> -- --approve --no-extensions
    herdr agent prompt <target> <text> --wait [--until <state>] [--timeout <ms>]
    herdr agent wait <target> [--until <state>] [--timeout <ms>]
    herdr pane close <pane_id>
    herdr pane rename <pane_id> <label>

Result shapes follow the herdr socket API: ``pane split`` returns
``.result.pane.pane_id``, agent commands return ``.result.agent`` (with
``agent_status``), and failures carry ``{"error": {"code", "message"}}`` in the
payload (usually with a non-zero exit code). Shapes are parsed defensively: an
unexpected payload is reported as a failure, never assumed (Herdr API drift is
a recorded risk, so unknown shapes must not crash a drive run).

Agent names are bounded by herdr's ``[a-z][a-z0-9_-]{0,31}`` rule (32
characters, unique among live agents). :func:`agent_name_for_child` derives
``drive-<child-id-lowercased>`` deterministically, truncates it to the rule and
resolves collisions against names already in use.

The matching test double lives in ``skill/shared/tests/fake_herdr_cli.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping

from . import herdr_pane

#: Prefix for child-session agent names (``drive-<child-id-lowercased>``).
AGENT_NAME_PREFIX = "drive-"

#: herdr's agent-name limit: ``[a-z][a-z0-9_-]{0,31}`` is 32 characters.
AGENT_NAME_MAX_LENGTH = 32

#: herdr's agent-name rule.
_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: Characters outside herdr's allowed set are collapsed to ``-``.
_NAME_UNSAFE_RE = re.compile(r"[^a-z0-9_-]+")

#: herdr lifecycle states accepted by ``--until``.
AGENT_STATUSES: tuple[str, ...] = ("idle", "working", "blocked", "done", "unknown")

#: Failure code herdr returns when a pane is not at an interactive prompt.
AGENT_NOT_READY = "agent_not_ready"

#: Default per-CLI-call subprocess timeout (seconds).
DEFAULT_TIMEOUT = 60.0

#: Default poll interval (seconds) when retrying ``agent_not_ready``.
DEFAULT_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# Agent naming
# ---------------------------------------------------------------------------


def _normalise_name_fragment(value) -> str:
    """Lower-case *value* and collapse anything herdr rejects into ``-``."""
    text = _NAME_UNSAFE_RE.sub("-", str(value or "").strip().lower())
    return text.strip("-_")


def is_valid_agent_name(name) -> bool:
    """Whether *name* satisfies herdr's ``[a-z][a-z0-9_-]{0,31}`` rule."""
    return bool(_AGENT_NAME_RE.match(str(name or "")))


def agent_name_for_child(
    child_id,
    *,
    existing: Iterable[str] = (),
    prefix: str = AGENT_NAME_PREFIX,
) -> str:
    """Return the herdr agent name for *child_id*.

    ``drive-<child-id-lowercased>``, sanitised to herdr's character set and
    deterministically truncated to ``AGENT_NAME_MAX_LENGTH``. When the result
    is already in use (``existing`` enumerates live names), a ``-2``, ``-3``, …
    suffix is appended inside the same length budget so the name stays valid
    and unique. The function never raises and is stable for the same inputs.
    """
    fragment = _normalise_name_fragment(child_id)
    base = f"{prefix}{fragment}" if fragment else prefix.rstrip("-_")
    base = base[:AGENT_NAME_MAX_LENGTH].rstrip("-_") or prefix.rstrip("-_")

    taken = {str(name) for name in existing}
    if base not in taken:
        return base
    for index in range(2, 1000):
        suffix = f"-{index}"
        candidate = base[: AGENT_NAME_MAX_LENGTH - len(suffix)].rstrip("-_") + suffix
        if candidate not in taken:
            return candidate
    # Pathological exhaustion (a thousand live long names): return the base so
    # the caller's herdr call fails loudly rather than this helper raising.
    return base


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def _parse_json(text) -> dict | None:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [item for item in value if item is not None]


def _result(
    *,
    command: list[str],
    success: bool,
    reason: str = "",
    code: str | None = None,
    returncode: int | None = None,
    payload: dict | None = None,
    **extra,
) -> dict:
    result = {
        "success": success,
        "reason": reason,
        "code": code,
        "command": list(command),
        "returncode": returncode,
    }
    if payload is not None:
        result["payload"] = payload
    result.update(extra)
    return result


def _unexpected(outcome: dict, expected: str) -> dict:
    """Convert a successful-but-unparsable outcome into a reported failure."""
    return {
        **outcome,
        "success": False,
        "reason": f"unexpected herdr response (expected {expected})",
    }


def _mount(result: dict, key: str = "result") -> Mapping:
    """Return the ``result`` object of a response, or an empty mapping."""
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return {}
    body = payload.get(key)
    return body if isinstance(body, dict) else {}


class HerdrSessionClient:
    """Fail-open client for the session-level herdr CLI surface.

    Every method returns a dict with at least ``success``, ``reason``, ``code``,
    ``command`` and ``returncode``; successful calls add the parsed identifiers
    (``pane_id``, ``agent``, ``agent_status``, …). No method raises.

    Injection points (all optional): ``runner`` (a ``subprocess.run``
    replacement), ``which`` (binary resolution), ``herdr_bin`` (explicit
    binary), ``env`` (subprocess environment; ``None`` inherits), ``timeout``
    (per-call seconds), ``clock``/``sleep`` (polling clocks for the
    ``agent_not_ready`` retry).
    """

    def __init__(
        self,
        *,
        herdr_bin: str | None = None,
        env: Mapping | None = None,
        runner: Callable | None = None,
        which: Callable | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._herdr_bin = herdr_bin
        self._env = env
        self._runner = runner or subprocess.run
        self._which = which
        self._timeout = timeout
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    # -- internals ---------------------------------------------------------

    def env(self) -> dict:
        """Return the environment mapping used for subprocess calls."""
        return dict(os.environ) if self._env is None else dict(self._env)

    def resolve_bin(self) -> str | None:
        """Resolve the herdr binary (explicit, ``HERDR_BIN_PATH``, then PATH)."""
        if self._herdr_bin:
            return str(self._herdr_bin)
        return herdr_pane.resolve_herdr_bin(self.env(), self._which)

    def _invoke(self, args: list[str]) -> dict:
        """Run ``herdr <args>`` once and classify the outcome (never raises)."""
        binary = self.resolve_bin()
        if not binary:
            return _result(
                command=[], success=False, reason="herdr CLI unavailable",
            )
        command = [binary, *args]
        try:
            proc = self._runner(
                command,
                capture_output=True,
                text=True,
                env=herdr_pane.subprocess_env(self._env),
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return _result(
                command=command, success=False, reason="herdr call timed out",
            )
        except Exception as exc:  # noqa: BLE001 - fail-open by design
            return _result(
                command=command, success=False,
                reason=f"herdr call failed: {exc}",
            )

        returncode = getattr(proc, "returncode", None)
        payload = _parse_json(getattr(proc, "stdout", "") or "")

        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "") or None
            message = str(error.get("message") or "").strip()
            reason = code or "herdr error"
            if message:
                reason = f"{reason}: {message}"
            return _result(
                command=command, success=False, reason=reason, code=code,
                returncode=returncode, payload=payload, message=message,
            )
        if returncode not in (0, None):
            return _result(
                command=command, success=False,
                reason=f"herdr exited with code {returncode}",
                returncode=returncode, payload=payload,
            )
        if payload is None:
            return _result(
                command=command, success=False, reason="malformed herdr output",
                returncode=returncode,
            )
        if not isinstance(payload.get("result"), dict):
            return _result(
                command=command, success=False,
                reason="unexpected herdr response (missing result)",
                returncode=returncode, payload=payload,
            )
        return _result(
            command=command, success=True, returncode=returncode, payload=payload,
        )

    def _agent_fields(self, outcome: dict) -> dict:
        """Add the parsed agent identity/state, or fail on an unknown shape."""
        agent = _mount(outcome).get("agent")
        if not isinstance(agent, dict):
            return _unexpected(outcome, "result.agent")
        status = agent.get("agent_status")
        return {
            **outcome,
            "agent": agent,
            "agent_name": agent.get("name"),
            "agent_status": status if isinstance(status, str) else None,
            "pane_id": agent.get("pane_id"),
        }

    # -- pane operations ---------------------------------------------------

    def split_pane(
        self,
        cwd,
        *,
        direction: str | None = "right",
        env: Mapping | None = None,
        focus: bool = False,
        current: bool = True,
        pane_id: str | None = None,
    ) -> dict:
        """Split a sibling pane and return its ``pane_id``.

        Splits the caller's pane by default (``--current``); never creates a
        tab or workspace. ``env`` entries become repeated ``--env K=V`` flags
        for the launched process.
        """
        args = ["pane", "split"]
        if pane_id:
            args += ["--pane", str(pane_id)]
        if current:
            args.append("--current")
        if direction:
            args += ["--direction", str(direction)]
        if cwd:
            args += ["--cwd", str(cwd)]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        args.append("--focus" if focus else "--no-focus")

        outcome = self._invoke(args)
        if not outcome["success"]:
            return outcome
        pane = _mount(outcome).get("pane")
        pane_id_value = pane.get("pane_id") if isinstance(pane, dict) else None
        if not pane_id_value:
            return _unexpected(outcome, "result.pane.pane_id")
        return {**outcome, "pane_id": str(pane_id_value), "pane": pane}

    def close_pane(self, pane_id: str) -> dict:
        """Close *pane_id*."""
        outcome = self._invoke(["pane", "close", str(pane_id)])
        if not outcome["success"]:
            return outcome
        return {**outcome, "pane_id": str(pane_id)}

    def rename_pane(self, pane_id: str, label: str) -> dict:
        """Set the label of *pane_id* (surfaces a retained pane's verdict)."""
        outcome = self._invoke(["pane", "rename", str(pane_id), str(label)])
        if not outcome["success"]:
            return outcome
        return {**outcome, "pane_id": str(pane_id), "label": str(label)}

    # -- agent operations --------------------------------------------------

    def start_agent(
        self,
        name: str,
        pane_id: str,
        *,
        kind: str = "pi",
        args: Iterable[str] | None = None,
        timeout_ms: int | None = None,
        ready_budget_seconds: float = 0.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> dict:
        """Start an interactive agent in *pane_id*.

        herdr requires the pane to be at its interactive shell prompt; if it is
        not, the CLI reports ``agent_not_ready``. With a positive
        ``ready_budget_seconds`` the start is retried (polling every
        ``poll_interval``) within that budget; otherwise the first
        ``agent_not_ready`` is surfaced as a reported reason. Never raises.
        """
        if not is_valid_agent_name(name):
            return _result(
                command=[], success=False,
                reason=f"invalid herdr agent name: {name!r}",
            )
        command = ["agent", "start", str(name), "--kind", str(kind),
                   "--pane", str(pane_id)]
        if timeout_ms is not None:
            command += ["--timeout", str(int(timeout_ms))]
        extra = [str(arg) for arg in (args or ())]
        if extra:
            command += ["--", *extra]

        budget = max(0.0, float(ready_budget_seconds))
        deadline = self._clock() + budget
        attempts = 0
        while True:
            attempts += 1
            outcome = self._invoke(command)
            if outcome["success"]:
                return {**self._agent_fields(outcome), "attempts": attempts}
            if outcome.get("code") != AGENT_NOT_READY:
                return {**outcome, "attempts": attempts}
            remaining = deadline - self._clock()
            if budget <= 0 or remaining <= 0:
                return {**outcome, "attempts": attempts}
            self._sleep(min(max(poll_interval, 0.0), remaining))

    def prompt_agent(
        self,
        target: str,
        text: str,
        *,
        wait: bool = True,
        until: str | Iterable[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Submit *text* to an agent, optionally waiting for a settled state.

        ``until`` implies ``--wait`` (the herdr CLI rejects ``--until`` alone).
        """
        states = [str(state) for state in _as_list(until)]
        command = ["agent", "prompt", str(target), str(text)]
        if wait or states:
            command.append("--wait")
        for state in states:
            command += ["--until", state]
        if timeout_ms is not None:
            command += ["--timeout", str(int(timeout_ms))]

        outcome = self._invoke(command)
        if not outcome["success"]:
            return outcome
        return self._agent_fields(outcome)

    def wait_agent(
        self,
        target: str,
        *,
        until: str | Iterable[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict:
        """Wait until *target* reaches one of ``until`` (herdr's defaults else)."""
        command = ["agent", "wait", str(target)]
        for state in _as_list(until):
            command += ["--until", str(state)]
        if timeout_ms is not None:
            command += ["--timeout", str(int(timeout_ms))]

        outcome = self._invoke(command)
        if not outcome["success"]:
            return outcome
        return self._agent_fields(outcome)


__all__ = [
    "AGENT_NAME_MAX_LENGTH",
    "AGENT_NAME_PREFIX",
    "AGENT_NOT_READY",
    "AGENT_STATUSES",
    "HerdrSessionClient",
    "agent_name_for_child",
    "is_valid_agent_name",
]
