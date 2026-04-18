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
