---
name: interview
description: "Canonical interview-conduct rules shared by intake and plan skills. Not directly invocable by the model."
agent: build
disable-model-invocation: true
---

# Interview Conduct — Shared Contract

This skill defines the shared interview-conduct rules used by **intake** and
**plan**. It is a prose-only skill; agents invoke it by reading this file, not
by running a script. Both consuming skills reference it from their § Interview
sections instead of restating the rules inline.

> **Consuming skills:** `skill/intake/SKILL.md` (§ Interview),
> `skill/plan/SKILL.md` (§ Interview).
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

When the agent cannot proceed without producer input (clarifying questions
unanswered, critical information missing), mark the work item as needing
producer review:

```bash
wl reviewed <work-item-id> true
```

This flags the item so the producer knows attention is required. The agent
should **STOP** and wait for the producer's response. Once answers are
received, continue the interview or proceed.

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
