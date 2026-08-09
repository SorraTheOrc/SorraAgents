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

## Worklog resolution

`orchestrate_estimate.py` injects the resolved `--worklog-dir` into every `wl`
call (`wl show`, the effort/risk `wl update`, and `wl comment add`) via the
shared resolution in `../shared/status_lifecycle.py`:

1. **Explicit `--worklog-dir` value** (from a CLI flag / caller)
2. **Prefix-to-sibling scan** — the work-item id prefix (e.g. `OSL`) is matched
   against sibling projects' `config.yaml` so a non-SorraAgents item resolves
   to its own worklog store even when the harness cwd is the framework repo
3. **cwd chain** — `<cwd>/.worklog`, git root, nearest initialized ancestor
4. **No flag** — `wl` resolves from cwd (failures surface real error detail)

The script resolves the correct worklog store regardless of the directory it
is invoked from. See `docs/dev/worklog-sync.md` for the shared resolution
order and `wl sync` failure modes.

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
   python3 ./scripts/run_skill.py --issue <id> <<'JSON' > final-<id>.json
   { "items": [...], "o": ..., "m": ..., "p": ..., "overheads": {...}, "parent": {...}, "children": [...], "certainty": 85, "assumptions": [...], "unknowns": [...] }
   JSON
   ```

   The script gates, computes, updates issue metadata, and posts a comment. Returns JSON with `human_text` and `comment_result`.

4. Verify: `wl show <issue-id> --format full`

## Scripts

- Orchestrator: `./scripts/orchestrate_estimate.py`
- CLI wrapper: `./scripts/run_skill.py`
- Calculators: `calc_effort.py`, `calc_risk.py`, `calc_effort_with_risk.py`
- Formatters: `assemble_json.py`, `json_to_human.py`

### Policy

- **Prefer orchestrator script** over ad-hoc commands
- If script missing/fails, request human guidance

### Example

```sh
python3 ./scripts/run_skill.py --issue SA-0MPYMFZXO0004ZU4 <<'JSON' > final-SA-0MPYMFZXO0004ZU4.json
{ ... }
JSON
wl show SA-0MPYMFZXO0004ZU4 --format full
```
