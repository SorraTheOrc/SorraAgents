import json

from skill.intake_or_find_related.scripts import run as intake_skill


class DummyProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_intake_runs_when_stage_is_idea(monkeypatch):
    calls = []
    work_id = "SA-TEST-IDEA"
    intake_calls = {"count": 0}

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["wl", "show"]:
            if intake_calls["count"] == 0:
                payload = {
                    "workItem": {
                        "id": work_id,
                        "stage": "idea",
                        "title": "New idea",
                        "description": "",
                    }
                }
            else:
                payload = {
                    "workItem": {
                        "id": work_id,
                        "stage": "intake_complete",
                        "title": "New idea",
                        "description": "Summary: updated",
                    }
                }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["opencode", "run"]:
            intake_calls["count"] += 1
            return DummyProc(stdout="ok")
        return DummyProc(stdout="{}")

    monkeypatch.setattr(intake_skill, "_run", fake_run)

    result = intake_skill.run(work_id)

    assert result["intakePerformed"] is True
    assert any(cmd[:2] == ["opencode", "run"] for cmd in calls)
    assert result["updatedDescription"] == "Summary: updated"


def test_search_updates_description_when_related_found(monkeypatch):
    calls = []
    work_id = "SA-TEST-REL"

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["wl", "show"]:
            payload = {
                "workItem": {
                    "id": work_id,
                    "stage": "plan_complete",
                    "title": "Add related search",
                    "description": "Summary: add related search",
                }
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            payload = {"workItems": [{"id": "SA-RELATED-1"}]}
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "update"]:
            return DummyProc(stdout=json.dumps({"success": True}))
        return DummyProc(stdout="{}")

    monkeypatch.setattr(intake_skill, "_run", fake_run)

    result = intake_skill.run(work_id)

    assert result["intakePerformed"] is False
    assert result["relatedFound"] is True
    assert result["addedRelatedIds"] == ["SA-RELATED-1"]
    assert any(cmd[:2] == ["wl", "update"] for cmd in calls)
    assert "related-to: SA-RELATED-1" in result["updatedDescription"]


def test_search_returns_false_when_no_related(monkeypatch):
    work_id = "SA-TEST-NONE"

    def fake_run(cmd):
        if cmd[:2] == ["wl", "show"]:
            payload = {
                "workItem": {
                    "id": work_id,
                    "stage": "plan_complete",
                    "title": "Nothing related",
                    "description": "Summary: nothing related",
                }
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            payload = {"workItems": []}
            return DummyProc(stdout=json.dumps(payload))
        return DummyProc(stdout="{}")

    monkeypatch.setattr(intake_skill, "_run", fake_run)

    result = intake_skill.run(work_id)

    assert result["intakePerformed"] is False
    assert result["relatedFound"] is False
    assert result["addedRelatedIds"] == []


def test_short_circuits_when_related_marker_present(monkeypatch):
    work_id = "SA-TEST-HAS-REL"

    def fake_run(cmd):
        if cmd[:2] == ["wl", "show"]:
            payload = {
                "workItem": {
                    "id": work_id,
                    "stage": "plan_complete",
                    "title": "Has related",
                    "description": "Summary: ok\n\nrelated-to: SA-EXISTING-1",
                }
            }
            return DummyProc(stdout=json.dumps(payload))
        if cmd[:2] == ["wl", "list"]:
            return DummyProc(stdout=json.dumps({"workItems": []}))
        return DummyProc(stdout="{}")

    monkeypatch.setattr(intake_skill, "_run", fake_run)

    result = intake_skill.run(work_id)

    assert result["relatedFound"] is True
    assert result["addedRelatedIds"] == []
