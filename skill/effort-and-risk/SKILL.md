---
name: effort-and-risk
description: "Produce engineering effort and risk estimates using WBS, three-point (PERT) estimating, risk matrix, uncertainty, and assumptions. Operates on a provided issue id and its prepared plan."
---

Purpose
-------

Produce a machine-readable engineering estimate (effort + risk) and human-readable summary for a prepared work item.

**Outputs**: JSON object with effort (units, t-shirt size, O/M/P, expected, recommended, range), risk (probability, impact, score, level, drivers, mitigations), confidence, assumptions, unknowns. Human summary posted by orchestrator.

## Status lifecycle

`run_skill.py` does **not** modify the work item's `status` or `stage`.

The pre-run status is captured and restored deterministically via the shared `StatusLifecycle` helpers (`StatusLifecycle.show` / `StatusLifecycle.update_status`). The `StatusLifecycle` **context manager** is deliberately not used here: its success exit sets `status=completed`, which would violate the documented lifecycle — items at `intake_complete`/`plan_complete` stay `open` until the post-release close.

## Gating

Issue MUST be in `intake_complete` or `plan_complete` stage. If not, refuse with: "The issue does not have a sufficiently detailed plan... Run the intake skill with `/skill:intake <issue-id>` or the plan command with `/skill:plan <issue-id>`." No other output on refusal.

## Orchestrator

`orchestrate_estimate.py` accepts items in `intake_complete` or `plan_complete` stages. Estimates can be applied early and refined later.

## When to use

After Producer sets stage to `intake_complete` or `plan_complete`.

## Required inputs

- Issue ID (fetch with `wl show <id> --json`)
- WBS from child work items (or parent item itself for small scope)
- O/M/P estimates (overall, optionally per-item; scripts aggregate)
- Overheads: coordination, review, testing, risk buffer (listed separately)
- Risk: Probability (1–5) and Impact (1–5) for parent and each child, with short titles
- Certainty % (0–100)
- Assumptions and unknowns (short strings each)

## Principles

- Canonical unit: effort_units
- Estimate: E = (O + 4M + P) / 6 (PERT)
- Surface assumptions and unknowns explicitly
- T-shirt boundaries from `references/t-shirt_sizes.json`

## Workflow (from repo root)

1. Fetch issue: `wl show <issue-id> --json`
2. Prepare JSON input with items, O/M/P, overheads, risk, certainty, assumptions, unknowns
3. Run orchestrator, capture output to `<issue-id>`-based filename:

   ```sh
   python3 skill/effort-and-risk/scripts/run_skill.py --issue <id> <<'JSON' > final-<id>.json
   { "items": [...], "o": ..., "m": ..., "p": ..., "overheads": {...}, "parent": {...}, "children": [...], "certainty": 85, "assumptions": [...], "unknowns": [...] }
   JSON
   ```

   The script gates, computes, updates issue metadata, and posts a comment. Returns JSON with `human_text` and `comment_result`.

4. Verify: `wl show <issue-id> --format full`

## Scripts

- Orchestrator: `skill/effort-and-risk/scripts/orchestrate_estimate.py`
- CLI wrapper: `skill/effort-and-risk/scripts/run_skill.py`
- Calculators: `calc_effort.py`, `calc_risk.py`, `calc_effort_with_risk.py`
- Formatters: `assemble_json.py`, `json_to_human.py`

### Policy

- **Prefer orchestrator script** over ad-hoc commands
- If script missing/fails, request human guidance

### Example

```sh
python3 skill/effort-and-risk/scripts/run_skill.py --issue SA-0MPYMFZXO0004ZU4 <<'JSON' > final-SA-0MPYMFZXO0004ZU4.json
{ ... }
JSON
wl show SA-0MPYMFZXO0004ZU4 --format full
```
