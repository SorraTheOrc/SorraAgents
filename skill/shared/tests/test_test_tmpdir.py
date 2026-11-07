"""Unit tests for ``skill/shared/test_tmpdir.py`` (SA-0MW3EN72O005BMIP).

The helper is the single source of truth for two framework-side mitigations of
the ``/tmp`` inode-exhaustion defect (SA-0MW2QED9M005FWOK):

* ``resolve_test_tmpdir`` resolves a **disk-backed** temporary directory in a
  documented precedence order and falls back portably, and
* ``prune_stale_test_roots`` removes stale ``pytest-of-<user>/pytest-*`` roots
  while never touching another user's roots, the ``pytest-current`` target,
  the newest roots or a root a live pytest process may hold.

Both are fail-open: every forced error must surface as a reason string, never
as an exception, because a prune/TMPDIR problem may never change a test run's
outcome. These tests drive the real production functions; only the platform
(tmpfs detection), the clock and the ``/proc`` scan are injected.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared import test_tmpdir

_ENV_VARS = (
    test_tmpdir.TEST_TMPDIR_ENV,
    test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV,
    test_tmpdir.TEST_ROOT_MAX_AGE_ENV,
    test_tmpdir.TEST_ROOT_KEEP_ENV,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every test from an operator override in the ambient env."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _make_root(base: Path, name: str, *, age_seconds: float) -> Path:
    root = base / name
    root.mkdir(parents=True)
    (root / "tmpfile").write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(root, (stamp, stamp))
    return root


def _current_user_base(parent: Path) -> Path:
    base = parent / test_tmpdir.PYTEST_BASE_TEMPLATE.format(
        user=test_tmpdir._current_user()
    )
    base.mkdir(parents=True)
    return base


# ---------------------------------------------------------------------------
# resolve_test_tmpdir — precedence and fallback
# ---------------------------------------------------------------------------


def test_explicit_override_wins_and_is_created(tmp_path, monkeypatch):
    target = tmp_path / "override" / "nested"
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_ENV, str(target))
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(tmp_path / "disk"))

    path, reason = test_tmpdir.resolve_test_tmpdir()

    assert path == target
    assert target.is_dir()
    assert test_tmpdir.TEST_TMPDIR_ENV in reason


def test_disk_backed_candidate_is_used_and_created(tmp_path, monkeypatch):
    disk_root = tmp_path / "disk-root"
    disk_root.mkdir()
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(disk_root))
    # The fixture lives under /tmp, which IS a tmpfs on the affected host; the
    # mount check is a platform concern and is exercised separately below.
    monkeypatch.setattr(test_tmpdir, "_is_tmpfs", lambda _path: False)

    path, reason = test_tmpdir.resolve_test_tmpdir()

    expected = disk_root / test_tmpdir.TEST_TMPDIR_DIRNAME_TEMPLATE.format(
        uid=test_tmpdir._current_uid()
    )
    assert path == expected
    assert expected.is_dir()
    assert "disk-backed" in reason


def test_tmpfs_disk_root_is_skipped_in_favour_of_gettempdir(tmp_path, monkeypatch):
    disk_root = tmp_path / "disk-root"
    disk_root.mkdir()
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(disk_root))
    monkeypatch.setattr(test_tmpdir, "_is_tmpfs", lambda _path: True)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fallback))

    path, reason = test_tmpdir.resolve_test_tmpdir()

    assert path == fallback
    assert "tmpfs" in reason
    assert not os.listdir(disk_root)


def test_missing_disk_root_falls_back_to_gettempdir(tmp_path, monkeypatch):
    monkeypatch.setenv(
        test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(tmp_path / "absent" / "root")
    )
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fallback))

    path, reason = test_tmpdir.resolve_test_tmpdir()

    assert path == fallback
    assert "absent" in reason
    assert "fallback" in reason


def test_unusable_override_falls_through_to_disk_candidate(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_ENV, str(blocker / "nested"))
    disk_root = tmp_path / "disk-root"
    disk_root.mkdir()
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(disk_root))
    monkeypatch.setattr(test_tmpdir, "_is_tmpfs", lambda _path: False)

    path, reason = test_tmpdir.resolve_test_tmpdir()

    assert path.parent == disk_root
    assert "override" in reason
    assert "disk-backed" in reason


def test_resolution_never_raises_when_everything_fails(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_ENV, str(blocker / "nested"))
    monkeypatch.setenv(
        test_tmpdir.TEST_TMPDIR_DISK_ROOT_ENV, str(blocker / "missing-root")
    )

    def _boom():
        raise RuntimeError("forced gettempdir failure")

    monkeypatch.setattr(tempfile, "gettempdir", _boom)

    path, reason = test_tmpdir.resolve_test_tmpdir()  # must not raise

    assert isinstance(path, Path)
    assert str(path)
    assert "resolution error" in reason


# ---------------------------------------------------------------------------
# prune_stale_test_roots — safety rules
# ---------------------------------------------------------------------------


def test_prune_removes_stale_keeps_current_target_and_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(test_tmpdir, "_active_pytest_paths", list)
    base = _current_user_base(tmp_path)
    current = _make_root(base, "pytest-0", age_seconds=7200)
    stale_a = _make_root(base, "pytest-1", age_seconds=7200)
    stale_b = _make_root(base, "pytest-2", age_seconds=7200)
    fresh = _make_root(base, "pytest-3", age_seconds=0)
    unrelated = base / "not-a-pytest-root"
    unrelated.mkdir()
    (base / test_tmpdir.PYTEST_CURRENT_NAME).symlink_to(current.name)

    removed, reason = test_tmpdir.prune_stale_test_roots(
        base, max_age_seconds=3600, keep_recent=1, now=time.time()
    )

    assert removed == 2
    assert current.is_dir(), "the pytest-current target must never be removed"
    assert fresh.is_dir(), "the newest root must be kept"
    assert not stale_a.exists()
    assert not stale_b.exists()
    assert (base / test_tmpdir.PYTEST_CURRENT_NAME).is_symlink()
    assert unrelated.is_dir()
    assert "removed=2" in reason


def test_prune_keeps_newest_roots_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(test_tmpdir, "_active_pytest_paths", list)
    base = _current_user_base(tmp_path)
    now = time.time()
    roots = [
        _make_root(base, f"pytest-{index}", age_seconds=7200 + (10 - index))
        for index in range(4)
    ]

    # No keep_recent argument: the documented default of 2 applies.
    removed, reason = test_tmpdir.prune_stale_test_roots(
        base, max_age_seconds=3600, now=now
    )

    assert removed == 2
    assert not roots[0].exists()
    assert not roots[1].exists()
    assert roots[2].is_dir()
    assert roots[3].is_dir()
    assert "kept_recent=2" in reason


def test_prune_absent_base_is_a_noop(tmp_path):
    absent = tmp_path / test_tmpdir.PYTEST_BASE_TEMPLATE.format(
        user=test_tmpdir._current_user()
    )

    removed, reason = test_tmpdir.prune_stale_test_roots(absent, now=time.time())

    assert removed == 0
    assert "absent" in reason
    assert not absent.exists()


def test_prune_never_touches_another_users_root(tmp_path):
    foreign = tmp_path / "pytest-of-somebody-else"
    foreign.mkdir()
    stale = _make_root(foreign, "pytest-1", age_seconds=7200)

    removed, reason = test_tmpdir.prune_stale_test_roots(
        foreign, max_age_seconds=3600, keep_recent=0, now=time.time()
    )

    assert removed == 0
    assert stale.is_dir(), "another user's roots are sacrosanct"
    assert "not pytest-of-" in reason


def test_prune_skips_root_held_by_an_active_pytest_process(tmp_path, monkeypatch):
    base = _current_user_base(tmp_path)
    in_use = _make_root(base, "pytest-1", age_seconds=7200)
    release = _make_root(base, "pytest-2", age_seconds=7200)
    monkeypatch.setattr(
        test_tmpdir,
        "_active_pytest_paths",
        lambda: [str(in_use / "test_output" / "log.txt")],
    )

    removed, reason = test_tmpdir.prune_stale_test_roots(
        base, max_age_seconds=3600, keep_recent=0, now=time.time()
    )

    assert removed == 1
    assert in_use.is_dir(), "a root a live pytest may hold must be skipped"
    assert not release.exists()
    assert "skipped_in_use=1" in reason


def test_prune_failure_is_reported_and_never_raised(tmp_path, monkeypatch):
    base = _current_user_base(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced prune failure")

    monkeypatch.setattr(test_tmpdir, "_prune_base", _boom)

    removed, reason = test_tmpdir.prune_stale_test_roots(base, now=time.time())

    assert removed == 0
    assert "prune error" in reason


class TestDocumentedContractPinsTheHelpers:
    """Docs-vs-source pin (SA-0MW3ENVLU0097JRD): the operator guidance in
    ``docs/dev/test-skill-reference.md`` §3 names exactly the env vars,
    defaults and failure signature the helpers honour (same style as
    ``skill/implement/tests/test_gate_classification_docs.py``).

    The behavioural rules themselves are covered above; this class pins the
    *documented contract* so a renamed constant, a changed default or a
    signature drift fails here without the operator guidance being updated.
    """

    _DOC = REPO_ROOT / "docs" / "dev" / "test-skill-reference.md"

    def test_documented_env_var_names_match_production(self) -> None:
        doc = self._DOC.read_text(encoding="utf-8")
        for name in _ENV_VARS:
            assert f"`{name}`" in doc, (
                f"env var {name!r} is not documented in "
                "docs/dev/test-skill-reference.md — the TMPDIR/precedence "
                "guidance must name the exact variable the helper reads"
            )

    def test_documented_defaults_match_production(self) -> None:
        doc = self._DOC.read_text(encoding="utf-8")
        for token in (
            str(int(test_tmpdir.TEST_ROOT_MAX_AGE_DEFAULT)),  # 3600 seconds
            str(test_tmpdir.TEST_ROOT_KEEP_DEFAULT),  # 2 newest roots
            test_tmpdir.TEST_TMPDIR_DISK_ROOT_DEFAULT,  # /var/tmp
        ):
            assert token in doc, (
                f"default value {token!r} is not documented in "
                "docs/dev/test-skill-reference.md — a changed default must "
                "update the prune/TMPDIR guidance"
            )

    def test_failure_signature_tokens_are_documented(self) -> None:
        """The incident's recognisable output and diagnosis are documented so
        a future ENOSPC-in-Playwright failure is identified on sight."""
        doc = self._DOC.read_text(encoding="utf-8")
        for token in (
            "PlaywrightBrowserProvider.createContext",
            "IUse% 100",
            "unparsed",
        ):
            assert token in doc, (
                f"failure-signature token {token!r} is not documented in "
                "docs/dev/test-skill-reference.md"
            )

    def test_resolver_honours_the_documented_precedence(
        self, tmp_path, monkeypatch
    ) -> None:
        """Smoke pin of the documented precedence order: the explicit override
        wins over the disk-backed candidate and the tempfile fallback.
        (Behaviour is covered exhaustively above; this pins the contract as
        documented.)
        """
        override = tmp_path / "override"
        monkeypatch.setattr(
            test_tmpdir, "_is_tmpfs", lambda _path: False
        )
        monkeypatch.setenv(test_tmpdir.TEST_TMPDIR_ENV, str(override))

        path, reason = test_tmpdir.resolve_test_tmpdir()

        assert path == override
        assert "override" in reason
