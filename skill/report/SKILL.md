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

## Use Cases

- <use-case-1>
- <use-case-2>

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

✅ Ready to ship
This completes the <skill_name> process for <id> (<title>).
```

### Layout rules

- **Header**: `# Completed <skill_name>` (the calling skill's name, e.g.
  `# Completed plan`).
- **Title line**: `**<title>** (<id>)` — the work-item title bolded, id in
  parens.
- **Use Cases**: optional bulleted list of the work-item's use cases.
  Inserted between the title line and the headline. Omitted entirely (no
  heading) when no use cases are supplied.  Each item is a `- <text>` line.
- **Headline**: 1-3 sentences of prose summarising the session.
- **AC table**: rows are `| <ac#> | <Description> | <Metric> | <met|unmet> |`
  with the verdict column literally `met` or `unmet`. AC numbers run
  1..N in the order given. When no criteria were evaluated, a single
  `| — | No acceptance criteria supplied | — | — |` row is rendered.
- **Meta-Data**: each field on its own bullet as `<icon> <text>`; `Children`
  is a plain count (no ContextHub icon exists for it). Deterministic from
  `wl show <id> --children --json` (see [Meta-Data](#meta-data) below).
- **Producer Actions**: actions the producer must take to **complete the
  current stage** — blocking only. Default `None needed`; omit the flag
  entirely when nothing is required. Routine workflow progression is **not**
  a producer action (see [Producer Actions](#producer-actions) below).
- **Notes**: freeform — context, caveats, assumptions that don't fit the
  structured sections. **Must sit between `## Producer Actions` and
  `## Conclusion`**.
- **Conclusion**: the readiness verdict is the **first line** of the section,
  on its own line, colour-coded via ANSI escapes (best-effort — colour is
  emitted only into a colour-capable sink, so pipes, files, redirected output
  and `TERM=dumb` receive the plain verdict with **no** escape bytes):
  - `✅ Ready to ship` in green (`\033[32m…\033[0m`) when every AC is `met`,
    followed by `This completes the <skill_name> process for <id> (<title>).`;
  - `🚫 Not ready to ship` in red (`\033[31m…\033[0m`) otherwise, followed by
    `<Skill_name> session for <id> (<title>) is incomplete.`
  The `next_action` value is no longer rendered; it is retained only for
  backward compatibility.

#### Colour capability (AC3, SA-0MVYEZ3V3004AQXN)

`colour_enabled()` decides whether escapes are emitted, in this precedence:

1. An explicit argument — `render_report(..., colour=True|False)` / CLI
   `--colour` / `--no-colour` — is the most specific instruction and always
   wins (a deliberate per-invocation override).
2. `NO_COLOR` (any non-empty value — <https://no-color.org>) — never colour;
   an env-level opt-out is not overridden by `FORCE_COLOR`.
3. `FORCE_COLOR` (`1`/`true`/`yes`/`on`) — colour even when stdout is not a
   terminal (tests, CI, log viewers that render ANSI).
4. `TERM=dumb` — never colour.
5. `sys.stdout.isatty()` — colour only for a terminal.

The default (`None`) is auto-detection (steps 2-5), which is what keeps
agent-pasted reports escape-free.
- **Terminator**: the rendered report's final line is `</end_session>` on its own line (only for terminal sessions; question-ended sessions omit the report and the marker).

### Producer Actions

The normative rule: `## Producer Actions` contains **only** actions the
producer must take to complete the **current** stage. It answers one
question — *is the producer blocking this work item right now?*

**Belongs in Producer Actions** (blocking, current-stage, agent cannot do it):

- answering an open question the stage is waiting on;
- making a decision between named options;
- granting an approval/review the stage is gated on;
- supplying missing access, credentials, or a token;
- performing a manual step the agent cannot (e.g. a UI-only configuration).

**Does NOT belong** — routine workflow progression. The Conclusion's
standalone ready/not-ready verdict line already conveys the outcome, so
repeating progression here falsely signals that the producer is blocking:

- "Proceed to /skill:plan", "Run /skill:audit next";
- "Proceed to the next stage" / "Proceed with implementation";
- "Review the intake brief, then run /skill:plan";
- "Next step: implement".

When the only "action" is to continue the normal workflow, the section must
read exactly `None needed` (omit `--producer-actions` entirely). Reviewing a
brief or report is part of the normal workflow — only request a *decision*
("choose A or B"), an approval, or a missing *input*.

Positive / negative examples of the rendered section:

```markdown
## Producer Actions

None needed
```

```markdown
## Producer Actions

Answer the OPEN QUESTION in comment SA-C0MVQ1QK1R0052WGP before `/skill:plan` can start.
```

```markdown
## Producer Actions

Proceed to /skill:plan        <!-- WRONG: progression is not a producer action -->
```

The rule is enforced twice, by design: the prose here is the primary fix, and
the renderer guard (below) keeps a report correct when an agent ignores it.
Classification is conservative — ambiguous text is rendered unchanged,
because hiding a real producer action is worse than repeating a routine one.

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

The audit verdict is resolved deterministically, in this precedence order:

1. the structured top-level `auditResult.readyToClose` (authoritative);
2. a legacy boolean `workItem.auditResult` (keeps simple-dict callers working);
3. the legacy `workItem.audit` mirror — an explicit
   `Ready to close: Yes|No` line in `audit.text` wins, otherwise a `status`
   of `Complete` (or similar) is treated as a pass;
4. otherwise `unknown` → `❔ not run` (an unaudited item never fabricates a
   verdict).

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
  --ac "<skill-session deliverable 1 description>|<verification metric>|met" \
  --ac "<skill-session deliverable 2 description>|<verification metric>|met" \
  [--producer-actions "<producer actions needed to complete the CURRENT stage; blocking only — omit for 'None needed'>"] \
  [--notes "<freeform context/caveats/assumptions>"] \
  [--next-action <review|plan|implement|...>] \
  [--use-case "<use-case string>"]
```

> **Note:** The `--ac` values record the **skill session's** own deliverables
> — not the work item's feature acceptance criteria. Pass the concrete
> outcomes the skill itself produces (e.g. "brief drafted", "children
> created", "build green"); the work item's feature ACs belong in the
> **Use Cases** section (SA-0MTQY7YM5009J6QR).

```bash
# Example: intake skill passes its own skill-session deliverables
python3 $(skill_path report)/scripts/render_report.py SA-0EXAMPLE0000 \
  --skill-name intake \
  --headline "Interviewed the producer, captured requirements, and drafted the brief." \
  --ac "Producer interview completed — requirements captured | interview notes | met" \
  --ac "Feature brief drafted and linked to work item | wl show --json | met" \
  --ac "Open questions identified and recorded | appendix entries | met" \
  [--producer-actions "Answer the OPEN QUESTION in comment SA-C0EXAMPLE before proceeding."] \
  [--notes "Interview took 15 minutes; producer confirmed budget constraints."] \
  [--next-action plan]
```

The script prints the rendered report to stdout — **paste it verbatim into
your final response**, so the operator sees the report itself (not just the
tool call), then close with: `<work-item-id>: <one-line summary>`. Do NOT
re-summarize the report in a different format — the report is the summary.
````

Rules for callers:

1. **AC verdicts come from the calling skill's own evaluation** — the
   renderer never guesses them; pass one `--ac` per criterion.
2. **Skill-session ACs, not work-item ACs.** The `--ac` table records the
   **skill session's** own success criteria (e.g. brief drafted, children
   created, build green, audit performed). **Work-item feature ACs** are
   surfaced in the **Use Cases** section (SA-0MTQY7YM5009J6QR), not in
   the `--ac` table. Passing the work item's feature ACs (which are
   unmet until the feature is built) would incorrectly render the report
   as "Not ready to ship" even though the skill session completed
   successfully.
3. **Meta-Data is rendered by the script** from live `wl show --json` —
   the caller supplies only prose (headline, Producer Actions, Notes,
   next action).
   `--producer-actions` carries **blocking, current-stage** actions only;
   omit it (or pass `None needed`) when the next step is just the next
   phase of the workflow.
4. Run it as the **final step**, after all status/stage transitions, so the
   Meta-Data reflects the end-of-session state.
5. Output is stdout markdown; the calling skill **pastes it verbatim into
   its final response** — tool output alone does not put the report in front
   of the operator. (Persisting the report to the work item is a follow-up;
   v1 is stdout only.)
6. The rendered report ends with `</end_session>` on its own line as its final line for terminal sessions; question-ended sessions do not render the report or the marker.

## Pane-title status indicator

When the report is rendered through the CLI, the helper also prepends a
status prefix to the current herdr pane label (the pane title), so an operator
watching a grid of panes can see at a glance how each session ended — without
opening the pane (`SA-0MTFLQEQQ0083EW1`).

The prefix is prepended to the existing title (e.g. `✅ Manually triggered
intake`) and the update is idempotent: re-running replaces the prefix rather
than stacking a second one.

| Status | When | Icon | Text fallback |
|--------|------|------|---------------|
| `done` | All ACs `met` and no substantive producer content | ✅ | `Done` |
| `note` | All ACs `met` but substantive Producer Actions / Notes | ⚠️ | `Note` |
| `attention` | Any AC verdict other than `met` (`unmet`/`partial`/`adjusted`), or an abort/failure | 🚫 | `Attention` |

- **Derivation** is from the supplied AC verdicts, Producer Actions and Notes
  (`adjust`/`partial` count as not fully met → red). Producer actions are
  routed through the shared routine-next-step classifier
  (`shared/herdr_pane.py`), so a routine next step ("Proceed to
  /skill:plan") yields `done`, never `note` — the same classifier the
  renderer uses to normalise the section, so the two can never disagree.
- **Text fallbacks** reuse the renderer's `--no-icons` / `WL_NO_ICONS=1`
  mechanism (no colour coding).
- **Length**: the combined title is bounded to herdr's 60-character limit,
  preserving a trailing ` - <work-item-id>` suffix when truncation is needed.
- **Abort paths** (`intake.py abort`, `implement.py abort`, audit-runner
  non-zero exits) set the red state directly, even when no report is rendered.
- **Fail-open**: outside a herdr pane (no `HERDR_PANE_ID`), when the `herdr`
  CLI is unavailable, or if the rename call fails, the update is skipped
  silently and never breaks the skill run (AC5).

## Renderer

`$(skill_path report)/scripts/render_report.py`:

- `render_report(...)` → full report markdown. Accepts an optional
  ``use_cases`` parameter (list of strings or ``None``); when provided,
  a ``## Use Cases`` section is rendered between the title line and the
  headline.
- `render_metadata(work_item, children=None, audit_result=None)` → the `## Meta-Data` bullet block. `audit_result` is the **top-level** `auditResult` object from the `wl show --json` envelope; when omitted, the legacy `workItem.auditResult` / `workItem.audit` fallbacks apply (see the precedence list above).
- `render_report_from_workitem(work_item, *, skill_name, headline, acceptance_criteria, ..., children=None, audit_result=None, use_cases=None, colour=None)` → full report from an already-fetched work-item dict plus the envelope's structured `auditResult`.
- `normalise_producer_actions(producer_actions)` → collapses routine-only
  producer actions to `None needed` (returning genuine blocking text
  verbatim) and writes **one** stderr diagnostic naming the discarded text.
  It delegates to the single shared classifier
  `shared/herdr_pane.py:normalise_producer_actions`, which the pane
  status derivation consumes too, so both paths agree by construction.
- `render_from_wl(skill_name, work_item_id, ..., use_cases=None, colour=None)` → fetches the `wl show <id> --children --json` envelope and threads its top-level `auditResult` into the metadata.
- `colour_enabled(env=None, stream=None)` → whether ANSI colour may be
  emitted into `stream` (see [Colour capability](#colour-capability-ac3-sa-0mvyez3v3004aqxn)).
- CLI (`render_report.py <id> --skill-name ... --ac ... --use-case ... [--colour|--no-colour]`): fetches the item
  via `wl show <id> --children --json` (worklog store resolved via the
  shared prefix-to-sibling scan, so it works from any cwd incl. git
  worktrees) and prints the report. Use `--use-case` (repeatable) to
  supply use-case strings; `--colour` forces escapes into a non-terminal
  sink and `--no-colour` guarantees plain text (default: auto-detect).

Tests: `./tests/test_render_report.py` (offline — fixture JSON,
no live `wl` calls). Agent test execution must go through the
test skill (`/skill:test`) — the Test execution policy in
AGENTS_GLOBAL.md prohibits direct `pytest` invocations.

## Example rendered report

```markdown
# Completed plan

**Standardize skill session end-of-session reporting via helper report skill** (SA-0MSJ082OY003IQ8S)

## Use Cases

- As a producer reviewing a report, I want to see the work item's objectives
  at a glance alongside the skill's outcome.
- As an agent producing a report, I want a clear, documented way to pass use
  cases into the report renderer so I can include them in my output.

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

✅ Ready to ship
This completes the plan process for SA-0MSJ082OY003IQ8S (Standardize skill session end-of-session reporting via helper report skill).
```

### Non-plan example: audit

The audit skill's AC table records **audit session** deliverables (audit was
performed, verdict determined), not the work item's feature ACs — which are
displayed in the Use Cases section instead.

```markdown
# Completed audit

**Add unit tests for the payment processing module** (SA-0EXAMPLEAUDIT0)

## Use Cases

- As a developer, I want my payment module to be tested for edge cases.
- As a reviewer, I want confidence that the ACs are fully covered.

Performed a comprehensive audit of the work item's implementation against
its acceptance criteria.

## Acceptance Criteria

| AC# | Description | Metric | Verdict |
|---|---|---|---|
| 1 | Audit performed — work item assessed against ACs | audit report generated | met |
| 2 | Audit verdict determined and recorded | verdict stated | met |

## Meta-Data

- Type: feature
- Priority: ⭐ high
- Status: 🔄 in-progress
- Stage: 🔍 in_review
- Risk: ⚠️ Medium
- Effort: 🐇 Small
- Children: 2
- Audit: ✅ passed

## Producer Actions

None needed

## Notes

All feature ACs are covered by tests; implementation follows the design docs.

## Conclusion

✅ Ready to ship
This completes the audit process for SA-0EXAMPLEAUDIT0 (Add unit tests for the payment processing module).
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
