---
name: owner-inference
disable-model-invocation: true
description: "Infer a suspected owner for a failing test via CODEOWNERS, git blame, recent commits. Use when attributing failures."
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


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

```bash
python3 ../report/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met|unmet" \
  --ac "<...>|<...>|met|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

Then close with: `<work-item-id>: <one-line summary>`. Do NOT re-summarize the report in a different format — the report is the summary.
