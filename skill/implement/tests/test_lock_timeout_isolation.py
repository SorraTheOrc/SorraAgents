"""Lock-timeout environment hermeticity (SA-0MVY0G5EH000LIMB).

When this suite is itself run by ``implement.py finish``, an outer finish may
export ``IMPLEMENT_FINISH_LOCK_TIMEOUT`` / ``IMPLEMENT_START_LOCK_TIMEOUT`` to
wait out a concurrent holder. A lock-refusal test that holds the lock itself
then inherits that value and sleeps the whole timeout before refusing, turning
a sub-second test into a multi-hour hang.

``conftest._no_ambient_lock_timeout`` neutralises both variables for every test
in this package. These tests assert that contract.
"""

from __future__ import annotations

import os

import pytest

from skill.implement.tests import conftest

#: The two lock-wait env vars read through ``_resolve_lock_timeout()`` in
#: ``implement.py``. These are the complete neutralisation set; no other test in
#: this package reaches either variable (audited 2026-11-08, see the conftest
#: module docstring).
LOCK_TIMEOUT_ENV_VARS = (
    "IMPLEMENT_FINISH_LOCK_TIMEOUT",
    "IMPLEMENT_START_LOCK_TIMEOUT",
)


def test_lock_timeout_vars_are_absent_while_the_fixture_is_active():
    """The autouse fixture leaves neither lock-timeout var in the environment.

    This is the contract that makes the refusal regression instantaneous even
    when the ambient environment exports both variables for a real finish.
    """
    for var in LOCK_TIMEOUT_ENV_VARS:
        value = os.environ.get(var)
        assert value in (None, "0"), (
            f"{var}={value!r} leaked past the autouse neutralisation fixture"
        )


def test_fixture_deletes_both_ambient_lock_timeouts():
    """The fixture body removes both variables from a seeded environment.

    Exercises the fixture directly (rather than relying on whichever ambient
    values happen to be present) so the neutralisation is proven, not assumed.
    """
    seeded = pytest.MonkeyPatch()
    try:
        seeded.setenv("IMPLEMENT_FINISH_LOCK_TIMEOUT", "10800")
        seeded.setenv("IMPLEMENT_START_LOCK_TIMEOUT", "600")
        assert os.environ["IMPLEMENT_FINISH_LOCK_TIMEOUT"] == "10800"
        assert os.environ["IMPLEMENT_START_LOCK_TIMEOUT"] == "600"

        conftest._no_ambient_lock_timeout.__wrapped__(seeded)

        for var in LOCK_TIMEOUT_ENV_VARS:
            assert var not in os.environ, f"{var} was not neutralised"
    finally:
        seeded.undo()
