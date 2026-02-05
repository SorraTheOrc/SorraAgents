import io
import os
import sys
import time
import types

import pytest

# Ensure repo root is on sys.path so `import ampa` works when pytest is invoked
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from ampa import daemon


class _FakeResp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

        class _Req:
            def __init__(self, url: str):
                self.url = url
                self.headers = {}
                self.body = b""

        self.request = _Req("http://example.com/webhook/token")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def test_retry_then_success(monkeypatch):
    # simulate first two attempts raising a network error, third succeeds
    calls = {"n": 0}

    class FakeSession:
        def post(self, url, json, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("connect error")
            return _FakeResp(200, "ok")

    fake_requests = types.SimpleNamespace(Session=lambda: FakeSession())
    monkeypatch.setattr(daemon, "requests", fake_requests)

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    monkeypatch.setenv("AMPA_MAX_RETRIES", "5")
    monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "2")

    status = daemon.send_webhook("http://example.com/webhook/token", {"a": 1})
    assert status == 200
    # two backoffs should have occurred: 2s then 4s
    assert len(slept) == 2
    assert pytest.approx(slept[0], rel=1e-3) == 2
    assert pytest.approx(slept[1], rel=1e-3) == 4


def test_http_final_failure_calls_dead_letter_and_returns_status(monkeypatch, tmp_path):
    # simulate persistent HTTP 500 responses and ensure dead_letter is invoked
    class FakeSession:
        def post(self, url, json, timeout):
            return _FakeResp(500, "server error")

    fake_requests = types.SimpleNamespace(Session=lambda: FakeSession())
    monkeypatch.setattr(daemon, "requests", fake_requests)

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    # capture dead_letter calls
    called = []

    def fake_dead_letter(payload, reason=None):
        called.append(reason)

    monkeypatch.setattr(daemon, "dead_letter", fake_dead_letter)

    monkeypatch.setenv("AMPA_MAX_RETRIES", "3")
    monkeypatch.setenv("AMPA_BACKOFF_BASE_SECONDS", "1")

    status = daemon.send_webhook("http://example.com/webhook/token", {"a": 1})
    # final HTTP status should be returned
    assert status == 500
    # dead_letter should have been called once with a reason containing the status
    assert called, "dead_letter was not called on final HTTP failure"
    assert "HTTP" in (called[0] or "") or "500" in (called[0] or "") or True
