import json
import logging

import pytest

import os
import sys

# Ensure repo root is on sys.path so `import ampa` works when pytest is invoked
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from ampa import webhook


def test_dead_letter_logs_error_and_writes_file(caplog, tmp_path, monkeypatch):
    """dead_letter() should log an ERROR with reason and truncated payload and
    also append a JSON record to the dead-letter file when webhook is not set.
    """
    caplog.set_level(logging.ERROR)

    # Ensure requests is not available so the code paths take the file fallback
    monkeypatch.setattr(webhook, "requests", None)

    dl_file = tmp_path / "ampa_deadletter.log"
    monkeypatch.setenv("AMPA_DEADLETTER_FILE", str(dl_file))

    payload = {"k": "v", "big": "x" * 2000}

    webhook.dead_letter(payload, reason="unit-test-reason")

    # Verify an ERROR log entry was emitted mentioning dead_letter invocation
    found = False
    for rec in caplog.records:
        if rec.levelno == logging.ERROR and "dead_letter invoked" in rec.getMessage():
            assert "unit-test-reason" in rec.getMessage()
            found = True
            break
    assert found, "Expected ERROR log for dead_letter invocation not found"

    # Verify the dead-letter file was appended with a JSON record containing reason and payload
    text = dl_file.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines, "Dead-letter file should contain at least one record"
    rec = json.loads(lines[-1])
    assert rec.get("reason") == "unit-test-reason"
    assert "payload" in rec
