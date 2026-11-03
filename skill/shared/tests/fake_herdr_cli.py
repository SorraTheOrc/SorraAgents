#!/usr/bin/env python3
"""Recording fake ``herdr`` CLI for tests (test-only, never installed).

Two flavours of the same double, so a test can choose how much realism it
needs:

``FakeHerdrRunner``
    An in-process replacement for ``subprocess.run``. It records every
    invocation (argv, in order) and returns the next *scripted* response as a
    ``CompletedProcess``. Use it with
    ``HerdrSessionClient(runner=FakeHerdrRunner(...))`` for fast, deterministic
    tests of the helper's parsing and fail-open behaviour.

``fake_herdr_cli.py`` run as a script
    An executable shim that speaks the same protocol over a real subprocess
    boundary. Use it with ``HerdrSessionClient(herdr_bin=<shim>)`` when the
    test must assert the *exact* herdr invocations (arguments and order) that a
    caller constructed, including the environment it passed. The shim takes its
    script and recording paths from ``FAKE_HERDR_SCRIPT`` / ``FAKE_HERDR_RECORD``
    (or the ``--script`` / ``--record`` flags placed before the herdr argv):
    every call appends one JSON line ``{"argv": [...]}`` to the record file and
    prints the next scripted response.

Script format (both flavours) is a JSON object::

    {
      "responses": [
        {"stdout": {"result": {"type": "pane_info", "pane": {"pane_id": "w1:p9"}}}},
        {"stdout": {"error": {"code": "agent_not_ready", "message": "..."}},
         "exit_code": 1},
        {"raises": "timeout"}
      ],
      "default": {"stdout": {"result": {"type": "ok"}}}
    }

Entry fields: ``stdout`` (a JSON-serialisable object or a raw string),
``stderr`` (string), ``exit_code`` (int, default 0), ``raises`` (one of
``"timeout"`` / ``"oserror"``, in-process runner only), ``delay`` (seconds,
shim only).

Responses are consumed **in order**, so a script itself pins the call order;
the recorded argv list proves the arguments the caller built. The response
builders at the top of this module emit the real CLI's envelopes (verified
against ``herdr`` 0.9.5) so tests never hand-roll a shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable

#: Environment variable holding the shim's script file path.
SCRIPT_ENV = "FAKE_HERDR_SCRIPT"

#: Environment variable holding the shim's recording (JSONL) file path.
RECORD_ENV = "FAKE_HERDR_RECORD"

#: Exit code used when no response is scripted for a call.
UNSCRIPTED_EXIT_CODE = 127


# ---------------------------------------------------------------------------
# Response builders (real herdr envelopes, herdr 0.9.5)
# ---------------------------------------------------------------------------


def _envelope(command: str, result: dict) -> dict:
    return {"id": f"cli:{command}", "result": result}


def pane_info_response(pane_id: str, *, label: str | None = None,
                       command: str = "pane:split", **fields) -> dict:
    """A ``pane_info`` success envelope (``pane split`` / ``pane rename``)."""
    pane = {"pane_id": pane_id, "label": label, "tab_id": "w1:t1",
            "workspace_id": "w1", "agent": None, "agent_status": "unknown"}
    pane.update(fields)
    return _envelope(command, {"type": "pane_info", "pane": pane})


def agent_info_response(name: str, pane_id: str, *, status: str = "idle",
                        command: str = "agent:wait", **fields) -> dict:
    """An ``agent_info`` success envelope (``agent wait``)."""
    agent = {"name": name, "pane_id": pane_id, "agent": "pi",
             "agent_status": status, "tab_id": "w1:t1", "workspace_id": "w1"}
    agent.update(fields)
    return _envelope(command, {"type": "agent_info", "agent": agent})


def agent_started_response(name: str, pane_id: str, *, status: str = "idle",
                           argv: Iterable[str] = ()) -> dict:
    """An ``agent_started`` success envelope (``agent start``)."""
    agent = {"name": name, "pane_id": pane_id, "agent": "pi",
             "agent_status": status, "tab_id": "w1:t1", "workspace_id": "w1"}
    return _envelope("agent:start",
                     {"type": "agent_started", "agent": agent,
                      "argv": list(argv)})


def agent_prompted_response(name: str, pane_id: str, *, status: str = "done",
                            **fields) -> dict:
    """An ``agent_prompted`` success envelope (``agent prompt --wait``)."""
    return agent_info_response(name, pane_id, status=status,
                               command="agent:prompt", **fields)


def ok_response() -> dict:
    """An ``ok`` success envelope (``pane close``)."""
    return _envelope("pane:close", {"type": "ok"})


def error_response(code: str, message: str = "") -> dict:
    """A herdr error envelope (``{"error": {"code", "message"}}``)."""
    return {"id": f"cli:error:{code}",
            "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Script normalisation / selection
# ---------------------------------------------------------------------------


def _normalise_entry(entry: dict) -> dict:
    normalised = {
        "stdout": entry.get("stdout", {"result": {"type": "ok"}}),
        "stderr": entry.get("stderr", ""),
        "exit_code": int(entry.get("exit_code", 0)),
        "raises": entry.get("raises"),
        "delay": float(entry.get("delay", 0.0) or 0.0),
    }
    return normalised


def normalise_script(script) -> tuple[list[dict], dict]:
    """Return ``(responses, default)`` from a script dict/list."""
    if script is None:
        return [], {}
    if isinstance(script, list):
        return [_normalise_entry(entry) for entry in script], {}
    if isinstance(script, dict):
        responses = script.get("responses") or []
        default = script.get("default")
        return (
            [_normalise_entry(entry) for entry in responses],
            _normalise_entry(default) if default else {},
        )
    raise TypeError(f"unsupported fake herdr script: {type(script)!r}")


def _entry_stdout_text(entry: dict) -> str:
    stdout = entry.get("stdout", "")
    if isinstance(stdout, str):
        return stdout
    return json.dumps(stdout)


def select_response(responses: list[dict], default: dict, index: int) -> dict | None:
    """Return the response for call *index* (in order), else *default*."""
    if 0 <= index < len(responses):
        return responses[index]
    return default or None


# ---------------------------------------------------------------------------
# In-process runner
# ---------------------------------------------------------------------------


class FakeHerdrRunner:
    """A ``subprocess.run`` stand-in with ordered scripted responses.

    Records every invocation in ``calls`` (argv lists, in order) and the
    keyword arguments in ``kwargs``. No response scripted for a call yields a
    non-zero exit with an explanatory stderr, so an unexpected call fails a
    test loudly rather than silently succeeding.
    """

    def __init__(self, script=None, *, default=None) -> None:
        responses, scripted_default = normalise_script(script)
        self.responses = responses
        self.default = scripted_default if default is None else _normalise_entry(default)
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append([str(part) for part in cmd])
        self.kwargs.append(dict(kwargs))
        index = len(self.calls) - 1
        entry = select_response(self.responses, self.default, index)
        if entry is None:
            return subprocess.CompletedProcess(
                cmd, UNSCRIPTED_EXIT_CODE, "",
                f"fake herdr: no scripted response for call {index}: {cmd}",
            )
        if entry["raises"] == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 1)
        if entry["raises"] == "oserror":
            raise OSError("fake herdr: simulated launch failure")
        return subprocess.CompletedProcess(
            cmd, entry["exit_code"], _entry_stdout_text(entry), entry["stderr"],
        )

    # -- assertions helpers -------------------------------------------------

    def arguments(self) -> list[list[str]]:
        """The recorded herdr argv lists (the binary is argv[0])."""
        return [call[1:] for call in self.calls]

    def raw_arguments(self) -> list[list[str]]:
        """The recorded calls exactly as received (binary included)."""
        return list(self.calls)


# ---------------------------------------------------------------------------
# Subprocess shim
# ---------------------------------------------------------------------------


def shim_path() -> str:
    """Absolute path of this module (the shim is its own executable)."""
    return os.path.abspath(__file__)


def ensure_executable(path: str | None = None) -> str:
    """Make sure the shim has its executable bit set; return its path."""
    target = path or shim_path()
    mode = os.stat(target).st_mode
    if not mode & 0o111:
        os.chmod(target, mode | 0o755)
    return target


def read_record(path: str | None) -> list[list[str]]:
    """Return the recorded argv lists from a shim record file (missing → [])."""
    if not path or not os.path.exists(path):
        return []
    calls: list[list[str]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            argv = entry.get("argv") if isinstance(entry, dict) else None
            if isinstance(argv, list):
                calls.append([str(part) for part in argv])
    return calls


def write_script(path: str, script) -> str:
    """Write *script* (dict or list) to *path* as JSON; return *path*."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(script, handle)
    return path


def _split_shim_args(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Split leading ``--script``/``--record`` flags from the herdr argv."""
    script = None
    record = None
    remaining = list(argv)
    while remaining:
        head = remaining[0]
        if head == "--script" and len(remaining) > 1:
            script, remaining = remaining[1], remaining[2:]
        elif head == "--record" and len(remaining) > 1:
            record, remaining = remaining[1], remaining[2:]
        else:
            break
    return script, record, remaining


def main(argv: list[str] | None = None) -> int:
    """Entry point of the shim: record the call, print the scripted response."""
    args = list(sys.argv[1:] if argv is None else argv)
    script_flag, record_flag, herdr_argv = _split_shim_args(args)
    script_file = script_flag or os.environ.get(SCRIPT_ENV)
    record_file = record_flag or os.environ.get(RECORD_ENV)

    previous = read_record(record_file)
    if record_file:
        with open(record_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": herdr_argv}) + "\n")

    responses: list[dict] = []
    default: dict = {}
    if script_file and os.path.exists(script_file):
        with open(script_file, encoding="utf-8") as handle:
            responses, default = normalise_script(json.load(handle))

    entry = select_response(responses, default, len(previous))
    if entry is None:
        print(f"fake herdr: no scripted response for call {len(previous)}: "
              f"{herdr_argv}", file=sys.stderr)
        return UNSCRIPTED_EXIT_CODE
    if entry["delay"]:
        import time as _time

        _time.sleep(entry["delay"])
    if entry["stderr"]:
        print(entry["stderr"], file=sys.stderr)
    print(_entry_stdout_text(entry))
    return entry["exit_code"]


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.exit(main())
