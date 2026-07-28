"""Integration-style test for the find-related script using a fake `wl` CLI.

This test runs the actual find_related.py script via a subprocess and provides
a small fake `wl` executable placed on PATH that reads/modifies a JSON state file
to emulate `wl show`/`wl search`/`wl update` behaviour.

It verifies:
- a first run fetches a work item, searches for related items, and updates the description
- a second run is idempotent and does not duplicate the report section
"""

import json
import os
import subprocess
import sys


def _write_wl_state(path, state):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _make_fake_wl_script(tmp_path, state_file):
    """Create a fake `wl` executable that uses a JSON state file."""
    wl_script = tmp_path / "wl"
    wl_script.write_text(
        rf"""#!/usr/bin/env python3
import json, sys, os

state_path = {str(state_file)!r}

with open(state_path, 'r', encoding='utf-8') as fh:
    state = json.load(fh)

args = sys.argv[1:]
if not args:
    print('{{}}')
    sys.exit(0)

cmd = args[0]

missing_ids = state.get('missing_show_ids', [])

if cmd == 'show':
    work_item_id = args[1]
    if work_item_id in missing_ids:
        print('', file=sys.stderr)
        sys.exit(1)
    if '--json' in args:
        item = state.get('show', {{}})
        if work_item_id in state.get('work_items', {{}}):
            item = state['work_items'][work_item_id]
        # Match real wl show --json output format
        print(json.dumps({{"success": True, "workItem": item}}))
    sys.exit(0)

elif cmd == 'search':
    # Skip --semantic flag if present to find the actual keyword
    filtered_args = [a for a in args[1:] if not a.startswith('--')]
    keyword = filtered_args[0] if filtered_args else ''
    results = state.get('search_results', {{}}).get(keyword, [])
    # Ensure --json format matches real wl search output (bare list is also OK)
    print(json.dumps(results))
    sys.exit(0)

elif cmd == 'update':
    work_item_id = args[1]
    desc_idx = args.index('--description') if '--description' in args else -1
    if desc_idx >= 0 and desc_idx + 1 < len(args):
        new_desc = args[desc_idx + 1]
        state['updated_description'] = new_desc
        if work_item_id in state.get('work_items', {{}}):
            state['work_items'][work_item_id]['description'] = new_desc
        with open(state_path, 'w', encoding='utf-8') as fh:
            json.dump(state, fh)
    print(json.dumps({{}}))
    sys.exit(0)

print('{{}}')
""",
        encoding="utf-8",
    )
    wl_script.chmod(0o755)
    return wl_script


def test_find_related_integration(tmp_path, monkeypatch):
    """End-to-end integration test of the find_related script."""
    repo_root = os.getcwd()
    state_file = tmp_path / "wl_state.json"

    # Setup the fake wl state
    initial_state = {
        "work_items": {
            "TEST-001": {
                "id": "TEST-001",
                "title": "Add automation script for find-related skill",
                "description": "## Summary\nCreate a Python script to find related work items.\n",
                "status": "open",
            }
        },
        "search_results": {
            "automation": [
                {
                    "id": "REL-001",
                    "title": "Previous automation work",
                    "description": "Related automation work",
                    "status": "completed",
                }
            ],
            "script": [
                {
                    "id": "REL-001",
                    "title": "Previous automation work",
                    "description": "Related automation work",
                    "status": "completed",
                },
                {
                    "id": "REL-002",
                    "title": "Script refactoring task",
                    "description": "Refactoring scripts",
                    "status": "open",
                },
            ],
        },
        "show": {
            "id": "TEST-001",
            "title": "Add automation script for find-related skill",
            "description": "## Summary\nCreate a Python script to find related work items.\n",
            "status": "open",
        },
        "updated_description": "",
    }
    _write_wl_state(state_file, initial_state)

    # Create fake wl script
    _make_fake_wl_script(tmp_path, state_file)

    # Use the real python executable to run the find_related script
    script_path = os.path.join(
        repo_root, "skill/find-related/scripts/find_related.py"
    )

    env = os.environ.copy()
    # Prepend tmp_path to PATH so fake `wl` is picked up
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    # First run: should find related items and update description
    proc = subprocess.run(
        [
            sys.executable,
            script_path,
            "--work-item-id",
            "TEST-001",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, f"find_related failed: {proc.stderr}"
    out = json.loads(proc.stdout)

    assert out["workItemId"] == "TEST-001"
    assert out["found"] is True
    assert len(out["addedIds"]) == 2  # REL-001 and REL-002
    assert "REL-001" in out["addedIds"]
    assert "REL-002" in out["addedIds"]
    assert out["reportInserted"] is True
    assert out["relatedItemCount"] == 2
    assert out["repoMatchCount"] >= 0

    # Verify the description was updated
    with open(state_file, "r") as fh:
        updated_state = json.load(fh)
    updated_desc = updated_state.get("updated_description", "")
    assert "Related work (automated report)" in updated_desc
    assert "REL-001" in updated_desc
    assert "REL-002" in updated_desc
    assert "Previous automation work" in updated_desc or "Script refactoring" in updated_desc

    # Second run: verify idempotency
    proc2 = subprocess.run(
        [
            sys.executable,
            script_path,
            "--work-item-id",
            "TEST-001",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc2.returncode == 0, f"second run failed: {proc2.stderr}"

    # Verify idempotency: the report section should not be duplicated
    with open(state_file, "r") as fh:
        state_after_second = json.load(fh)
    desc_after_second = state_after_second.get("updated_description", "")
    # Should only have one automated report section
    assert desc_after_second.count("Related work (automated report)") == 1, (
        "Report section should not be duplicated"
    )


def test_find_related_integration_no_results(tmp_path):
    """Integration test with no related items found."""
    repo_root = os.getcwd()
    state_file = tmp_path / "wl_state.json"

    initial_state = {
        "work_items": {
            "TEST-002": {
                "id": "TEST-002",
                "title": "Completely unique work item",
                "description": "## Summary\nUnique description with no matches.\n",
                "status": "open",
            }
        },
        "search_results": {},
        "show": {
            "id": "TEST-002",
            "title": "Completely unique work item",
            "description": "## Summary\nUnique description with no matches.\n",
            "status": "open",
        },
        "updated_description": "",
    }
    _write_wl_state(state_file, initial_state)

    _make_fake_wl_script(tmp_path, state_file)

    script_path = os.path.join(
        repo_root, "skill/find-related/scripts/find_related.py"
    )

    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            script_path,
            "--work-item-id",
            "TEST-002",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, f"find_related failed: {proc.stderr}"
    out = json.loads(proc.stdout)

    assert out["workItemId"] == "TEST-002"
    assert out["found"] is False or out["relatedItemCount"] == 0


def test_find_related_integration_show_failure(tmp_path):
    """Integration test with wl show failure."""
    repo_root = os.getcwd()
    state_file = tmp_path / "wl_state.json"

    # No work item for SHOW-FAIL, so show will return empty
    initial_state = {
        "work_items": {},
        "search_results": {},
        "show": {
            "id": "SHOW-FAIL",
            "title": "",
            "description": "",
            "status": "open",
        },
        "missing_show_ids": ["NONEXISTENT"],
        "updated_description": "",
    }
    _write_wl_state(state_file, initial_state)

    _make_fake_wl_script(tmp_path, state_file)

    script_path = os.path.join(
        repo_root, "skill/find-related/scripts/find_related.py"
    )

    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            script_path,
            "--work-item-id",
            "NONEXISTENT",
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # Should exit with error
    assert proc.returncode != 0
