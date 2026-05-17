import tests.validate_agents as val


def test_agent_front_matter_valid():
    results = val.validate_all_agents(base="agent")
    errors = {p: r['errors'] for p, r in results.items() if r['errors']}
    # Fail the test if any agent file has errors (missing required fields, disallowed models)
    if errors:
        msgs = []
        for p, es in errors.items():
            msgs.append(f"{p}: {', '.join(es)}")
        raise AssertionError("Agent front-matter validation errors:\n" + "\n".join(msgs))


def test_agent_front_matter_warnings_are_visible():
    # Warnings should not fail the test suite, but we assert they are a list per-file
    results = val.validate_all_agents(base="agent")
    for p, r in results.items():
        assert isinstance(r.get('warnings', []), list)


def test_wildcard_justification_suppresses_warning():
    """Wildcard bash permissions with a documented justification should not produce warnings."""
    fm_raw = (
        "description: Test agent\n"
        "mode: primary\n"
        "model: github-copilot/gpt-5.2\n"
        "temperature: 0.4\n"
        "permission:\n"
        "  bash:\n"
        "    '*': allow  # wildcard-bash-justification: needed for CI tasks\n"
    )
    data = {"description": "Test", "mode": "primary", "model": "github-copilot/gpt-5.2",
            "temperature": 0.4, "permission": {"bash": {"*": "allow"}}}
    errors, warnings = val.validate_front_matter(data, "", fm_raw)
    assert errors == []
    assert warnings == []


def test_wildcard_without_justification_produces_warning():
    """Wildcard bash permissions without justification should still produce a warning."""
    fm_raw = (
        "description: Test agent\n"
        "mode: primary\n"
        "model: github-copilot/gpt-5.2\n"
        "temperature: 0.4\n"
        "permission:\n"
        "  bash:\n"
        "    '*': allow\n"
    )
    data = {"description": "Test", "mode": "primary", "model": "github-copilot/gpt-5.2",
            "temperature": 0.4, "permission": {"bash": {"*": "allow"}}}
    errors, warnings = val.validate_front_matter(data, "", fm_raw)
    assert errors == []
    assert any("wildcard" in w for w in warnings)


def test_contradiction_justification_suppresses_warning():
    """Tool/boundary contradictions with a documented justification should not produce warnings."""
    fm_raw = (
        "description: Test agent\n"
        "mode: primary\n"
        "model: github-copilot/gpt-5.2\n"
        "temperature: 0.4\n"
        "tools:\n"
        "  write: true\n"
        "  # tools-write-contradiction-justification: write only for worklog, not code\n"
    )
    data = {"description": "Test", "mode": "primary", "model": "github-copilot/gpt-5.2",
            "temperature": 0.4, "tools": {"write": True}}
    body = "\nBoundaries:\n- Never modify code directly\n"
    errors, warnings = val.validate_front_matter(data, body, fm_raw)
    assert errors == []
    assert warnings == []


def test_contradiction_without_justification_produces_warning():
    """Tool/boundary contradictions without justification should still produce a warning."""
    fm_raw = (
        "description: Test agent\n"
        "mode: primary\n"
        "model: github-copilot/gpt-5.2\n"
        "temperature: 0.4\n"
        "tools:\n"
        "  write: true\n"
    )
    data = {"description": "Test", "mode": "primary", "model": "github-copilot/gpt-5.2",
            "temperature": 0.4, "tools": {"write": True}}
    body = "\nBoundaries:\n- Never modify code directly\n"
    errors, warnings = val.validate_front_matter(data, body, fm_raw)
    assert errors == []
    assert any("contradiction" in w for w in warnings)


def test_all_agents_pass_validation():
    """All existing agent files pass validation with no errors and no warnings."""
    results = val.validate_all_agents(base="agent")
    for path, r in results.items():
        assert r["errors"] == [], f"{path} has errors: {r['errors']}"
        assert r["warnings"] == [], f"{path} has warnings: {r['warnings']}"


def test_missing_required_field_temperature():
    """A front-matter missing 'temperature' should produce an error."""
    data = {"description": "Test", "mode": "primary", "model": "github-copilot/gpt-5.2"}
    errors, warnings = val.validate_front_matter(data, "")
    assert any("temperature" in e for e in errors)
