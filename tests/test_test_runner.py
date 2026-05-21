from __future__ import annotations

import shlex

from skill.test_runner import canonicalize_quiet_pytest_command


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


def test_canonicalize_quiet_pytest_command_leaves_non_pytest_commands() -> None:
    assert canonicalize_quiet_pytest_command("npm test") == "npm test"
