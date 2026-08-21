# Context Budget — pi session startup static context

This document records the **pre-change baseline** for the pi session startup
static-context surface (measured at F1 implementation time, 2026-08-09), and
documents how to re-measure it. The reduction target is tracked by the epic
[SA-0MSJI53RX006E2PS — Reduce initial LLM context from skills and
skill-related startup files](https://github.com/SorraTheOrc/SorraAgents).

## Measurement tool

Canonical tool: `skill/context-audit/scripts/measure_context.py`.

```bash
# Human-readable table
python3 skill/context-audit/scripts/measure_context.py

# Machine-readable (JSON)
python3 skill/context-audit/scripts/measure_context.py --json

# Regression gate (exits 2 when any threshold is exceeded)
python3 skill/context-audit/scripts/measure_context.py --thresholds docs/dev/context-budget.thresholds.json
```

It reports bytes and a rough token estimate (`chars/4`) per component:

- `global_agents` — `AGENTS_GLOBAL.md` at repo root
- `project_agents` — project `AGENTS.md` at repo root
- `skills_prose` — sum of frontmatter `description` prose across
  `skill/*/SKILL.md` (the skills-discovery section of the session prompt)
- `total` — sum of the above

## Baseline (pre-change, 2026-08-09)

| Component | Bytes | Tokens (est.) | Loaded |
|---|---|---|---|
| Global AGENTS.md (`AGENTS_GLOBAL.md`) | 19,518 | ~4,880 | startup, every session |
| Project AGENTS.md (`AGENTS.md`) | 1,334 | ~334 | startup, this repo |
| Skills-section description prose (17 skills) | 3,523 | ~881 | startup, every session |
| **Total** | **24,375** | **~6,094** | startup |

Measured with the canonical tool:

```
Startup static-context budget (bytes / tokens):
  global_agents   :  19518 B /  4880 tok
  project_agents  :   1334 B /   334 tok
  skills_prose    :   3523 B /   881 tok (17 skills)
  total           :  24375 B /  6094 tok
```

> The full on-demand `SKILL.md` contents (172,905 B across 17 files, ~43K
> tokens) are **not** part of the startup surface — they load only when a
> skill is invoked — and are therefore excluded from this budget (they are
> addressed separately by F5, which trims them for on-demand cost).

> **F2 update (2026-08-09):** after compacting all 17 skill descriptions
> (SA-0MSLK78W7009HIXC), skills prose dropped from 3,523 B to **1,796 B**
> (~49% cut). New measured budget:
>
> ```
>   global_agents   :  19518 B /  4880 tok
>   project_agents  :   1334 B /   334 tok
>   skills_prose    :   1796 B /   449 tok (17 skills)
>   total           :  22648 B /  5662 tok
> ```
>
> Update `context-budget.thresholds.json` to the post-F2 values when F2 is
> accepted (the gate should pin the approved budget, not the pre-change one).

> **F4 update (2026-08-09):** after compacting `AGENTS_GLOBAL.md` to the
> ≤8,192 B budget (SA-0MSLK7LGA003J0KP), the global-file contribution
> dropped from 19,518 B to **8,170 B** (~58% cut). New measured budget:
>
> ```
>   global_agents   :   8170 B /  2042 tok
>   project_agents  :   1334 B /   334 tok
>   skills_prose    :   1796 B /   449 tok (17 skills)
>   total           :  11300 B /  2825 tok
> ```
>
> `context-budget.thresholds.json` now pins `global_agents: 8192` (the F4
> budget) with `total: 13049`.

> **F5 update (2026-08-09):** after trimming the six largest SKILL.md files
> (SA-0MSLK7SAE0032V9K), the **on-demand** skill-load surface dropped
> substantially (implementation-reference detail relocated to
> `docs/dev/*-skill-reference.md`, loaded only on demand):
>
> | Skill | Before (B) | After (B) | Reduction |
> |---|---|---|---|
> | audit | 52,116 | 11,991 | ~77% |
> | plan | 23,602 | 16,589 | ~30% |
> | implement | 21,911 | 15,447 | ~30% |
> | intake | 17,541 | 12,213 | ~30% |
> | ship | 12,306 | 8,672 | ~30% |
> | test | 8,322 | 6,007 | ~28% |
> | **Total** | **135,798** | **70,919** | **~48%** |
>
> The six heaviest skills now load with ≥28–77% less context per invocation,
> with all relocated detail preserved verbatim (or summarized with link + SA
> reference) in `docs/dev/`. Skill-doc tests were updated to check the
> reference docs (F5 AC4). The audit skill's `--no-context-files --no-skills`
> invariant is unchanged (enforced by `skill/audit/tests`).

> **Intake measurement note:** the intake baseline (3,492 B prose) was
> measured with a looser methodology that varied between sessions (3,371 B
> → 3,505 B depending on block-style YAML handling). This tool defines the
> canonical, deterministic measurement; subsequent features (F2–F6) and the
> final verification gate use it exclusively.

## Thresholds / regression gate

`docs/dev/context-budget.thresholds.json` ships an example gate that fails a
PR/CI run when the startup budget regresses above the pre-change baseline:

```json
{
  "global_agents": 8192,
  "project_agents": 1334,
  "skills_prose": 3523,
  "total": 13049
}
```

Use it in CI as:

```bash
python3 skill/context-audit/scripts/measure_context.py \
  --thresholds docs/dev/context-budget.thresholds.json || echo "context budget exceeded"
```

Exit codes: `0` within budget, `2` when a threshold is exceeded, `1` on
usage/measurement errors. `--threshold NAME=BYTES` overrides (repeatable)
and `--thresholds FILE` combine; inline `--threshold` wins for duplicate keys.

> **F6 verification (2026-08-09, SA-0MSLK7XNZ00366YY):** end-to-end
> verification of the context-reduction epic. Before/after measurements vs
> the F1 baseline (24,375 B total):
>
> | Component | F1 baseline (B) | After F2–F5 (B) | Reduction |
> |---|---|---|---|
> | global_agents | 19,518 | 8,144 | ~58% |
> | project_agents | 1,334 | 1,334 | — |
> | skills_prose | 3,523 | 1,796 | ~49% |
> | **total** | **24,375** | **11,274** | **~54%** |
>
> This is a **~54% reduction** in startup static context (~13.1 KB saved per
> session, well above the epic's ≥15% target). On-demand SKILL.md content also
> dropped ~48% (F5: 135,798 → 70,919 B across the six largest skills).
>
> ### Enforcement (committed gate)
>
> The regression gate is enforced in two places:
>
> 1. **Pre-push hook** (`.githooks/pre-push`): runs the gate on every push and
>    fails the push when a threshold is exceeded. Bypass with
>    `CONTEXT_BUDGET_SKIP=1` (not recommended). Fail-open when the tooling is
>    absent (e.g. worktrees of old commits).
> 2. **Full-suite test** (`tests/test_context_budget_gate.py`): asserts the
>    gate passes with the committed thresholds; runs on every test-suite
>    execution, so CI and pre-`in_review` runs fail on regression.
>
> Manual invocation:
>
> ```bash
> python3 skill/context-audit/scripts/measure_context.py \
>   --include-hidden --thresholds docs/dev/context-budget.thresholds.json
> ```
> Exit 0 = within budget, 2 = exceeded.

## Updating this baseline

After any intentional, reviewed context-surface change (e.g. F2–F5), re-run
the tool, update the table above, and update `context-budget.thresholds.json`
to the new values. The regression gate should always reflect the **accepted**
budget, so accidental regressions fail CI while approved reductions are
pinned in place.

## Rollout to all framework projects

The context-budget pre-push gate (F6, SA-0MSLK7XNZ00366YY) is enforced in
SorraAgents and all framework projects: ContextHub, dev-scripts, llm-manager,
open_source_llm, and Tableau-Card-Engine. (ampa was retired 2026-08-20 and is
out of scope.) Each project ships the v2 pre-push hook
(`.githooks/pre-push`) with **worklog sync + context-budget gate + branch
policy**, wired via `core.hooksPath=.githooks` (or equivalent), plus its own
committed per-project thresholds file
(`docs/dev/context-budget.thresholds.json`).

### How the v2 gate works in a multi-project framework

The gate is identical across projects; what differs per project is the
**thresholds file** (each repo has its own `AGENTS.md`, `AGENTS_GLOBAL.md`, and
skill set, so the measured startup surface differs). The hook:

1. resolves `measure_context.py` from the **local** path
   (`skill/context-audit/scripts/measure_context.py`) when present
   (SorraAgents only), otherwise from the **global symlink**
   (`~/.pi/agent/skills/context-audit/scripts/measure_context.py`) installed
   by `install_pi.sh`;
2. reads the project's **committed** thresholds file
   (`docs/dev/context-budget.thresholds.json`);
3. runs `measure_context.py --include-hidden --thresholds <file>` and fails
   the push when any threshold is exceeded.

### Global fallback path for measure_context.py

Only SorraAgents ships `skill/context-audit/` in-repo. All other framework
projects get the skill via `install_pi.sh`, which symlinks the whole skills
tree to `~/.pi/agent/skills/`; the hook therefore falls back to
`~/.pi/agent/skills/context-audit/scripts/measure_context.py` when the local
path is absent. This keeps the gate working in any project whose machine has
run `scripts/install_pi.sh`, and stays **fail-open** when the framework is
entirely absent (old repos, fresh worktrees, machines without pi installed).

### Per-project threshold generation workflow

Each project's thresholds are generated from its own measurement (AC2,
SA-0MT1WO815009KXUC):

```bash
# from the project root, using the installed global skill:
python3 ~/.pi/agent/skills/context-audit/scripts/measure_context.py \
    --include-hidden --generate-thresholds

# or write the file directly (match the project docs layout):
python3 ~/.pi/agent/skills/context-audit/scripts/measure_context.py \
    --include-hidden --write-thresholds docs/dev/context-budget.thresholds.json
```

Commit the generated `docs/dev/context-budget.thresholds.json` with the hook
installation. Re-generate (and update the file) only when the project's
context surface changes intentionally.

### New projects via install_pi.sh

`scripts/install_pi.sh` (v2 hook wiring, SA-0MT1WUEIT006U1HC) automates the
gate for **future** projects. When run inside a git project, it:

1. copies the v2 `pre-push` hook from the source SorraAgents checkout into
   the project's `.githooks/pre-push` (idempotent);
2. sets `core.hooksPath=.githooks` if not already configured;
3. verifies the global context-audit skill symlink path
   (`~/.pi/agent/skills/context-audit/scripts/measure_context.py`);
4. reminds the operator to generate and commit a per-project thresholds
   file if one is missing.

Bypass hook installation with `PI_SKIP_HOOK_INSTALL=1`.

### Troubleshooting

| Symptom | Cause / resolution |
|---|---|
| Gate never runs | `measure_context.py` unavailable locally and globally → hook is fail-open by design (old repos/worktrees/machines without pi). Run `scripts/install_pi.sh` to install the global skill. |
| Push blocked by context budget | Startup surface exceeded committed thresholds. Re-run `measure_context.py --include-hidden --thresholds docs/dev/context-budget.thresholds.json` to see which component regressed; fix the regression, or (only for intentional, reviewed changes) regenerate thresholds per the workflow above. |
| Need to bypass the gate | `CONTEXT_BUDGET_SKIP=1` bypasses the context-budget gate universally (not recommended; never use to ship a regression). |
| Worklog sync not running | `WORKLOG_SKIP_PRE_PUSH=1` disables worklog sync (must not be set globally); `wl`/`worklog` binary missing also skips sync (pushing continues). |
| Branch policy wrongly blocking | `BRANCH_POLICY_SKIP=1` bypasses the protected-branch check (not recommended). |
| Hook not executing | Verify `git config core.hooksPath` returns `.githooks` in the project; ensure `.githooks/pre-push` is executable (`chmod +x`). |
