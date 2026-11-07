# Standup skill

Produce a short, first-person standup report from the Herdr selection list,
written to be read aloud (TTS) in under two minutes.

**Rendering instructions live in [`SKILL.md`](SKILL.md)** — that document is the
authoritative contract. This README covers the shape of the skill and its
output.

## Two layers

1. **Deterministic data layer** — `scripts/generate_standup.py` fetches, windows
   and selects the items (`--json` emits one structured array per section:
   `yesterday_items`, `today_critical_items`, `today_additional_items`,
   `regressions_items`, plus `blockers_tts`, `project_name` and `window`). It
   makes no LLM calls, so its behaviour is testable and the report head matches
   the downtime dispatcher's head by construction.
2. **Narrative layer — the invoking agent** — clusters each section's items
   into free-form categories chosen at report time and renders one spoken
   paragraph per category, one sentence per item. Categories are picked
   independently per section; there is no fixed taxonomy.

## Report format

- Five fixed sections in order: "Yesterday I completed work on…", "Open critical
  items", "Additional focus items for today", "Regressions", "Blockers" —
  empty sections still appear with one spoken line.
- Each category is a **grouped paragraph**, not a bullet list: one paragraph per
  category, one sentence per item.
- Each item's sentence is its explicit **`## Impact`** section — one sentence on
  what the change gave the user — when present. An item with no `## Impact`
  falls back to the `Title — user story` phrasing from its description; a
  multi-sentence Impact is trimmed to its first sentence to hold the read
  budget.
- The report **never** renders work item ids (no `WL-…`/`SA-…`, brackets,
  backticks or underscores in spoken lines), and dates are spoken form
  ("5th September 2026").

## Fallback rendering

`format_report()` (the script's own markdown output, written to
`standups/YYYY_MM_DD.md`) is the deterministic fallback for scheduled,
non-agent runs: the same five sections, one impact-first line per item. Use it
when no agent is available to cluster and render; prefer the agent-rendered
grouped narrative everywhere else.

## Usage

```bash
python3 $(skill_path standup)/scripts/generate_standup.py --json   # structured data for agent rendering
python3 $(skill_path standup)/scripts/generate_standup.py         # fallback markdown report
python3 $(skill_path standup)/scripts/generate_standup.py --help  # flags and their defaults
```

Tests live in `tests/`; they cover impact extraction (present, absent,
malformed), the impact-first render with its fallback, the golden grouped
report, and the `SKILL.md` rendering contract.
