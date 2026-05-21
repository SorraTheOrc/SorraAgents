from __future__ import annotations

import shlex
from pathlib import Path

QUIET_PYTEST_FLAGS = ("-q", "-r", "a", "--disable-warnings")
_STRIP_FLAGS = {
    "-q",
    "-qq",
    "-v",
    "-vv",
    "-vvv",
    "--quiet",
    "--verbose",
    "--disable-warnings",
    "--showlocals",
}


def _strip_pytest_flags(args: list[str]) -> list[str]:
    """Remove verbosity flags that conflict with the quiet agent contract."""
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        current = args[i]
        if current in _STRIP_FLAGS:
            i += 1
            continue
        if current == "-r" and i + 1 < len(args):
            i += 2
            continue
        cleaned.append(current)
        i += 1
    return cleaned


def canonicalize_quiet_pytest_command(command: str, *, show_locals: bool = False) -> str:
    """Return a pytest command normalized to the agent's quiet contract.

    The canonical agent invocation is:
        pytest -q -r a --disable-warnings

    If ``show_locals`` is true, ``--showlocals`` is added as an additional
    debugging aid.

    Non-pytest commands are returned unchanged.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        # Preserve the original command if shell parsing fails.
        return command

    if not parts:
        return command

    prefix_len = None
    first_token = Path(parts[0]).name
    if first_token == "pytest":
        prefix_len = 1
    elif len(parts) >= 3 and Path(parts[0]).name.startswith("python") and parts[1] == "-m" and Path(parts[2]).name == "pytest":
        prefix_len = 3
    else:
        return command

    prefix = parts[:prefix_len]
    remainder = _strip_pytest_flags(parts[prefix_len:])
    quiet_flags = list(QUIET_PYTEST_FLAGS)
    if show_locals:
        quiet_flags.append("--showlocals")

    return shlex.join(prefix + quiet_flags + remainder)
