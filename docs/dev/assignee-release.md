# Assignee release: ownership, session backstop and stale-claim reaper

A non-empty `assignee` is a **scheduling hold** — `wl next` skips a held item,
so an item left on a lingering agent claim is invisible to dispatch. The
canonical statement lives in
[AGENTS_GLOBAL.md](../../AGENTS_GLOBAL.md#assignee-release-invariant) and is
cross-referenced here rather than restated:

> any code path that claims an `assignee` must release it on every terminal
> outcome; a persistent hold is represented by the producer assignee (`Map`) or
> `needsProducerReview`, never by a lingering agent assignee.

This document is the reference for *how* that invariant is enforced across
three layers, and what each layer guarantees (SA-0MW0F91KN004J06U).

## 1. The lifecycle transition (`StatusLifecycle`)

`skill/shared/status_lifecycle.py` releases **any non-producer claim** on
**every** exit path by default:

| Exit path | Release |
|---|---|
| success (`completed`, optional stage) | yes |
| exception / rollback | yes |
| `restore_on_exit=True` (read-only analysis skills) | yes |

Two claims are covered:

- the `assignee=` the context manager set on entry (its own hold);
- an agent claim **observed on entry** that the run did not set itself — the
  workflow pre-claim (`AGENTS_GLOBAL.md` step 1 claims *before* a skill runs).
  Before this widening that hold survived the skill's own terminal transition.

The decision uses the one producer-aware predicate,
`skill/shared/claim_release.py::should_release_assignee`, so the lifecycle and
the backstops below can never disagree about what a protected hold is.

**Never released:** the producer `Map` hold and any item carrying
`needsProducerReview` — the two sanctioned persistent holds.

**Opt-out:** `preserve_assignee_on_exit=True`, for a deliberate producer /
scheduling hold or a caller that restores a pre-existing assignee itself (the
`verify_context_reduction` restore). A call site that passes it must say why in
an inline comment.

### Direct durable writes: `StatusLifecycle.update_status`

Not every skill terminal transition goes through the context manager: a
prose-only skill (`plan`) calls `update_status` directly, `tree_coverage`
advances the AC-coverage stage that way, and `effort-and-risk` restores the
captured status in a `finally` block. Relying on an explicit `assignee=""` at
each of those call sites meant one omission reintroduced the hold — the exact
failure the producer review caught on the `plan` skill
(SA-0MW0F91KN004J06U).

`update_status` is therefore the **single release choke point** as well:

- a transition to any status other than the active-work status
  (`in-progress`) is a terminal outcome of the run, so the item's current
  non-producer claim is released in the same `wl update`;
- the claim is read first (`wl show`); when the item cannot be read the
  transition still goes ahead — the release is a backstop, never a
  precondition;
- `assignee=<value>` writes that value verbatim and disables the automatic
  release (the caller manages the hold);
- `needs_producer_review=True` in the same write makes it a sanctioned
  persistent hold, so the release is suppressed;
- `release_assignee=True/False` forces or forbids the release (the
  `StatusLifecycle` context manager passes `False` on its own exit paths,
  which it has already decided, so `preserve_assignee_on_exit=True` can never
  be second-guessed there).

`StatusLifecycle.update_status(id, "open", stage="plan_complete")` therefore
leaves a plan item unheld even though the skill never entered the context
manager.

### Ownership rule

A terminal transition may clear **any non-producer** assignee, not only the
identity of the run performing it. This is deliberate: the workflow's pre-claim
is made by the agent *before* the skill runs, and a dead session's claim is
indistinguishable from a live one at transition time. Protection against
clobbering a live sibling comes from elsewhere:

- the release fires **only on the transition the run itself performs**;
- the invariant *actively worked ⇒ `status: in_progress`*
  (SA-0MTFTFUIH000UWM9) keeps live work claimed;
- the session backstop (below) releases only the identity it owns;
- the reaper (below) only touches claims idle longer than a TTL.

## 2. Session backstop (`release_session_claim` / `signal_release`)

A session can end without ever reaching a lifecycle exit: an interrupting
`SIGINT`/`SIGTERM`, a timeout, an abort, or a workflow that simply stops. Two
helpers in `skill/shared/claim_release.py` close that window:

- `release_session_claim(work_item_id, …)` — releases **this session's** claim.
  The session identity comes from `IMPLEMENT_AGENT_NAME`,
  `WORKLOG_AGENT_NAME`, `AGENT_NAME` or `PI_AGENT_NAME`; when one is set it is
  the ownership guard, so a finishing run can never clear a concurrently live
  sibling's claim. With no identity configured it releases any non-producer
  claim (the operator-approved ownership rule), always preserving producer
  holds and `needsProducerReview` items.
- `signal_release(work_item_id, …)` — a context manager that installs temporary
  `SIGINT`/`SIGTERM` handlers, releases the claim, and terminates with the
  conventional `128 + signum`. Previous handlers are restored on a normal exit.
  Used by the read-only `refactor` and `find-related` lifecycles, which
  otherwise die without an exit hook.

**Where the backstop runs.** The end-of-session report
(`skill/report/scripts/render_report.py <id>`) — every skill's final step —
invokes `release_session_claim` after rendering, so a session that never
reached a terminal lifecycle step still leaves its item unheld. Opt out with
`--no-release-claim`. The release is best-effort: a failure is reported but
never breaks the report.

**CLI:**

```bash
python3 skill/shared/claim_release.py release <id> [--reason ...] [--json]
python3 skill/shared/claim_release.py reap [--ttl-hours N] [--dry-run] [--json]
```

**Residual window.** `SIGKILL` cannot be caught. A process killed by `SIGKILL`
with no surviving supervisor writes nothing, so its claim survives until the
reaper's TTL expires (§3) or an operator releases it.

## 3. Stale-claim reaper (`reap_stale_claims`)

A TTL-bounded sweep for claims orphaned by a dead session. It runs after a
successful `wl sync` through `skill/shared/worklog_sync.py`, so it needs no
herdr/ContextHub change and reaches every skill that syncs at start-up.

| Property | Behaviour |
|---|---|
| TTL | default **24 h**; `WORKLOG_CLAIM_REAPER_TTL_HOURS` or `reap_stale_claims(ttl_hours=…)` |
| Scope | non-producer claims on **non-terminal** items only |
| Never touched | producer `Map` hold, `needsProducerReview`, `completed`/`done`/`deleted` |
| Age source | most recent `activityAt` / `updatedAt` (any mutation refreshes them); an item with no parseable timestamp is never released |
| Reporting | machine-readable `{"ttl_hours", "scanned", "released_count", "released": [{id, previous_assignee, age_hours}], "skipped": {...}}` plus one comment per release naming the previous assignee |
| Idempotent | a second pass is idempotent — it finds nothing to release |
| Disable | `WORKLOG_CLAIM_REAPER=0`, `sync_worklog(reap_stale_claims=False)` |
| Failure | best-effort — logged and reported as `sync_worklog()["reaper"]`, never downgrades the sync |

Dry-run (`--dry-run`) reports candidates with age and writes nothing.

The reaper releases the **claim** only; it never rewrites `status`/`stage`. A
stale `in-progress` item therefore keeps its status — that downgrade is the
job of `wl sync`'s claim-hold TTL (§ below), which runs against the shared
row. The two mechanisms are complementary and deliberately keep their own
responsibility: the reaper clears an assignee nothing is maintaining any more,
while the sync claim-hold prevents a stale row from *overwriting* a live
claim.

The reaper shares `should_release_assignee` with §1 and §2, so the producer and
`needsProducerReview` exceptions cannot drift between them.

### Why the reaper and `wl sync`'s claim-hold TTL are different

`wl sync`'s `syncClaimHoldTtlHours` (ContextHub, default 24 h — see
[worklog-sync.md](worklog-sync.md#held-claims-survive-background-syncs))
prevents a *stale row overwriting a live claim* during import, and downgrades a
claim older than the TTL. The reaper does the complementary job: it releases a
claim nothing is maintaining any more (and never touches status/stage). The two
share the TTL idea, not the mechanism, and this repository does not require any
ContextHub change for the reaper.

## 4. Historical backlog: what is and is not cleaned

The release rule applies to **outcomes**, not to history. Existing
`completed`/`done` items holding a non-`Map` assignee are left as they are:

- they are **not dispatch-blocking** (`wl next` does not offer terminal
  items), so clearing them would be cosmetic;
- rewriting hundreds of historical rows would obscure the audit trail for no
  scheduling gain.

The one-off cleanup in SA-0MW0F91KN004J06U therefore released only the three
then-current **non-terminal** holds that were blocking dispatch, each read back
through `wl show` to prove the item was dispatchable again.

## 5. Configuration summary

| Variable | Default | Meaning |
|---|---|---|
| `WORKLOG_PRODUCER_ASSIGNEE` | `Map` | the producer identity whose hold is never released |
| `WORKLOG_CLAIM_REAPER_TTL_HOURS` | `24` | reaper idle-hours threshold |
| `WORKLOG_CLAIM_REAPER` | enabled | `0`/`false`/`no`/`off` disables the post-sync hook |
| `IMPLEMENT_AGENT_NAME` / `WORKLOG_AGENT_NAME` / `AGENT_NAME` / `PI_AGENT_NAME` | unset | the session's own identity (session-backstop ownership guard) |

## Tests

- `tests/test_assignee_release_all_skills.py` — the table-driven matrix for
  every skill terminal entry point. Every row seeds an **externally
  pre-claimed** agent assignee (the workflow pre-claim), including the
  prose-only `plan` row and the read-only callers that release through a
  direct `update_status` (`effort-and-risk`, `tree_coverage._advance_stage`).
- `skill/shared/test_status_lifecycle.py` — the widened lifecycle release
  (externally set agent claim, failure/rollback, `restore_on_exit`,
  `needsProducerReview` preservation, producer `Map` preservation) **and** the
  `update_status` choke point (terminal write, claim status, explicit
  assignee, `release_assignee` opt-out, unreadable item).
- `tests/test_effort_and_risk_e2e_status.py` — the direct-write release
  against a real `wl` store (pre-claim → run → empty assignee, status/stage
  preserved).
- `skill/shared/tests/test_claim_release.py` — predicate, session backstop,
  interrupting signal (real store), reaper release/idempotence/preservation.
- `skill/shared/tests/test_worklog_sync.py` — the post-sync reaper hook.
- `skill/report/tests/test_render_report.py` — the end-of-session backstop.
- `tests/test_assignee_release_docs.py` — this documentation and the canonical
  invariant wording.
