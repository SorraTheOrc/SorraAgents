---
name: effort_and_risk
description: "Produce engineering effort and risk estimates using WBS, three-point (PERT) estimating, risk matrix, uncertainty, and assumptions. Operates on a provided issue id and its prepared plan."
---

Purpose
-------
Produce a concise, auditable engineering estimate (effort + risk) for a prepared work item. The skill's canonical outputs are:

- A machine-readable JSON object containing effort (hours, tshirt size, O/M/P, expected, recommended, range), risk (probability, impact, score, level, top drivers, mitigations), confidence, assumptions, and unknowns.
- A human-readable summary generated and posted by the orchestrator; the posted content is included in the orchestrator output.

Gating (mandatory)
-------------------

Before doing any work the issue MUST be in the `plan_complete` stage. If it is not, refuse and output ONLY this single sentence (replace <issue-id> with the actual id):

The issue does not have a sufficiently detailed plan, as shown by it not being in the stage of `plan_complete`. Run the planning command with `/plan <issue-id>`

Do not output any other text when refusing.

When to use
-----------
Use this skill only after the Producer has prepared a plan and set the work item's stage to `plan_complete`. The plan should contain in-scope/out-of-scope notes, a lightweight WBS (5–12 items) and any known estimates or constraints.

Required inputs (what you must prepare before running the scripts)
----------------------------------------------------------------
- issue id (string). Fetch the full issue and its children for auditability: wl show <issue-id> --children --json
- A lightweight WBS. Use the issue's child work items as the WBS source (children are returned recursively by wl show --children). If the issue has no children and the scope is small, the parent issue itself can be treated as the WBS.
- Provide Optimistic (O), Most Likely (M), and Pessimistic (P) estimates in hours for the overall work scope. Optionally (for traceability), provide O/M/P per WBS item or per child issue; the scripts will aggregate per-item inputs into the overall estimate when present.
- Explicit additive overheads (hours): coordination, review, testing/integration, risk buffer. These MUST be listed separately (do not hide them inside O/M/P).
- Parent and child risk inputs: for the parent issue and for each child, a Probability (1–5) and Impact (1–5). Include short titles for children to aid triage.
- Certainty % (0–100) representing the assessor's confidence in the provided inputs.
- Clear lists of assumptions and unknowns (each as short strings).

Principles (kept brief)
-----------------------
- Use hours as the canonical unit.
- Use three-point (PERT) estimating for expected value: E = (O + 4*M + P) / 6.
- Surface assumptions and unknowns explicitly so reviewers can decide if further planning (spikes) is needed.
- T-shirt sizing boundaries are defined in references/t-shirt_sizes.json; scripts use that file to pick sizes.

Canonical workflow (minimal, authoritative)
-----------------------------------------
Follow these steps from the skill directory (skill/effort_and_risk):

1) Fetch the issue and its children (audit file):

   wl show <issue-id> --children --json > issue.json

2) Prepare the inputs (JSON) using the plan and WBS. The input should include keys such as:

   {
     "items": [{"id":"CHILD-1","title":"Design","o":2,"m":4,"p":6}, ...],
     "o": <hours>, "m": <hours>, "p": <hours>,
     "overheads": {"coordination": <h>, "review": <h>, "testing": <h>, "risk_buffer": <h>},
     "parent": {"probability": <1-5>, "impact": <1-5>},
     "children": [{"id":"ISSUE-1","probability":2,"impact":1,"title":"child A"}, ...],
     "certainty": 85,
     "assumptions": ["..."],
     "unknowns": ["..."]
   }

3) Run the orchestrator. It enforces gating, computes effort and risk, updates issue metadata, and posts the comment. The script returns a single JSON object that includes:
   - human_text (the content of the posted comment)
   - comment_result (CLI response details)

   python3 scripts/orchestrate_estimate.py <<'JSON' > final.json
   { ... }
   JSON

   Optional helper: run_skill.py can accept stdin JSON to supply per-item estimates and overrides:

   python3 scripts/run_skill.py --issue <issue-id> <<'JSON'
   {"items": [{"id":"CHILD-1","title":"Design","o":2,"m":4,"p":6}, {"id":"CHILD-2","title":"Build","o":6,"m":10,"p":14}]}
   JSON

4) Verify what was posted by inspecting final.json (human_text) or, if needed, the issue itself:

   wl show <issue-id> --format full

Quick helpers (alternate / individual steps)
-------------------------------------------
Helper scripts exist for inspecting intermediate calculations (effort-only or risk-only). Use them when you need to validate inputs or troubleshoot. The orchestrator is the single canonical runner for producing final.json and posting the comment.

What you must keep in the plan before running this skill
--------------------------------------------------------
- A clear WBS derived from child work items (recursive). If there are no children and the scope is small, treat the parent as the WBS item.
- O/M/P inputs traceable to those WBS items (overall totals may be derived from the per-item inputs).
- Known dependencies that may affect risk or schedule.
- Any non-trivial unknowns flagged as items that would cause re-planning.

Outputs
-------
- final.json: canonical machine-readable estimate (as described above), plus orchestration metadata including human_text and comment_result.

References (bundled)
--------------------
- references/t-shirt_sizes.json — T-shirt thresholds used by scripts
- scripts/calc_effort.py
- scripts/calc_risk.py
- scripts/calc_effort_with_risk.py
- scripts/assemble_json.py
- scripts/json_to_human.py
- scripts/orchestrate_estimate.py

Notes and auditability
----------------------
Keep the issue.json (wl show output) and final.json alongside audit comments. The orchestrator and individual scripts are authoritative — the SKILL.md describes how to prepare inputs and when to run them, not how to re-teach the estimation techniques. For full traceability, attach or store the WBS and any calculation inputs with the issue.

If you want a more prescriptive checklist (tests, CI, or making the comment author configurable), open an issue and we can implement that in a follow-up. This file preserves intent while making the scripts the single source of truth for calculations.
