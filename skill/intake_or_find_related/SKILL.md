---
name: intake_or_find_related
description: Ensure a Worklog work item has sufficient related-context before implementation. The skill will decide whether to run an intake or search Worklog for related items and update the work item description in a conservative, auditable way.
---

## Purpose

Provide a deterministic, agent-friendly workflow that ensures a work item has related context
before implementation. The skill either performs a lightweight intake (when the item lacks
basic context) or searches existing Worklog items for related work and appends safe
references to the work item description.

## When to use

- Before running the `implement` skill on a work item when you want to ensure the target
  work item has traceable related context (related work items, parents, or references).

## Inputs

- work-item id: required. Must be a valid Worklog id (e.g. `SA-0MLBZ4ZU7018UC6U`).
- Optional runtime flags: `--dry-run` (do not perform updates), `--verbose` (log steps).

## Outputs

- A JSON summary printed to stdout with keys:
  - intakePerformed (boolean)
  - relatedFound (boolean)
  - updatedDescription (string)
  - addedRelatedIds (array of ids)
  - dryRun (boolean) — present when run with `--dry-run` to indicate no side-effects were made.

## Decision logic & Steps (agentic)

The skill is designed to be run by other agents. It follows a small, auditable decision tree:

1. Fetch the current work item (`wl show <id> --json`) and normalize the fields used: title, description, stage.

2. Definition gate (fast checks):
   - If the description already contains conservative related markers (examples: `related-to:`, `discovered-from:`, `blocked-by:`) then the skill should not perform further searches and should return success with relatedFound=true.
   - If the work item is in `idea` stage or the description is empty, mark intake as required.

3. Intake branch:
   - Call the intake workflow non-interactively: `opencode run "/intake <id>"`.
   - Re-fetch the work item and re-evaluate related markers. If intake produced related markers, return success.
   - If intake did not produce sufficient context, return a controlled error explaining what is missing (this is the safe failure mode so callers can decide to escalate to a human).

4. Search branch (only when intake not required and no related markers):
   - Derive a short list (3–6) of conservative keywords from the title and description.
   - Run `wl list <keyword> --json` for each keyword and collect candidate items.
   - Filter candidates to remove the subject work item and items that are clearly irrelevant (e.g., very old with low relevance).
   - Append a minimal `related-to: <id>` line per unique candidate to the work item description using `wl update`.

5. Return a compact JSON summary describing actions taken, any added ids, and whether side-effects were performed (dry-run flag).

## Hard requirements

- Do not create new work items. Only update the description of the input work item.
- Keep matching conservative to avoid false positives. Only append `related-to:` lines when confidence is reasonable (keyword match and short title similarity).
- Respect `--dry-run`: when set, do not call `wl update` or `opencode run`; instead, return the planned changes in the JSON summary.
- Preserve author intent: do not rewrite or remove existing description text except to append `related-to:` lines.

## Acceptance criteria

- The skill exposes a deterministic CLI entrypoint (script) and returns JSON on stdout.
- When the work item already contains related markers the skill returns quickly without side-effects and relatedFound=true.
- When the work item is missing context and stage indicates `idea`, the skill runs intake and returns intakePerformed=true and updated description on success.
- When the work item is missing context but intake is not required, the skill searches Worklog and appends related markers when candidates are found; relatedFound and addedRelatedIds reflect results.

## Examples

Dry-run example (no side-effects):

```bash
python3 skill/intake_or_find_related/scripts/run.py --dry-run SA-0MLBZ4ZU7018UC6U
```

Normal run:

```bash
python3 skill/intake_or_find_related/scripts/run.py SA-0MLBZ4ZU7018UC6U
```

Example JSON output:

```json
{
  "intakePerformed": false,
  "relatedFound": true,
  "updatedDescription": "Summary...\n\nrelated-to: SA-...",
  "addedRelatedIds": ["SA-..."],
  "dryRun": false
}
```

## Tests

- Unit tests should cover: detection of related markers, intake decision path, search path, append behavior, and dry-run behavior.

## References to Bundled Resources

- scripts/run.py — reference implementation that follows this workflow. Unit tests live under `tests/`.

## Security note

- Do not leak credentials or large work item bodies in logs. When posting summaries to comments, prefer short extracts and ids.

End.
