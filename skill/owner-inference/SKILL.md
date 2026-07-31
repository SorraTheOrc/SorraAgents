---
name: owner-inference
description: Infer a suspected owner for a failing test file using CODEOWNERS, git blame, recent commits, and an override map.
---

Purpose
-------

Deterministic heuristic to identify the likely owner of a failing test file. Used by triage's `check_or_create_critical_issue` to populate "suspected owner" in new issues.

When to use
-----------

When triage creates a critical `test-failure` item and needs an owner.

Inputs / Outputs
----------------

**Input**: JSON payload `{ repo_path: ".", file_path: "tests/test_foo.py", commit?: "abc123", confidence_threshold?: 0.3 }`

**Output**: `{ assignee: string, confidence: 0.0-1.0, reason: string, heuristic: string }`

## Heuristics (in priority order)

1. **Override map** — `.worklog/triage/owner-map.yaml`
2. **CODEOWNERS** — GitHub-style file
3. **Git blame** — most frequent author by line count
4. **Recent commits** — most frequent committer (last 50 commits)
5. **Fallback** — `Build` with confidence 0.0

## Script

`./scripts/infer_owner.py`

## Example

```bash
python3 ./scripts/infer_owner.py --repo . --file tests/test_foo.py --commit abc123
wl show SA-0MPYMFZXO0004ZU4 --json
```

## References

- Triage: `../triage/SKILL.md`
- Runbook: `../triage/resources/runbook-test-failure.md`
