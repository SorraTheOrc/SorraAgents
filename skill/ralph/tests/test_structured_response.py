import json

from skill.ralph.scripts.structured_response import parse_structured_response


def test_parse_structured_response_extracts_summary_and_actions():
    raw = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "summary": "Add a regression test",
                "actions": [
                    {"command": "pytest", "args": ["tests/test_ralph_loop.py", "-q"]},
                    {"type": "bash", "args": "python3 -m pytest -q"},
                ],
            },
        }
    )

    parsed = parse_structured_response(raw)

    assert parsed is not None
    assert parsed.text == "Add a regression test"
    assert parsed.summary == "Add a regression test"
    assert parsed.actions[0].command == "pytest"
    assert parsed.actions[0].args == ("tests/test_ralph_loop.py", "-q")
    assert parsed.actions[1].command == "bash"
    assert parsed.actions[1].args == ("python3", "-m", "pytest", "-q")
    assert "Structured remediation actions:" in parsed.render()


def test_parse_structured_response_uses_actions_when_summary_missing():
    raw = json.dumps(
        {
            "type": "assistant_response",
            "actions": [
                {"command": "edit", "args": ["skill/ralph/scripts/ralph_loop.py"]},
            ],
        }
    )

    parsed = parse_structured_response(raw)

    assert parsed is not None
    assert parsed.text == "edit skill/ralph/scripts/ralph_loop.py"
    assert parsed.summary == "edit skill/ralph/scripts/ralph_loop.py"
    assert parsed.actions[0].render() == "edit skill/ralph/scripts/ralph_loop.py"


def test_parse_structured_response_returns_none_for_unstructured_json():
    raw = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": []}})

    assert parse_structured_response(raw) is None
