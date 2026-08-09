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
| Skills-section description prose (17 skills) | 3,407 | ~852 | startup, every session |
| **Total** | **24,259** | **~6,065** | startup |

Measured with the canonical tool:

```
Startup static-context budget (bytes / tokens):
  global_agents   :  19518 B /  4880 tok
  project_agents  :   1334 B /   334 tok
  skills_prose    :   3407 B /   852 tok (17 skills)
  total           :  24259 B /  6065 tok
```

> The full on-demand `SKILL.md` contents (172,905 B across 17 files, ~43K
> tokens) are **not** part of the startup surface — they load only when a
> skill is invoked — and are therefore excluded from this budget (they are
> addressed separately by F5, which trims them for on-demand cost).

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
  "global_agents": 19518,
  "project_agents": 1334,
  "skills_prose": 3407,
  "total": 24259
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

## Updating this baseline

After any intentional, reviewed context-surface change (e.g. F2–F5), re-run
the tool, update the table above, and update `context-budget.thresholds.json`
to the new values. The regression gate should always reflect the **accepted**
budget, so accidental regressions fail CI while approved reductions are
pinned in place.
