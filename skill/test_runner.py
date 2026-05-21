from __future__ import annotations

import shlex
from pathlib import Path

QUIET_PYTEST_FLAGS = ("-q", "-r", "a", "--disable-warnings")
QUIET_NPM_FLAGS = ("--silent",)
_STRIP_PYTEST_FLAGS = {
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
_STRIP_NPM_FLAGS = {"--silent", "-s"}


def _strip_pytest_flags(args: list[str]) -> list[str]:
    """Remove verbosity flags that conflict with the quiet agent contract."""
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        current = args[i]
        if current in _STRIP_PYTEST_FLAGS:
            i += 1
            continue
        if current == "-r" and i + 1 < len(args):
            i += 2
            continue
        cleaned.append(current)
        i += 1
    return cleaned


def _is_pytest_command(parts: list[str]) -> int | None:
    if not parts:
        return None
    first_token = Path(parts[0]).name
    if first_token == "pytest":
        return 1
    if len(parts) >= 3 and Path(parts[0]).name.startswith("python") and parts[1] == "-m" and Path(parts[2]).name == "pytest":
        return 3
    return None


def _canonicalize_pytest_command(parts: list[str], *, show_locals: bool = False) -> str:
    prefix_len = _is_pytest_command(parts)
    if prefix_len is None:
        raise ValueError("not a pytest command")

    prefix = parts[:prefix_len]
    remainder = _strip_pytest_flags(parts[prefix_len:])
    quiet_flags = list(QUIET_PYTEST_FLAGS)
    if show_locals:
        quiet_flags.append("--showlocals")

    return shlex.join(prefix + quiet_flags + remainder)


def _is_npm_test_command(parts: list[str]) -> bool:
    if not parts:
        return False
    if Path(parts[0]).name != "npm":
        return False

    npm_args = [arg for arg in parts[1:] if arg not in _STRIP_NPM_FLAGS]
    if not npm_args:
        return False

    if npm_args[0] == "test":
        return True
    if npm_args[0] in {"run", "run-script"} and len(npm_args) >= 2 and npm_args[1].startswith("test"):
        return True
    return False


def _canonicalize_npm_test_command(parts: list[str]) -> str:
    npm_args = [arg for arg in parts[1:] if arg not in _STRIP_NPM_FLAGS]
    return shlex.join([parts[0], *QUIET_NPM_FLAGS, *npm_args])


def canonicalize_quiet_test_command(command: str, *, show_locals: bool = False) -> str:
    """Return a test command normalized to the agent's quiet contract.

    Pytest commands are normalized to:
        pytest -q -r a --disable-warnings

    If ``show_locals`` is true, ``--showlocals`` is added as an additional
    debugging aid.

    Non-pytest test commands such as ``npm test`` are normalized to a quiet
    variant when supported by the package manager.

    Non-test commands are returned unchanged.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        # Preserve the original command if shell parsing fails.
        return command

    if not parts:
        return command

    if _is_pytest_command(parts) is not None:
        return _canonicalize_pytest_command(parts, show_locals=show_locals)

    if _is_npm_test_command(parts):
        return _canonicalize_npm_test_command(parts)

    return command


def canonicalize_quiet_pytest_command(command: str, *, show_locals: bool = False) -> str:
    """Backward-compatible wrapper for pytest-only callers."""
    return canonicalize_quiet_test_command(command, show_locals=show_locals)
