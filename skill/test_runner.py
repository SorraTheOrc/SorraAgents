from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

QUIET_PYTEST_FLAGS = ("-q", "-r", "a", "--disable-warnings")
QUIET_NPM_FLAGS = ("--silent",)

# Resolved pytest command — set by resolve_pytest_command() and then reused.
# Kept as a module global so the PATH probe runs once per process.
_PYTEST_COMMAND: str | None = None


def resolve_pytest_command() -> str:
    """Return the pytest command actually usable in this environment.

    ``pytest`` may be installed in a user site-packages bin dir
    (``~/.local/bin``) that is not on PATH in non-login shells (e.g. agent
    tool shells, cron/nohup contexts), so a bare ``pytest`` invocation
    fails with "command not found" (SA-0MSQ012QG005N22S). Resolution order:

    1. ``pytest`` on PATH (via ``shutil.which``) → ``pytest``
    2. ``~/.local/bin/pytest`` executable when the plain command is not on
       PATH but the user-installed copy exists
    3. ``python3 -m pytest`` — the most robust fallback since the Python
       interpreter is almost always on PATH even when user bin dirs are not

    The result is cached in ``_PYTEST_COMMAND`` so the PATH probes run once.

    Returns:
        A runnable command string (``pytest``, an absolute path, or
        ``python3 -m pytest``).
    """
    global _PYTEST_COMMAND
    if _PYTEST_COMMAND is not None:
        return _PYTEST_COMMAND

    # 1. pytest on PATH
    if shutil.which("pytest") is not None:
        _PYTEST_COMMAND = "pytest"
        return _PYTEST_COMMAND

    # 2. ~/.local/bin/pytest (pip install --user)
    home = os.environ.get("HOME", "")
    if home:
        local_pytest = Path(home).expanduser() / ".local" / "bin" / "pytest"
        if local_pytest.is_file() and os.access(str(local_pytest), os.X_OK):
            _PYTEST_COMMAND = str(local_pytest)
            return _PYTEST_COMMAND

    # 3. python3 -m pytest
    if shutil.which("python3") is not None:
        _PYTEST_COMMAND = "python3 -m pytest"
        return _PYTEST_COMMAND

    # All fallbacks exhausted — the caller will surface a FileNotFoundError.
    _PYTEST_COMMAND = "pytest"
    return _PYTEST_COMMAND


def executable_test_command(command: str) -> str:
    """Return *command* with the bare ``pytest`` executable resolved.

    Canonical pytest command forms (e.g. ``pytest -q -r a``) stay canonical
    for cache keying and cross-consumer reuse, but when the plain ``pytest``
    token is not on PATH the spawned subprocess would fail. This helper
    rewrites only the executable prefix at run time, e.g.:

        pytest -q -r a  →  /home/user/.local/bin/pytest -q -r a
        pytest -q -r a  →  python3 -m pytest -q -r a

    Explicit ``python -m pytest`` forms are left untouched (the interpreter
    is resolvable). Non-pytest commands are returned unchanged.

    Args:
        command: Shell command string (may be a canonical quiet form).

    Returns:
        The command string with the pytest prefix resolved for execution.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if not parts or _is_pytest_command(parts) != 1:
        return command

    resolved = resolve_pytest_command()
    if resolved in (parts[0], "pytest"):
        return command

    prefix = shlex.split(resolved)
    return shlex.join(prefix + parts[1:])

# Known output-filtering commands that may appear in a shell pipeline after a
# test command (e.g. `npm test 2>&1 | tail -30`). These never change which
# tests run — they only reshape the output stream — so they are stripped when
# normalizing a command for cache keying. Anything else is treated as a
# semantically meaningful pipeline stage and left untouched (conservative).
_OUTPUT_FILTER_COMMANDS = {"tail", "head", "grep", "egrep", "fgrep", "tee"}

# Output redirect tokens that merge or discard output streams without changing
# the tests that run. Stripping them lets `npm test 2>&1` share the cache
# entry produced by `npm test`. Redirects to real files (e.g. `> log.txt`)
# are preserved because they change where output lands.
_OUTPUT_REDIRECT_TOKENS = {"2>&1", ">/dev/null", "2>/dev/null", "1>/dev/null"}
_REDIRECT_OPENERS = {">", "2>", "1>"}
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
    if npm_args[0] in {"run", "run-script"} and len(npm_args) >= 2 and npm_args[1].startswith("test"):  # noqa: SIM103
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

    Non-pytest test commands are normalized to a quiet variant when supported
    by the package manager.

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


def _strip_output_redirects(tokens: list[str]) -> list[str]:
    """Remove output-merge/discard redirects from a token list.

    Handles both one-token forms (``2>&1``, ``>/dev/null``) and two-token
    forms (``> /dev/null``, ``2> /dev/null``). Redirects to real files are
    preserved.
    """
    cleaned: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _OUTPUT_REDIRECT_TOKENS:
            i += 1
            continue
        if tok in _REDIRECT_OPENERS and i + 1 < len(tokens) and tokens[i + 1] == "/dev/null":
            i += 2
            continue
        cleaned.append(tok)
        i += 1
    return cleaned


def strip_output_filters(command: str) -> str:
    """Return the underlying test command with output-filtering pipelines stripped.

    Conservative by design: the first pipeline stage (the actual command) is
    kept, and every later stage must be a known output filter
    (``tail``/``head``/``grep``/``egrep``/``fgrep``/``tee``) for the pipeline
    to be stripped. If any later stage is unknown — e.g. ``npm test | sort``
    — the original command is returned unchanged so genuinely different runs
    never share a cache entry.

    Examples::

        npm test 2>&1 | tail -30            ->  npm test
        npm test 2>&1 | grep -E "Test Files" ->  npm test
        pytest -q | head                    ->  pytest -q
        npm test | sort                     ->  npm test | sort   (unchanged)

    Args:
        command: Shell command string.

    Returns:
        The stripped command string (original shell form), or the original
        command unchanged when parsing fails or a stage is not a known filter.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if not parts:
        return command

    if "|" not in parts:
        return shlex.join(_strip_output_redirects(parts))

    stages: list[list[str]] = []
    current: list[str] = []
    for tok in parts:
        if tok == "|":
            stages.append(current)
            current = []
        else:
            current.append(tok)
    stages.append(current)

    base = _strip_output_redirects(stages[0])
    for stage in stages[1:]:
        if not stage:
            return command  # malformed empty stage: don't guess
        first_token = Path(stage[0]).name
        if first_token not in _OUTPUT_FILTER_COMMANDS:
            return command  # not a known output filter: preserve the pipeline
    return shlex.join(base)


def normalize_test_command(command: str, *, show_locals: bool = False) -> str:
    """Return the cache-key form of a test command.

    Strips output-filtering pipelines/redirects first, then applies the quiet
    canonicalization from :func:`canonicalize_quiet_test_command` so that
    ``npm test 2>&1 | grep -E "Test Files|failed"`` and ``npm test`` key to
    the same cache entry.

    Args:
        command: Shell command string.
        show_locals: Whether pytest normalization should retain
            ``--showlocals`` (mirrors canonicalize_quiet_test_command).

    Returns:
        The normalized command string.
    """
    base = strip_output_filters(command)
    return canonicalize_quiet_test_command(base, show_locals=show_locals)
