---
name: report
disable-model-invocation: true
description: "Render canonical end-of-session reports: standardized format, deterministic Meta-Data, ContextHub icons. Use when ending a session."
---

## Purpose

Standardize the end-of-session summary every work-item skill produces. A
single, quickly scannable report lets producers see at a glance: what was
done, are the acceptance criteria met, what state is the work item in, and
what (if anything) the producer must do next.

This is a **helper skill**: it defines the canonical report format and
provides a deterministic metadata renderer. Other skills (intake, plan,
implement, audit, ship, …) call it as their **final step**. It does not
replace a skill's own workflow or status-lifecycle management.

## When to use

- **Always at the end of a work-item skill session** — the report is the
  **last thing the skill puts in its final response** before handing control
  back to the operator (paste the rendered output verbatim — never leave it
  only as tool output).
- When a producer asks "what happened in that session?" — the report is the
  single place to look.

## The canonical report template

Exact section order (do not reorder, do not rename):

```markdown
# Completed <skill_name>

**<title>** (<id>)

<headline summary — 1-3 sentences, prose>

## Acceptance Criteria

| AC# | Description | Metric | Verdict |
|-----|-------------|--------|---------|
| 1 | <Description> | <Metric> | met |
| 2 | <Description> | <Metric> | unmet |

## Meta-Data

- Type: <icon> <text>
- Priority: <icon> <text>
- Status: <icon> <text>
- Stage: <icon> <text>
- Risk: <icon> <text>
- Effort: <icon> <text>
- Children: <count>
- Audit: <icon> <text>

## Producer Actions

None needed

## Notes

<freeform agent commentary>

## Conclusion

This completes the <skill_name> process for <id> (<title>). Ready for <next_action>.
```

### Layout rules

- **Header**: `# Completed <skill_name>` (the calling skill's name, e.g.
  `# Completed plan`).
- **Title line**: `**<title>** (<id>)` — the work-item title bolded, id in
  parens.
- **Headline**: 1-3 sentences of prose summarising the session.
- **AC table**: rows are `| <ac#> | <Description> | <Metric> | <met|unmet> |`
  with the verdict column literally `met` or `unmet`. AC numbers run
  1..N in the order given. When no criteria were evaluated, a single
  `| — | No acceptance criteria supplied | — | — |` row is rendered.
- **Meta-Data**: each field on its own bullet as `<icon> <text>`; `Children`
  is a plain count (no ContextHub icon exists for it). Deterministic from
  `wl show <id> --children --json` (see [Meta-Data](#meta-data) below).
- **Producer Actions**: default `None needed`; only list real producer
  actions when something is required.
- **Notes**: freeform — context, caveats, assumptions that don't fit the
  structured sections. **Must sit between `## Producer Actions` and
  `## Conclusion`**.
- **Conclusion**: the literal sentence
  `This completes the <skill_name> process for <id> (<title>). Ready for <next_action>.`
- **Terminator**: the rendered report's final line is `</end_session>` on its own line (only for terminal sessions; question-ended sessions omit the report and the marker).

## Meta-Data

Rendered deterministically from `wl show <id> --children --json` — never
from the agent's memory. Fields and sources:

| Field | Source (`wl show --children --json`) | Icon source |
|-------|--------------------------------------|-------------|
| Type | `workItem.issueType` | epic only (ContextHub epic icon) |
| Priority | `workItem.priority` | ContextHub priority set |
| Status | `workItem.status` | ContextHub status set |
| Stage | `workItem.stage` | ContextHub stage set |
| Risk | `workItem.risk` | ContextHub risk set |
| Effort | `workItem.effort` | ContextHub effort set (incl. full-text aliases) |
| Children | `children` array length | — (count only) |
| Audit | `auditResult` (`null` → not run; `readyToClose` true/false) | ContextHub audit set |

Unknown/missing values render the neutral marker `— N/A`; known values with
no ContextHub icon (e.g. non-epic issue types) render as plain text. Icons
are decorative — the textual value always accompanies them.

## Icon mappings (ContextHub canonical set)

Source of truth: the **ContextHub** project (sibling of this repo —
``docs/icons-design.md`` spec and ``src/icons.ts`` implementation, consumed by
the `wl` CLI). The report helper mirrors these mappings — do not invent new
icons in individual skills. Bracketed-text fallbacks are used when icons are
unavailable (`--no-icons` / `WL_NO_ICONS=1` / non-emoji terminals); the
fallback always accompanies the icon so reports stay readable everywhere.

| Field | Value | Icon | Fallback |
|-------|-------|------|----------|
| Priority | critical | 🚨 | `[CRIT]` |
| Priority | high | ⭐ | `[HIGH]` |
| Priority | medium | 📋 | `[MED]` |
| Priority | low | 🐢 | `[LOW]` |
| Status | open | 🔓 | `[OPEN]` |
| Status | in-progress | 🔄 | `[INPR]` |
| Status | completed | ✔️ | `[DONE]` |
| Status | blocked | ⛔ | `[BLKD]` |
| Status | deleted | 🗑️ | `[DEL]` |
| Status | input_needed | 💬 | `[HELP]` |
| Stage | idea | 💡 | `[IDEA]` |
| Stage | intake_complete | 📥 | `[INTAKE]` |
| Stage | plan_complete | 📋 | `[PLAN]` |
| Stage | in_progress | 🛠️ | `[PROG]` |
| Stage | in_review | 🔍 | `[REVIEW]` |
| Stage | done | 🏁 | `[DONE]` |
| Risk | low | 🌱 | `[LOW]` |
| Risk | medium | ⚠️ | `[MED]` |
| Risk | high | 🔥 | `[HIGH]` |
| Risk | severe | 🚨 | `[SEV]` |
| Effort | XS / extra small | 🐜 | `[XS]` |
| Effort | S / small | 🐇 | `[S]` |
| Effort | M / medium | 🐕 | `[M]` |
| Effort | L / large | 🐘 | `[L]` |
| Effort | XL / extra large / xlarge | 🐋 | `[XL]` |
| Audit | passed (`readyToClose: true`) | ✅ | `[YES]` |
| Audit | failed (`readyToClose: false`) | ❌ | `[NO]` |
| Audit | not run (`auditResult: null`) | ❔ | `[UNKN]` |
| Type | epic | 🏰 | `[EPIC]` |

> **Note:** `src/icons.ts` pads some CLI fallbacks for column alignment
> (e.g. `[MED ]`, `[LOW ]`, `[DEL ]`); the report helper uses the unpadded
> documented values so bracketed text stays clean in markdown.

## How to invoke the helper (the contract)

Other skills embed this block as their **final step**. The helper renders
the report and prints it to stdout; the calling skill then **pastes the
printed report verbatim into its final response** so the operator sees it.

````markdown
### Final step: standardized end-of-session report

**Path resolution (read first):** The `$(skill_path report)` in the command
below is resolved at runtime by the **`skill_path` shell shim** (installed at
`~/.pi/agent/bin/skill_path`, first on PATH). When pasting this block into a
bash command, the shim resolves `$(skill_path report)` to the absolute skill
directory — it works from any project CWD. The shim mirrors the pi
`skill_path` tool's search order (`~/.pi/agent/skills/<name>`, then
`<cwd>/.pi/skills/<name>/`).

> ⚠️ **Do NOT** try to "resolve via the skill_path tool" separately and then
> paste the literal `$(skill_path report)` into bash — that was the original
> bug (SA-0MT7U3NCB000T1SR). The shim handles it automatically.

Render the canonical report before returning control to the operator:

```bash
python3 $(skill_path report)/scripts/render_report.py <work-item-id> \
  --skill-name <skill_name> \
  --headline "<1-3 sentence headline summary>" \
  --ac "<AC# description>|<verification metric>|met" \
  --ac "<...>|<...>|unmet" \
  [--producer-actions "<actions for the producer, or omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary.
````

Rules for callers:

1. **AC verdicts come from the calling skill's own evaluation** — the
   renderer never guesses them; pass one `--ac` per criterion.
2. **Meta-Data is rendered by the script** from live `wl show --json` —
   the caller supplies only prose (headline, Producer Actions, Notes,
   next action).
3. Run it as the **final step**, after all status/stage transitions, so the
   Meta-Data reflects the end-of-session state.
4. Output is stdout markdown; the calling skill **pastes it verbatim into
   its final response** — tool output alone does not put the report in front
   of the operator. (Persisting the report to the work item is a follow-up;
   v1 is stdout only.)
5. The rendered report ends with `</end_session>` on its own line as its final line for terminal sessions; question-ended sessions do not render the report or the marker.

## Renderer

`$(skill_path report)/scripts/render_report.py`:

- `render_report(data, *, skill_name, headline, ac_rows, producer_actions=None, notes="", next_action="review", no_icons=False)` → full report markdown.
- `render_metadata(data, no_icons=False)` → the `## Meta-Data` bullet block.
- `render_ac_table(ac_rows)` → the AC table body.
- `extract_metadata(data, no_icons=False)` → ordered `{label: (icon_text, raw_text)}` dict.
- CLI (`render_report.py <id> --skill-name ... --ac ...`): fetches the item
  via `wl show <id> --children --json` (worklog store resolved via the
  shared prefix-to-sibling scan, so it works from any cwd incl. git
  worktrees) and prints the report.

Tests: `./tests/test_render_report.py` (offline — fixture JSON,
no live `wl` calls). Run with `python3 -m pytest ./tests`.

## Example rendered report

```markdown
# Completed plan

**Standardize skill session end-of-session reporting via helper report skill** (SA-0MSJ082OY003IQ8S)

Planned the 4-feature breakdown and created 4 child items in test-first order.

## Acceptance Criteria

| AC# | Description | Metric | Verdict |
|---|---|---|---|
| 1 | Plan approved by operator | Interactive approval | met |
| 2 | Child items created in dependency order | wl show --children | met |

## Meta-Data

- Type: feature
- Priority: 📋 medium
- Status: 🔄 in-progress
- Stage: 📋 plan_complete
- Risk: ⚠️ Medium
- Effort: 🐕 Medium
- Children: 4
- Audit: ❔ not run

## Producer Actions

None needed

## Notes

Plan deferred until the report-helper tests land (dependency: tests → impl → wiring → docs).

## Conclusion

This completes the plan process for SA-0MSJ082OY003IQ8S (Standardize skill session end-of-session reporting via helper report skill). Ready for review.
```

## Related

- `../audit/SKILL.md` — audit's **persisted, machine-verified** report
  format is preserved; only its end-of-session summary reconciles with this
  template.
- **ContextHub** (sibling project, `docs/icons-design.md` / `src/icons.ts`) —
  canonical icon set consumed by the `wl` CLI.
- `../effort-and-risk/SKILL.md` — risk (low/medium/high) and effort
  t-shirt (XS–XL) values rendered in Meta-Data.
- `../shared/status_lifecycle.py` — the shared status/stage transitions
  whose results the Meta-Data Status/Stage fields reflect.
