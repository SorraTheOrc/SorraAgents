# Interview skill — implementation reference

Deep implementation-reference detail for the shared interview-conduct skill
(`skill/interview/SKILL.md`). This document preserves the full reference for
maintainers; agents consume the SKILL.md directly.

## Consuming skills

Both `skill/intake/SKILL.md` (§ Interview) and `skill/plan/SKILL.md` (§ Interview)
reference this shared skill. Each consuming skill retains its own skill-specific
capture fields:

- **Intake** — seed context, what to capture per feature (user stories, ACs,
  constraints).
- **Plan** — target outcome, definition of done per feature (pass/fail checks),
  constraints (performance, compatibility, rollout), risky assumptions.

## Shared interview rules summary

| Rule | Location |
|------|----------|
| ≤ 3 high-signal questions per round | § 1 |
| Multiple-choice preferred, freeform allowed | § 2 |
| **Recommended:** `<option>` — `<rationale>` | § 3 |
| Producer-review handoff via `wl reviewed` | § 4 |
| Idempotent appendix recording | § 5 |
| No invented requirements | § 6 |
| Respect `.gitignore` | § 7 |

## Producer review handoff

When an interview cannot proceed:

```bash
wl reviewed <work-item-id> true
```

The consuming skill documents the handoff policy:

- **Intake:** Agent **STOP** and wait for the producer's response.
- **Plan:** Handoff-only — release to `open` only when the session genuinely
  ends; if the run will resume in-session, hold `in_progress` or re-claim via
  `StatusLifecycle.ensure_claimed` immediately on resume.
