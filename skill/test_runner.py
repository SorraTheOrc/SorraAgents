from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# Canonical repository-override scrub helper, re-exported here as a single
# import surface for the test-execution paths (test_cache, run_tests,
# audit_runner). The helper itself lives in the stdlib-only
# ``shared.git_sandbox`` module so it stays loadable by file location
# (SA-0MUIA3OE40001QJX).
from shared.git_sandbox import (  # noqa: F401  (re-export)
    REPOSITORY_OVERRIDE_ENV_VARS,
    repository_override_vars_present,
    scrub_repository_overrides,
)

QUIET_PYTEST_FLAGS = ("-q", "-r", "a", "--disable-warnings")
QUIET_NPM_FLAGS = ("--silent",)

# Resolved pytest command — set by resolve_pytest_command() and then reused.
# Kept as a module global so the candidate probes run once per process.
_PYTEST_COMMAND: str | None = None

#: Per-candidate pytest availability probe bound (seconds). The probe is
#: ``<candidate> --version`` — cheap, and memoised with the resolved command so
#: the cost is paid at most once per process (SA-0MVK154IE007OS3D).
PYTEST_PROBE_TIMEOUT_SECONDS = 30.0

# Emit the repository-override scrub diagnostic at most once per process so a
# long suite run does not repeat it for every spawned command.
_SCRUB_DIAGNOSTIC_LOGGED = False


def log_repository_override_scrub(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Emit a one-time diagnostic naming scrubbed repository-override vars.

    Never logs the variable *values* (they may point at sensitive paths).
    Returns the names present in *env* (defaulting to ``os.environ``) so
    callers can use the result without a second scan. Silent when none are
    present and after the first emission.
    """
    global _SCRUB_DIAGNOSTIC_LOGGED
    present = repository_override_vars_present(env)
    if present and not _SCRUB_DIAGNOSTIC_LOGGED:
        _SCRUB_DIAGNOSTIC_LOGGED = True
        print(
            "test-runner: scrubbed repository-override env var(s) before "
            "spawning a test subprocess: " + ", ".join(present),
            file=sys.stderr,
        )
    return present


def _reset_scrub_diagnostic() -> None:
    """Test hook: allow the one-time diagnostic to fire again."""
    global _SCRUB_DIAGNOSTIC_LOGGED
    _SCRUB_DIAGNOSTIC_LOGGED = False


def _candidate_runs_pytest(candidate: list[str]) -> bool:
    """Whether *candidate* can actually run pytest (probed, not assumed).

    The probe runs ``<candidate> --version`` with output suppressed and treats
    any failure to start (``OSError``: missing file, not executable) or any
    non-zero exit (including a timeout) as "cannot run pytest", so a broken
    candidate — e.g. a ``pytest`` script whose shebang points at an
    interpreter without pytest installed — is never selected while a later
    candidate works.

    Args:
        candidate: Command token list, e.g. ``["pytest"]`` or
            ``["/usr/bin/python3", "-m", "pytest"]``.

    Returns:
        True when the candidate exits 0 for ``--version``.
    """
    try:
        proc = subprocess.run(
            [*candidate, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PYTEST_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _pytest_candidates() -> list[list[str]]:
    """Return the ordered pytest candidate commands (SA-0MVK154IE007OS3D AC1).

    Order: ``PYTEST_BIN`` → ``TEST_PYTHON`` → ``sys.executable -m pytest`` →
    ``pytest`` on PATH (the concrete ``~/.local/bin/pytest`` user-install is
    retained as its historical fallback) → ``python3 -m pytest``.

    ``sys.executable`` precedes the PATH ``pytest`` deliberately: the runner
    is started with the project's interpreter, so ``-m pytest`` runs the suite
    with the interpreter that carries the project's dependencies even when a
    PATH ``pytest`` script is bound to a different (e.g. system) interpreter.
    """
    candidates: list[list[str]] = []

    explicit_bin = os.environ.get("PYTEST_BIN", "").strip()
    if explicit_bin:
        candidates.append(shlex.split(explicit_bin))

    explicit_python = os.environ.get("TEST_PYTHON", "").strip()
    if explicit_python:
        candidates.append([*shlex.split(explicit_python), "-m", "pytest"])

    if sys.executable:
        candidates.append([sys.executable, "-m", "pytest"])

    if shutil.which("pytest") is not None:
        candidates.append(["pytest"])

    home = os.environ.get("HOME", "")
    if home:
        local_pytest = Path(home).expanduser() / ".local" / "bin" / "pytest"
        if local_pytest.is_file() and os.access(str(local_pytest), os.X_OK):
            candidates.append([str(local_pytest)])

    if shutil.which("python3") is not None:
        candidates.append(["python3", "-m", "pytest"])

    return candidates


def resolve_pytest_command() -> str:
    """Return the pytest command that actually works in this environment.

    A bare ``pytest`` may be missing (user-site bin dirs such as
    ``~/.local/bin`` are absent from non-login shells — SA-0MSQ012QG005N22S)
    **or** may exist on PATH while being bound to an interpreter that cannot
    import the project's dependencies (SA-0MVK154IE007OS3D). Resolution
    therefore probes every candidate with ``--version`` and selects the first
    one that runs, in this order (AC1):

    1. ``PYTEST_BIN`` — explicit override, used verbatim when it runs.
    2. ``TEST_PYTHON -m pytest`` — explicit interpreter override.
    3. ``sys.executable -m pytest`` — the interpreter that started the runner,
       which is the project's interpreter (matches the repository's canonical
       ``python3 -m pytest`` runner).
    4. ``pytest`` on PATH (via ``shutil.which``) — only when earlier forms
       cannot run pytest, so a PATH ``pytest`` bound to a foreign interpreter
       never shadows the runner's interpreter.
    5. ``~/.local/bin/pytest`` — retained user-install fallback
       (SA-0MSQ012QG005N22S).
    6. ``python3 -m pytest`` — last resort when the runner's interpreter
       cannot run pytest but PATH ``python3`` can.

    Candidates that fail the probe are skipped, so a selected command never
    refers to an executable that cannot run pytest. The resolved command is
    cached in ``_PYTEST_COMMAND`` (probes run once per process) and keeps the
    canonical ``python -m pytest`` / ``pytest`` shape consumed by
    :func:`executable_test_command`.

    Returns:
        A runnable command string (``pytest``, an absolute pytest path, or
        ``<python> -m pytest``); falls back to ``pytest`` when no candidate
        probes successfully, leaving the caller to surface the spawn error.
    """
    global _PYTEST_COMMAND
    if _PYTEST_COMMAND is not None:
        return _PYTEST_COMMAND

    for candidate in _pytest_candidates():
        if not candidate or not _candidate_runs_pytest(candidate):
            continue
        _PYTEST_COMMAND = shlex.join(candidate)
        return _PYTEST_COMMAND

    # Every candidate failed the probe — the caller will surface a
    # FileNotFoundError (or the interpreter's import error) on execution.
    _PYTEST_COMMAND = "pytest"
    return _PYTEST_COMMAND


def is_pytest_command(command: str) -> bool:
    """Whether *command* invokes pytest (``pytest`` or ``<python> -m pytest``)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return _is_pytest_command(parts) is not None


def resolve_pytest_executable() -> str:
    """Return the concrete interpreter/executable that runs pytest.

    This is the auditable label recorded in the run output and in the test
    cache entry (SA-0MVK154IE007OS3D AC2): the absolute ``python``
    interpreter for ``-m pytest`` forms, or the absolute ``pytest`` path when
    a bare ``pytest`` command was selected (falling back to the bare name when
    it cannot be located, e.g. when every probe failed).
    """
    resolved = resolve_pytest_command()
    parts = shlex.split(resolved)
    if not parts:
        return resolved
    executable = parts[0]
    if Path(executable).name == "pytest":
        return shutil.which(executable) or executable
    return executable


def executable_test_command(command: str) -> str:
    """Return *command* with the bare ``pytest`` executable resolved.

    Canonical pytest command forms (e.g. ``pytest -q -r a``) stay canonical
    for cache keying and cross-consumer reuse, but when the plain ``pytest``
    token is not on PATH the spawned subprocess would fail. This helper
    rewrites only the executable prefix at run time, e.g.:

        pytest -q -r a           →  /home/user/.local/bin/pytest -q -r a
        pytest -q -r a           →  /usr/bin/python3 -m pytest -q -r a
        python3 -m pytest -q -r a →  /venv/bin/python3 -m pytest -q -r a

    Both prefix shapes are rewritten to the probed resolver selection, so an
    explicit ``python3 -m pytest`` command also runs under the interpreter
    carrying the project's dependencies (SA-0MVK154IE007OS3D). A command whose
    prefix already matches the resolved selection is returned unchanged, which
    makes the rewrite idempotent. Non-pytest commands are returned unchanged.

    Args:
        command: Shell command string (may be a canonical quiet form).

    Returns:
        The command string with the pytest prefix resolved for execution.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    prefix_len = _is_pytest_command(parts)
    if prefix_len is None:
        return command

    resolved = shlex.split(resolve_pytest_command())
    if resolved[:prefix_len] == parts[:prefix_len]:
        return command

    return shlex.join(resolved + parts[prefix_len:])

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


def subprocess_env_with_scrubbed_overrides(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Scrubbed, PATH-augmented environment for spawning test subprocesses.

    Combines the shared repository-override scrub with the ``~/.local/bin``
    PATH injection the runners rely on (user-installed ``pytest`` is not on
    PATH in restricted agent/cron shells). Unrelated variables are preserved
    and the input mapping is never mutated.

    ``~/.local/bin`` is normalised to appear exactly once, at the front of
    PATH: if absent it is prepended, and if present (possibly repeated by a
    launcher/profile that appends it several times) it is deduplicated to a
    single leading entry. This keeps the spawned PATH canonical regardless of
    ambient duplication.
    """
    env = scrub_repository_overrides(os.environ if base is None else base)
    local_bin = os.path.expanduser("~/.local/bin")
    path_parts = env.get("PATH", "").split(os.pathsep)
    if local_bin in path_parts:
        path_parts = [local_bin] + [p for p in path_parts if p != local_bin]
    else:
        path_parts = [local_bin] + path_parts
    env["PATH"] = os.pathsep.join(path_parts)
    return env


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
