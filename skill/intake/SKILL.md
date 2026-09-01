---
name: intake
description: "Create an intake brief via interview-driven requirements gathering. Use when preparing a work item (Workflow step 1)."
agent: build
---

# Intake Skill

## Invocation (canonical)

Resolve the skill directory via the **`skill_path` tool** (never the project
repo, which does not and must not contain copies of skill scripts):

```bash
python3 $(skill_path intake)/scripts/intake.py <work-item-id>
```

`$(skill_path <name>)` is the canonical runtime resolution for skill
locations (the `<name>/` folder under the global install at
`~/.pi/agent/skills`, or under this repo's `skill/` tree) — resolve it first
with the `skill_path` tool, then invoke the script with the **absolute**
path; never run `./scripts/...` relative to the project repo.

---

You are authoring a new Worklog work item for a feature or bug fix, following an interview-driven approach to gather requirements, constraints, Acceptance Criteria (synonym: Success Criteria), and related work — ensuring sufficient detail for a developer to complete the work.

## Inputs

- `$1` — The work-item-id (`<prefix>-<hash>`). If valid, fetch and use it; if missing/invalid, treat `$ARGUMENTS` as seed intent and create a new item as needed; if the user meant an existing item, ask for a valid id.
- `$ARGUMENTS` — Optional freeform arguments after `<work-item-id>`.

## Results and Outputs

- A 1–2 sentence headline summary of the intake brief; final brief text and the new or updated work item.
- Idempotence: rerunning `/skill:intake` reuses existing work items representing the same item.

## Hard requirements

- **Never copy skill scripts between repositories.** The intake scripts
  (and all skills) resolve their shared libraries from the canonical global
  skills location (`~/.pi/agent/skills`, per-skill via `$(skill_path <name>)`)
  or this repo's `skill/` tree. If a script reports a missing shared module
  (see the graceful-failure guard in
  [docs/dev/skills-script-paths.md](../../docs/dev/skills-script-paths.md#graceful-failure-for-missing-shared-modules)
  and `import_guard.py` at the skills root), reinstall via `scripts/install_pi.sh` or
  invoke the skill from its canonical location — do **not** copy/paste script
  files into another project, as real-copy installs without `shared/` break
  import resolution.

- Do not create a work item for this intake process itself.
- Interview style: concise, high-signal questions, max three per round.
- Do not invent requirements — ask the user; don't ask leading or unnecessary questions when an obvious answer exists.
- If a response is unclear or ambiguous, ask for clarification rather than guessing.
- Respect `.gitignore` and agent framework ignore rules; prefer short multiple-choice suggestions but allow freeform.
- All work-item descriptions and comments **must be written in Markdown**.
- The goal is sufficient detail for a clear work item — not an exhaustive spec.
- Do not include procedural next steps (e.g., "Proceed to planning") in the brief; progression is handled by stage transitions.

## Status Lifecycle

All status transitions are managed by the shared `StatusLifecycle` context manager (from `../shared/status_lifecycle.py`) — never ad-hoc `wl update --status` commands. The lifecycle script at `$(skill_path intake)/scripts/intake.py` is the canonical CLI:

- **Claim** (before any other step): `python3 $(skill_path intake)/scripts/intake.py start <work-item-id> --assignee "<AGENT>"` — sets `status=in_progress`, prevents concurrent claims.
- **Auto-complete** (skip full intake for sufficiently defined items): `python3 $(skill_path intake)/scripts/intake.py auto-complete <work-item-id>`.
- **Finish**: `python3 $(skill_path intake)/scripts/intake.py finish <work-item-id> [--description-file <path>]`.
- **Abort** (release on failure): `python3 $(skill_path intake)/scripts/intake.py abort <work-item-id>`.

## Worklog resolution

`intake.py` routes every `wl` call through the shared `run_wl` helper, which injects `--worklog-dir` with precedence: (1) explicit `--worklog-dir`, (2) prefix-to-sibling scan (item-id prefix → sibling projects' worklog stores), (3) cwd chain (`<cwd>/.worklog`, git root, nearest ancestor), (4) no flag — `wl` resolves from cwd. Full detail: [docs/dev/intake-skill-reference.md](../../docs/dev/intake-skill-reference.md) and `docs/dev/worklog-sync.md`.

## Process (must follow)

### 0. Claim the work item

- **Before any other step**, claim the work item:
  ```bash
  python3 $(skill_path intake)/scripts/intake.py start <work-item-id> --assignee Map
  ```
  This must happen before any evaluation, context gathering, or preflight checks.

### 0b. Per-child intake pass (when children exist)

If the work item already has children, run intake on each child before proceeding:

- Run `wl show <work-item-id> --children --json` to fetch existing children.
- Order children by dependency edges using `wl dep list <id> --json` (topological order, ties broken by listed order).
- For each child, run the intake process on it (steps 1–11, recursing if the child has its own children).
- Use the shared tree-coverage helper to verify AC coverage across existing children:
  ```python
  from skill.shared.tree_coverage import run_coverage_review
  review = run_coverage_review(<work-item-id>)
  ```
- If the coverage review returns `recommendation: "stop"` with unresolvable conflicts,
  record the conflicts as a comment and stop — leave the item `open`.
- If the coverage review returns `recommendation: "auto_close"`, apply the auto-closed gaps
  and note them in a comment.
- If the coverage review returns `recommendation: "proceed"`, continue to Step 1.
- Idempotence: re-running must not create duplicate children or duplicate comments.

### 1. Evaluate whether intake is required (agent responsibility)

Run a lightweight evaluation to decide whether the item is well-defined enough to skip the interview/draft. Conservative, idempotent heuristics: `stage` already `intake_complete` or later → skip; description has a clear one-line headline + an "## Acceptance Criteria" section (1–3 measurable bullets) + concise implementation notes (≤~200 words) → well-defined; small item (`task`/`bug`, not `epic`) with explicit ACs + minimal implementation sketch → prefer to complete; parent/child relationships already express the context → consider skipping.

If intake is not needed:

```bash
python3 $(skill_path intake)/scripts/intake.py auto-complete <work-item-id>
wl comment add <work-item-id> "Intake auto-complete: work item appears sufficiently defined (ACs present / small task)." --actor Map --json   # optional
```

If uncertain, fall back to the normal intake process (no auto-complete on borderline evidence).

### 2. Gather context (agent responsibility)

- Derive 2–6 keywords from `<seed-context>` and user input; search work items (`wl search <keywords> --json`) and the repo (ignore `node_modules`, `.git`, `.`-prefixed folders).
- Duplicates: highlight and ask if they represent the work; if confirmed, ask the user to resolve; if parent/child, create the relationship when creating work items.
- Output labelled lists — "Potentially related docs" (paths) and "Potentially related work items" (titles + IDs) — and summarize each.

### 3. Work Item prep (agent responsibility)

- If `<work-item-id>` was provided: review `issueType`; update if it doesn't match: `wl update <work-item-id> --issue-type <correct-type> --json`.
- If no id was provided: extract a working title, infer the issue type (guide below), and create:
  ```bash
  wl create --stage idea --status in_progress --title "<title>" --description "<seed-context>" --issue-type <type> --assignee Map --json
  ```
  (Creation is not a status transition — the initial status is set at creation.) Remember the returned id.

**Issue type decision guide** — full table in [docs/dev/intake-skill-reference.md](../../docs/dev/intake-skill-reference.md). Summary:

- `bug` — currently **incorrect/broken**; the change corrects wrong behavior (not net-new capability).
- `feature` — **adds new capability** that did not exist before (not merely fixing broken).
- `chore` — **does not change code behavior** (config, CI, docs, deps, formatting).
- `task` — general-purpose (tests, refactoring, investigation, benchmarking).
- `epic` — **large scope** needing decomposition into subtasks.

**Decision procedure** — when uncertain, ask: (1) *broken/incorrect?* → `bug`; (2) *net-new behavior?* → `feature`; (3) *no code-behavior change (docs/CI/deps/formatting)?* → `chore`; (4) *general-purpose (tests/refactoring/investigation)?* → `task`; (5) *large enough for subtasks?* → `epic`.

**Priority classification guide** — use these rules to assign the correct `priority` (consulted only when the operator has not supplied an explicit priority value via `--priority` or similar flag):

| Priority  | Use when… | Do NOT use when… | Examples |
|-----------|-----------|-------------------|----------|
| `critical` | Security vulnerabilities, data loss, broken builds, or blocking the release pipeline | The issue is not blocking or does not involve data security/integrity | Patching a CVE, fixing CI pipeline failure, recovering from data corruption |
| `high`    | Major features, important bugs, or anything blocking other work | The work is not urgent or blocking | Shipping a major feature, fixing a significant user-facing bug |
| `medium`  | Default priority for standard features, enhancements, or non-blocking bugs | The issue is critical/high urgency or trivial | Regular feature development, minor bug fixes, improvements |
| `low`     | Polish, minor optimizations, nice-to-have improvements | The issue affects functionality or user experience | Cosmetic fixes, minor refactoring, documentation polish |

**Decision procedure** — when priority is not specified by the operator, ask:
1. *Does this involve security, data loss, or a broken build/release pipeline?* → `critical`
2. *Is this a major feature, important bug, or is it blocking other work?* → `high`
3. *Is this trivial polish or a nice-to-have with no functional impact?* → `low`
4. *Otherwise?* → `medium` (default for standard work)

> **Precedence rule:** Operator-specified priority (via `--priority` flag or explicit directive) always takes absolute precedence over classification. Classification is a fallback only.

### 4. Interview

Skip if the seed context suffices to draft a clear brief. Otherwise: soft limit of 3 questions per round (1+ rounds); do NOT ask questions answerable by repo search — use gathered context; goal is enough understanding to draft a problem definition with user stories, ACs, and related work (not a complete spec); if ambiguous, ask for clarification rather than guessing; do not proceed until sufficient information is gathered.

**Producer review:** When the agent cannot proceed without producer input (clarifying questions unanswered, critical information missing), mark the work item as needing producer review:

```bash
wl reviewed <work-item-id> true
```

This flags the item so the producer knows attention is required. The agent should STOP and wait for the producer's response. Once answers are received, continue the interview or proceed.

### 5. Draft intake brief (agent responsibility)

- Write a brief to `.worklog/tmp/intake-draft-<title>-<work-item-id>.md` with: **Problem statement** (1–2 sentences), **Users** (with example user stories), **Acceptance Criteria** (3–5 measurable bullets), **Constraints**, **Existing state**, **Desired change**, **Key Files (predicted)** (published as a `**Key Files:**` section; e.g. ``- `path/to/file.py` — Needs new function for X feature``), **Related work**.
- Present the draft and invite feedback; incorporate edits but don't block for approval — proceed automatically to the review stages.

### 6. Five mini-review stages (agent responsibility; must follow)

Run five conservative review iterations on the draft brief. If a proposed change could alter intent, ask a clarifying question first. After each stage: "Finished <type> review: <changes>" or "Finished <type> review: no changes needed".

1. **Completeness** — Problem, ACs, Constraints present and actionable; add missing bullets or concise placeholders when obvious.
2. **Capture fidelity** — User answers accurately/neutrally represented; shorten only for clarity.
3. **Related-work & traceability** — Related docs/work items correctly referenced.
4. **Risks & assumptions** — Add missing risks, mitigations, failure modes, assumptions in short bullets; include a scope-creep risk (record extra opportunities as linked work items, not scope).
5. **Polish & handoff** — Tighten language, copy-paste-ready commands, final 1–2 sentence headline.

### 7. Call the find_related skill

Collect related work via `/skill:find-related <work-item-id>`; add a report to the work item description.

### 8. Review the new issue in project context

- Adding dependencies: `wl comment add <work-item-id> --comment "Blocks:<blocked-id>" --json` / `wl comment add <work-item-id> --comment "Blocked-by:<blocking-id>" --json`
- Adjusting priority: `wl update <work-item-id> --priority <level> --json`

### 9. Update the work item and verify coverage

Write the final draft to the work item description:

```bash
python3 $(skill_path intake)/scripts/intake.py finish <work-item-id> --description-file .worklog/tmp/intake-draft-<title>-<work-item-id>.md
```

**AC coverage verification:** After updating the description, run the AC coverage review:

```python
from skill.shared.tree_coverage import run_coverage_review
review = run_coverage_review(<work-item-id>)
```

- If ``recommendation == "proceed"`` or ``"auto_close"`` → mark `intake_complete`.
- If ``recommendation == "stop"`` → **do NOT advance the stage**. Leave the item `open` with a comment describing the conflicts.

Then advance the stage:

This transitions `status=open`, `stage=intake_complete`.

### 10. Calculate Effort and Risk (agent responsibility; must follow)

- Call the effort_and_risk skill on the new or updated work item:
  ```bash
  python3 $(skill_path effort-and-risk)/scripts/orchestrate_estimate.py <work-item-id>
  ```
  (Refer to `../effort-and-risk/SKILL.md` for details.)

### 11. Finishing (must do as the final step only)

- `wl sync` to sync changes.  > **Note:** on a repo with **no commits yet** `wl sync` fails (unborn HEAD) — create an initial commit (`git commit --allow-empty -m "chore: initial"`) or use `wl sync --no-push`. See `docs/dev/worklog-sync.md`.
- `wl show <work-item-id>` (not --json); remove temp files (`.worklog/tmp/intake-draft-<title>-<work-item-id>.md`).
- Output a structured summary (`# Objective` headline, `# Acceptance Criteria` list, `# Effort and Risk` sizing; full template in [docs/dev/intake-skill-reference.md](../../docs/dev/intake-skill-reference.md)). The AC list always includes: at least one testing/validation criterion; "All related documentation is updated to reflect the changes, including code comments, README, and any relevant wiki or docs site entries."; and "Full project test suite must pass with the new changes."  > **Note:** CHANGELOG.md is **excluded** — managed by the ship skill's release pipeline. Do not include CI/CD pipeline tests.
- Finish with "This completes the Intake process for <work-item-id> <work-item-title>"

### 12. Error/abort handling

If the intake process fails or is interrupted before completion:

```bash
python3 $(skill_path intake)/scripts/intake.py abort <work-item-id>
```

This resets `status=open`, releasing the item for other agents.

## Traceability & idempotence

- All work item updates or creations must be idempotent: rerunning `/skill:intake` must not create duplicate links or clarifying-question entries.

## Editing rules & safety

- Preserve author intent; if uncertain, add a clarifying question instead of assuming.
- Keep edits minimal and conservative. Respect `.gitignore` and other ignore rules when searching the repo.
- If any automated step fails or is ambiguous, surface an explicit Open Question and pause for guidance.

## Appendix: Clarifying questions & answers (must include)

Every interview-driven intake must produce an auditable Appendix of clarifying questions asked and answers provided. Append it to the final draft file AND include it in the work item description (`wl update --description-file`).

Per entry: question text as asked; answer, answering party, and evidence/link (id, path, PR); prior answers if changed + final accepted answer; research summary (1–6 sentences) with links when applicable.

Example:

- Q: "Who is the primary user?" — Answer (user@acme): "Internal support engineers". Source: interactive reply.
- Q: "Can we reuse service X?" — Answer (engineer@acme): "Partially; need a small wrapper. Research: inspected services/x, no adapter — created follow-up wl-789".

Behavior: append before final approval; **idempotent** (update existing records, never duplicate); mark open questions "OPEN QUESTION" with context; respect `.gitignore`. Privacy: record only user/authorized-stakeholder info; redact secrets with a note ("[REDACTED sensitive snippet]"). Traceability: each entry linkable; include `related-to:<work-item-id>` or file-path references when practical.


## Final step: standardized end-of-session report

Render the canonical end-of-session report (helper: [`../report/SKILL.md`](../report/SKILL.md)) as the **last step**, replacing any ad-hoc end-of-session summary:

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
