# Routine questions vs genuine producer handoff

Why the `intake` / `plan` / `interview` skills reserve `needsProducerReview`
for genuine producer decisions, and how to tell the two cases apart.

> **Contract:** the normative rule lives in
> [`skill/interview/SKILL.md` § 4](../../skill/interview/SKILL.md#4-producer-review-handoff).
> Consuming skills (`plan`, `intake`) reference it rather than restating a
> laxer version. This page carries the rationale and the worked examples.

## Why the distinction matters

`needsProducerReview` is a **persistent hold**, not a notification. The
downtime dispatcher skips a held item, so setting the flag removes the item
from the queue until the producer clears it (see
[assignee-release.md](./assignee-release.md) for the companion claim/hold
model and [downtime-idle-gate-evaluation.md](./downtime-idle-gate-evaluation.md)
for the dispatch ranking).

The prep stages (`intake`, `plan`) are the dispatcher's fallback when the
implement and audit budgets are exhausted. They are deliberately uncapped, so
supply there is what keeps the host busy overnight. Before this rule, a prep
skill that reached *any* unanswered clarifying question marked the item and
stopped — including questions the agent could have answered itself.

## Evidence

The AC2 provenance audit for the parent investigation
(`WL-0MWTAD1KM0081UUD`, comment `WL-C0MWTTIOEN000M9LN`) traced the provenance
of every flagged prep-stage row across the active roots:

| Measure | Value |
|---------|-------|
| Flagged prep rows traced | 18 |
| Sourced `question-loop` (a clarifying question set the hold) | 16 |
| Total prep rows examined | 416 |
| Producer review as an overall contributor | 18 / 416 = 4.3 % |

So the mechanism is a real but minority cause of the empty dispatch budget —
but it is the one with the highest recurrence, and almost all of it was
routine-question driven rather than genuine producer handoff. Representative
rows:

- `AH-0MWEUSVJ1007EXJY` — intake posted Q1–Q3 and marked them `OPEN QUESTION`,
  which set the hold.
- `WL-0MWAJ3RPX000HQIG` — intake recorded its own recommendation, then set the
  handoff anyway.
- `AH-0MWFDQ9EY004NO4X` — "remains open for the producer", with no decision
  that actually required the producer.

Root cause: the skills said *"when clarifying questions are unanswered, mark
the item"* without distinguishing a question the agent can research from a
decision only the producer can make. This change fixes the rule; it does not
touch the dispatcher or any gate.

## The rule

| Category | Examples | Agent action |
|----------|----------|--------------|
| **Routine question** | Implementation strategy, existing-code pattern, which file to touch, how to test a prose rule, tooling choice | Research the repo or take a documented reasonable assumption, record it in the Appendix, continue |
| **Genuine producer handoff** | Business rule, user-experience or product-strategy choice, budget/priority trade-off, anything outward-facing or expensive to reverse | `wl reviewed <work-item-id> true`, record the question, STOP and wait |

Two tests decide the category:

1. **Can the repo or the work item answer it?** Yes ⇒ routine. Search, record
   the answer and its evidence in the Appendix.
2. **Would a wrong assumption be cheap to reverse?** Yes ⇒ routine. Assume,
   record the assumption, continue. If a wrong guess would be expensive or
   outward-facing ⇒ genuine handoff.

Marking an Appendix entry `OPEN QUESTION` is **not** a handoff. Only an
explicit `wl reviewed <work-item-id> true` for a genuine decision sets the
hold, and `wl reviewed <work-item-id> false` releases it once the answer is
recorded.

## Worked example (a) — routine question, no flag

> **Situation.** The plan skill needs to know how to test a prose-only rule.
>
> **Question.** "How should a prose-only skill rule be tested?"
>
> **Resolution (agent, research).** "A documentation-contract test plus a
> behavioural test of the flag." Research: inspected `skill/shared/tests/`;
> no existing prose tests.
>
> **Recorded in the Appendix.**
>
> - Q: "How should a prose-only skill rule be tested?" — Answer (agent,
>   research): "Documentation-contract assertion plus a behavioural flag
>   test." Source: `skill/shared/tests/` inspection.
>
> **Outcome.** The agent continues planning. No `wl reviewed` call is made and
> `needsProducerReview` stays `false` — the item remains dispatchable.

## Worked example (b) — genuine handoff, flag and stop

> **Situation.** Intake reaches a product choice it cannot settle.
>
> **Question.** "Should the billing export keep the legacy CSV column order?"
> **Recommended:** keep it — the least disruptive option.
>
> **Why it is genuine.** No repo evidence settles it, and a wrong guess changes
> a customer-facing contract — expensive to reverse.
>
> **Action.**
>
> ```bash
> wl reviewed <work-item-id> true
> ```
>
> The agent records the question in the Appendix, marks it `OPEN QUESTION`,
> **STOP**s and waits for the producer. When the answer arrives the agent
> continues the interview, records the answer, and releases the hold with
> `wl reviewed <work-item-id> false`.

## Scope

Skill prose only. No dispatcher change, no gate change, no worklog schema
change. The flag's behaviour (`wl reviewed`) is unchanged; only the criteria
for calling it are.

## Related

- [interview-skill-reference.md](./interview-skill-reference.md) — the shared
  interview contract's implementation reference.
- [assignee-release.md](./assignee-release.md) — claims and the persistent
  `needsProducerReview` hold.
- [downtime-idle-gate-evaluation.md](./downtime-idle-gate-evaluation.md) —
  how held items are skipped at dispatch.
- Work items: `SA-0MWUNYMLS003B52S` (this change),
  `WL-0MWTVPL1A005KB4D` (coordination),
  `WL-0MWTAD1KM0081UUD` (parent investigation, scope item 4c).
