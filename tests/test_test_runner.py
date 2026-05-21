from __future__ import annotations

import shlex

from skill.test_runner import canonicalize_quiet_pytest_command, canonicalize_quiet_test_command


def test_canonicalize_quiet_pytest_command_adds_required_flags() -> None:
    command = canonicalize_quiet_pytest_command("pytest tests/test_example.py")
    assert shlex.split(command) == [
        "pytest",
        "-q",
        "-r",
        "a",
        "--disable-warnings",
        "tests/test_example.py",
    ]


def test_canonicalize_quiet_pytest_command_supports_showlocals() -> None:
    command = canonicalize_quiet_pytest_command(
        'python -m pytest -vv -k "test_example and not slow"',
        show_locals=True,
    )
    assert shlex.split(command) == [
        "python",
        "-m",
        "pytest",
        "-q",
        "-r",
        "a",
        "--disable-warnings",
        "--showlocals",
        "-k",
        "test_example and not slow",
    ]


def test_canonicalize_quiet_test_command_adds_npm_silent() -> None:
    assert canonicalize_quiet_test_command("npm test") == "npm --silent test"


def test_canonicalize_quiet_test_command_leaves_non_test_commands() -> None:
    assert canonicalize_quiet_test_command("npm run build") == "npm run build"
