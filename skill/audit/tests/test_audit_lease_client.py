"""Proxy audit-lease client (LP-0MW96A9GW009KVF7, parent LP-0MW0KZUJP0019SLS AC1).

The audit skill acquires a cross-host audit-tier lease from the proxy
(``POST /llama/local/audit/lease``) for the lifetime of a run, renews it
periodically, and releases it at exit — so at most one audit holds a lease
across ALL client hosts (the single local GPU). Covers:

- AC1 (parent): ``audit_lease_begin`` acquires a lease for an ``audit-``
  session id and records it module-level for the run.
- Fail-safe (parent constraint): a proxy error, timeout, or a saturated
  ``audit-budget-saturated`` refusal after bounded retries NEVER blocks the
  audit — the run proceeds under legacy per-host guards.
- Disabled via ``AUDIT_LEASE_ENABLED=0``.
- The same session id is reused for acquire/renew/release (one identity per
  run); release is idempotent and crash-safe (proxy TTL backstop).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from audit.scripts import audit_runner


@pytest.fixture(autouse=True)
def _clean_lease_state():
    audit_runner._AUDIT_LEASE_STATE = None
    yield
    audit_runner._AUDIT_LEASE_STATE = None


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Never touch the real proxy in unit tests."""

    def _fail(*args, **kwargs):
        raise AssertionError("urllib must be mocked in unit tests")

    monkeypatch.setattr(audit_runner.urllib.request, "urlopen", _fail)


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


def test_lease_enabled_by_default(monkeypatch):
    monkeypatch.delenv(audit_runner.AUDIT_LEASE_ENABLED_ENV, raising=False)
    assert audit_runner._audit_lease_enabled() is True


def test_lease_disabled_via_env(monkeypatch):
    monkeypatch.setenv(audit_runner.AUDIT_LEASE_ENABLED_ENV, "0")
    assert audit_runner._audit_lease_enabled() is False


def test_begin_with_disabled_env_acquires_nothing(monkeypatch, capsys):
    monkeypatch.setenv(audit_runner.AUDIT_LEASE_ENABLED_ENV, "0")
    assert audit_runner.audit_lease_begin("SA-X") is None
    assert audit_runner._AUDIT_LEASE_STATE is None


# ---------------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------------


def test_begin_acquires_lease_and_records_module_state():
    responses = iter([
        {"admit": True, "lease_id": "lease-1", "live": 1, "available": 0},
    ])

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps(next(responses)))

    with mock.patch.object(audit_runner.urllib.request, "urlopen", _fake_urlopen):
        session_id = audit_runner.audit_lease_begin("SA-RUN")

    assert session_id is not None
    assert session_id.startswith("audit-SA-RUN-lease-")
    assert audit_runner._AUDIT_LEASE_STATE == {
        "session_id": session_id, "lease_id": "lease-1",
    }


def test_begin_is_idempotent_for_nested_runs():
    responses = iter([
        {"admit": True, "lease_id": "lease-1", "live": 1, "available": 0},
    ])

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps(next(responses)))

    with mock.patch.object(audit_runner.urllib.request, "urlopen", _fake_urlopen):
        outer = audit_runner.audit_lease_begin("SA-RUN")
        # Nested call (child audit, batch drain) reuses the held lease.
        inner = audit_runner.audit_lease_begin("SA-RUN")
        assert outer == inner
        # Only ONE acquire HTTP call happened.
        assert audit_runner._AUDIT_LEASE_STATE["session_id"] == outer


def test_proxy_error_fails_open_and_proceeds():
    """A proxy error/timeout never blocks the audit: begin returns None."""

    def _boom(req, timeout=None):
        raise OSError("connection refused")

    with mock.patch.object(audit_runner.urllib.request, "urlopen", _boom):
        session_id = audit_runner.audit_lease_begin("SA-X")

    assert session_id is None
    assert audit_runner._AUDIT_LEASE_STATE is None


def test_saturated_waits_bounded_retries_then_fails_open(capsys, monkeypatch):
    """AC1 wait semantics: saturated refuses are retried boundedly, then the
    audit proceeds without a lease (never hangs)."""
    monkeypatch.setenv(audit_runner.AUDIT_LEASE_ACQUIRE_RETRIES_ENV, "2")
    saturated = {
        "admit": False, "reason": "audit-budget-saturated",
        "retry_after_s": 0, "live": 1, "available": 0,
    }
    # retries=2 -> attempts 0,1,2; the first two are saturated waits, the
    # third exhausts the bound and fails open.
    responses = iter([saturated, saturated, saturated])
    sleeps = []

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(json.dumps(next(responses)))

    monkeypatch.setattr(audit_runner.time, "sleep", lambda s: sleeps.append(s))
    with mock.patch.object(audit_runner.urllib.request, "urlopen", _fake_urlopen):
        session_id = audit_runner.audit_lease_begin("SA-WAIT")

    assert session_id is None
    assert len(sleeps) == 2  # one bounded wait between the two attempts
    err = capsys.readouterr().err
    assert "audit-budget-saturated" in err


def test_lease_session_id_is_audit_prefixed_and_stable():
    sid1 = audit_runner._audit_run_session_id("SA-123")
    sid2 = audit_runner._audit_run_session_id("SA-123")
    assert sid1.startswith("audit-SA-123-lease-")
    # Two calls build different uuids, but a single run reuses the recorded one.
    assert sid1 != sid2
    assert len(sid1.split("-")[-1]) == 8


# ---------------------------------------------------------------------------
# Renew / release
# ---------------------------------------------------------------------------


def test_renew_current_posts_renew_endpoint():
    audit_runner._AUDIT_LEASE_STATE = {
        "session_id": "audit-S-X-lease-abc12345", "lease_id": "lease-1",
    }
    seen = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeResponse(json.dumps({"renewed": True}))

    with mock.patch.object(audit_runner.urllib.request, "urlopen", _fake_urlopen):
        audit_runner.audit_lease_renew_current()

    assert seen["url"].endswith("/llama/local/audit/lease/renew")
    assert seen["method"] == "POST"


def test_renew_current_noop_without_lease(capsys):
    audit_runner._AUDIT_LEASE_STATE = None
    audit_runner.audit_lease_renew_current()  # must not raise, no HTTP
    assert "Audit lease" not in capsys.readouterr().err


def test_end_releases_the_lease_and_clears_state():
    audit_runner._AUDIT_LEASE_STATE = {
        "session_id": "audit-S-X-lease-abc12345", "lease_id": "lease-1",
    }
    seen = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeResponse(json.dumps({"released": True, "live": 0}))

    with mock.patch.object(audit_runner.urllib.request, "urlopen", _fake_urlopen):
        audit_runner.audit_lease_end()

    assert seen["url"].endswith(
        "/llama/local/audit/lease/audit-S-X-lease-abc12345"
    )
    assert seen["method"] == "DELETE"
    assert audit_runner._AUDIT_LEASE_STATE is None


def test_end_is_idempotent_and_crash_safe():
    audit_runner._AUDIT_LEASE_STATE = None
    audit_runner.audit_lease_end()  # no-op


def test_lease_url_quoting_of_session_id():
    assert audit_runner._url_quote("audit-a-b-1") == "audit-a-b-1"
    assert audit_runner._url_quote("audit a/b") == "audit%20a%2Fb"


class _FakeResponse:
    """Minimal urllib response stand-in."""

    def __init__(self, body: str, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
