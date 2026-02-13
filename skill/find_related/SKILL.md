---
name: find_related
description: |
  Discover related work for a Worklog work item and generate a concise, auditable "Related work"
  report that can be appended to the work item description. This skill performs discovery and
  reporting only — it does not perform intake interviews.
---

## Purpose

Provide a deterministic, agent-friendly way to discover existing or prior work related to a
work item. The skill searches Worklog (open + closed), inspects repository files and docs with
conservative heuristics, and can optionally generate an LLM-backed "Related work (automated report)"
that is inserted into the work item description under a clearly-marked section.

## When to use

- When a work item needs evidence of related or precedent work before planning or implementation.
- When the intake process wants to augment context with an automated report without replacing human
  authored intake drafts.

## Inputs

- work-item id (required)
- flags: `--dry-run` (do not perform updates), `--verbose` (log steps), `--with-report` (generate and insert LLM-backed report)

## Outputs

- JSON summary printed to stdout, keys:
  - found (boolean)
  - addedIds (array of work item ids appended)
  - reportInserted (boolean)
  - updatedDescription (string)
  - dryRun (boolean)

## Decision logic

1. Fetch work item: `wl show <id> --json`.
2. If the description already contains related markers (e.g., `related-to:`) return quickly with found=true.
3. Derive conservative keywords from the title/description and run `wl list <keyword> --json` to collect candidates.
4. Inspect repository files (docs/README.md, README.md, and other top-level docs) for matching keywords; include those as repo matches.
5. If `--with-report` is set, call the LLM report generator hook to produce a short informational report. The report MUST be clearly labeled and inserted under the heading "Related work (automated report)".
6. If candidates are found and dry-run is false, append `related-to: <id>` lines and the report (if generated) to the work item description with `wl update`.
7. Return the JSON summary.

## Hard requirements

- Do not perform intake. This skill only discovers and reports.
- Default behaviour must be conservative: prefer false negatives over false positives when appending `related-to:` lines.
- Respect `--dry-run` flag: do not call `wl update` when dry-run is set.
- The LLM report generator must be a pluggable hook so tests can mock it. The default implementation returns an empty string.

## Tests

- Unit tests must mock `wl` interactions and the LLM hook. Tests should cover dry-run, candidate discovery, and report insertion.

## References to bundled resources

- scripts/run.py — reference implementation providing a testable API and CLI.

End.
