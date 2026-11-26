---
name: interview
description: "Define the canonical interview-conduct rules shared by intake and plan. Use when conducting an intake or plan interview."
agent: build
disable-model-invocation: true
---

# Interview Conduct — Shared Contract

This skill defines the shared interview-conduct rules used by **intake** and
**plan**. It is a prose-only skill; agents invoke it by reading this file, not
by running a script. Both consuming skills reference it from their § Interview
sections instead of restating the rules inline.

> **Consuming skills:** `$(skill_path intake)/SKILL.md` (§ Interview),
> `$(skill_path plan)/SKILL.md` (§ Interview).
> Each consuming skill retains its own skill-specific capture fields
> (seed context, target outcome, DoD, etc.) alongside the shared-conduct
> reference.

## Rules

### 1. Question volume

Soft limit of **≤ 3 high-signal questions per round** (one round per
interactive pass). Do not ask questions whose answers are already obtainable
by a repo or work-item search.

### 2. Question format

**Multiple-choice preferred; freeform allowed.** Present short option lists
where practical, but accept and record freeform responses without penalty.

### 3. Explicit preferred option (SA-0MUBVLFP70064SPJ)

For every interview question or round, state the agent's **preferred option**
explicitly using the following marker:

```
**Recommended:** <option> — <one-line rationale>
```

The recommendation is **advisory only** — the operator may choose any option,
and the agent proceeds with the operator's choice. The agent never
unilaterally selects or auto-selects the recommendation without confirmation.
This rule applies to every interview round and to questions routed to the
producer via `wl reviewed`.

### 4. Producer-review handoff

Producer review is a **persistent hold**: a flagged item is skipped by the
dispatcher until the producer clears it. Flagging is therefore reserved for a
**genuine handoff** — a decision only the producer can make. Routine questions
the agent can settle itself must never park the item.

Rationale, provenance evidence and the full worked example:
[docs/dev/routine-vs-producer-handoff.md](../../docs/dev/routine-vs-producer-handoff.md).

#### Routine questions vs genuine handoff

| Category | Examples | Agent action |
|----------|----------|--------------|
| **Routine question** | Implementation detail, technical constraint, existing-code pattern, which file to touch, how to test a prose rule | Research the repo or take a documented reasonable assumption, record it in the Appendix (§ 5) and continue |
| **Genuine producer handoff** | Business rule, user-experience or product-strategy choice, budget/priority trade-off — anything only the producer can decide | Call `wl reviewed <work-item-id> true`, then STOP and wait for the producer |

Two questions decide the category:

1. **Can the repo or the work item answer it?** If yes it is routine — search,
   then record the answer and its evidence in the Appendix.
2. **Would a wrong assumption be cheap to reverse?** If yes it is routine —
   assume, record the assumption, continue. If a wrong guess would be expensive
   or outward-facing, it is a genuine handoff.

A routine answer or assumption is recorded in the Appendix (§ 5) as a normal
entry. Marking an entry `OPEN QUESTION` does **not** by itself make it a
handoff: the flag is set only by an explicit `wl reviewed` call for a genuine
decision.

#### Worked example — routine question (no flag)

> Plan needs to know how to test a prose-only rule.
>
> - Q: "How should a prose-only skill rule be tested?" — Answer (agent,
>   research): "A documentation-contract test plus a behavioural test of the
>   flag." Research: inspected the shared test suite; no existing prose tests.
>
> The agent records the entry in the Appendix and continues planning. It does
> **not** call `wl reviewed`; `needsProducerReview` stays `false`.

#### Worked example — genuine handoff (flag and stop)

> Intake reaches a product choice it cannot settle.
>
> - Q: "Should the billing export keep the legacy CSV column order?"
>   **Recommended:** keep it — the least disruptive option.
>
> No repo evidence can settle this, and a wrong guess would change a
> customer-facing contract, so it is a genuine handoff:
>
> ```bash
> wl reviewed <work-item-id> true
> ```
>
> The agent records the question in the Appendix, marks it `OPEN QUESTION`,
> **STOP**s and waits for the producer. Once the answer is received the agent
> continues the interview and releases the hold (`wl reviewed <work-item-id>
> false`).

**Consuming skills:** `plan` and `intake` inherit this rule by reference to
this contract. They must not restate a laxer version that flags routine
questions.

### 5. Appendix recording (idempotent)

Every interview session must produce an auditable Appendix of clarifying
questions asked and answers provided, appended to the final draft (or
description) and to the work item.

Per entry: question text as asked, answer, answering party, and evidence/link
(id, path, PR). Update existing records — **never duplicate** — on re-runs.

Example format:

- Q: "Who is the primary user?" — Answer (user@acme): "Internal support engineers". Source: interactive reply.
- Q: "Can we reuse library Y?" — Answer (eng): "Partially; needs a wrapper." Research: inspected `libs/y`, no adapter found.

Behaviour: append before final approval; **idempotent** (update existing
records, never duplicate); mark open questions "OPEN QUESTION" with context;
respect `.gitignore`. Privacy: record only user/authorized-stakeholder
information; redact secrets with a note ("[REDACTED sensitive snippet]").

### 6. No invented requirements

Do not invent requirements — ask the user. If a response is unclear or
ambiguous, ask for clarification rather than guessing. Do not ask leading or
unnecessary questions when an obvious answer exists.

### 7. Respect ignore rules

Respect `.gitignore` and agent framework ignore rules when searching the repo
during the interview phase.

### 8. Follow the tool-output budget

When reading files or searching the repo, follow the shared
[tool-output budget](../shared/tool-output-budget.md) — `jq` over `cat`, capped
searches, ~10 KB per result.
