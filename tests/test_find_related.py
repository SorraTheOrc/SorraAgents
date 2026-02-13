import json

from types import SimpleNamespace

from skill.find_related.scripts import run as find_related


class DummyProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_no_related_marker_and_found_candidates(monkeypatch):
    calls = []
    work_id = "SA-FR-1"

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["wl", "show"]:
            payload = {
                "workItem": {
                    "id": work_id,
                    "title": "Add widget",
                    "description": "Make the widget",
                }
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            payload = [{"id": "SA-OTHER-1"}]
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "update"]:
            return DummyProc(stdout=json.dumps({"success": True}))
        return DummyProc(stdout="{}")

    monkeypatch.setattr(find_related, "_run", fake_run)

    result = find_related.run(work_id, dry_run=False, verbose=False, with_report=False)

    # When description is empty the current implementation treats candidate discovery
    # as present but will only set found True if candidate_ids exist; ensure behavior
    # reflects that we discovered an id even in dry-run mode.
    assert result["found"] is True
    assert result["addedIds"] == ["SA-OTHER-1"]
    assert "related-to: SA-OTHER-1" in result["updatedDescription"]


def test_dry_run_no_update(monkeypatch):
    work_id = "SA-FR-2"

    def fake_run(cmd):
        if cmd[:2] == ["wl", "show"]:
            # include an extra token so the keyword extractor yields terms
            payload = {
                "workItem": {
                    "id": work_id,
                    "title": "Foo bar widget",
                    "description": "",
                }
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            payload = [{"id": "SA-OLD-1"}]
            return DummyProc(stdout=json.dumps(payload))
        return DummyProc(stdout="{}")

    monkeypatch.setattr(find_related, "_run", fake_run)

    result = find_related.run(work_id, dry_run=True, verbose=False, with_report=False)

    assert result["found"] is True
    assert result["dryRun"] is True
    assert result["addedIds"] == ["SA-OLD-1"]


def test_with_report_calls_llm(monkeypatch):
    work_id = "SA-FR-3"
    llm_called = {"count": 0}

    def fake_run(cmd):
        if cmd[:2] == ["wl", "show"]:
            payload = {
                "workItem": {"id": work_id, "title": "Document", "description": "doc"}
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            return DummyProc(stdout=json.dumps([]))
        return DummyProc(stdout="{}")

    def fake_llm(work_item, candidate_ids):
        llm_called["count"] += 1
        return "Automated report: no direct matches found. See docs."

    monkeypatch.setattr(find_related, "_run", fake_run)

    result = find_related.run(
        work_id, dry_run=True, verbose=False, with_report=True, llm_hook=fake_llm
    )

    assert llm_called["count"] == 1
    assert result["reportInserted"] is False
    assert "Related work (automated report)" in result["updatedDescription"]
