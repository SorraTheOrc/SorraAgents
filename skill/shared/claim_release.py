#!/usr/bin/env python3
# <!-- REFACTOR-SA-0MW0UKUY7002DRJW
# smell: simplification
# severity: medium
# description: Return the condition `not is_producer_assignee(assignee)` directly
# -->
# <!-- REFACTOR-SA-0MW0UKTO1004MP0Y
# smell: formatting
# severity: low
# description: Shebang is present but file is not executable
# -->
"""Release work-item claims at session end and reap stale ones.

A non-empty ``assignee`` on a work item is a **scheduling hold** — ``wl next``
skips a held item — so a claim left behind after a session ends makes finished
work invisible to dispatch. The canonical statement lives in
``AGENTS_GLOBAL.md`` (Assignee release invariant):

> any code path that claims an ``assignee`` must release it on every terminal
> outcome; a persistent hold is represented by the producer assignee (``Map``)
> or ``needsProducerReview``, never by a lingering agent assignee.

``StatusLifecycle`` releases the claim on every lifecycle exit. Two gaps remain
and are closed here (SA-0MW0F91KN004J06U):

1. **Sessions that never reach a terminal lifecycle step.** ``SIGKILL`` is out
   of reach, but an interrupting ``SIGINT``/``SIGTERM``, a timeout and an
   abort/error path can all call :func:`release_session_claim` (or hold the
   :func:`signal_release` guard) so the session's own claim is released even
   when the workflow is abandoned.
2. **Claims orphaned by a dead session.** :func:`reap_stale_claims` scans for
   non-producer claims on non-terminal items older than a TTL and releases
   them, recording each release (machine-readable output plus a comment naming
   the previous assignee). It is idempotent and never touches a producer hold,
   a ``needsProducerReview`` item, or a ``completed``/``deleted`` item.

Producer awareness is one predicate (:func:`should_release_assignee`), shared
by every release path here and by :class:`shared.status_lifecycle.StatusLifecycle`,
so the reaper and the lifecycle transition can never disagree about what a
protected hold is.

Three identities appear in the ``assignee`` field, and only one is a hold:

* ``Map`` (:data:`PRODUCER_ASSIGNEE_DEFAULT`, overridable with
  ``WORKLOG_PRODUCER_ASSIGNEE``) — the producer's deliberate hold: never
  released, never reaped;
* ``dispatcher`` (:data:`DISPATCHER_ASSIGNEE_DEFAULT`) — the herdr downtime
  dispatcher's claim, recorded when it auto-dispatches a work item
  (WL-0MW51OB1Y0073TLG). It means "an agent is on it", **not** "the producer
  parked it", so it is released exactly like an agent claim;
* any other value — an agent identity (``implement``, ``plan``, …): released.

The dispatcher used to claim with ``Map``, which made an auto-dispatched claim
indistinguishable from a deliberate producer hold: sessions preserved it, the
reaper never reaped it, and the item was stranded as a scheduling hold with no
agent working. Keeping both identities single-sourced here is what stops the
two concepts drifting apart again.

Usage::

    from shared.claim_release import release_session_claim, reap_stale_claims

    # End of a skill session (best-effort; never raises):
    release_session_claim(work_item_id, author="implement")

    # Periodic / post-sync sweep:
    report = reap_stale_claims()

CLI::

    python3 skill/shared/claim_release.py release <id> [--reason ...] [--json]
    python3 skill/shared/claim_release.py reap [--ttl-hours N] [--dry-run] [--json]

Configuration (environment):

* ``WORKLOG_PRODUCER_ASSIGNEE`` — the producer identity to preserve
  (default ``Map``).
* ``WORKLOG_CLAIM_REAPER_TTL_HOURS`` — reaper TTL in hours (default ``24``).
* ``WORKLOG_CLAIM_REAPER=0`` — disable the post-sync reaper hook.
* ``IMPLEMENT_AGENT_NAME`` / ``WORKLOG_AGENT_NAME`` / ``AGENT_NAME`` /
  ``PI_AGENT_NAME`` — the session's own agent identity, used by
  :func:`release_session_claim` to avoid clearing a sibling session's claim.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: ``skill/`` — the directory that contains the ``shared`` package. Only used
#: when this file is executed directly as a script (``__package__`` is empty),
#: so importing the module never mutates ``sys.path``.
_SKILLS_ROOT = Path(__file__).resolve().parents[1]

LOG = logging.getLogger("skill.shared.claim_release")

Runner = Callable[[list[str]], subprocess.CompletedProcess]

#: Environment variable naming the producer identity to preserve.
PRODUCER_ASSIGNEE_ENV = "WORKLOG_PRODUCER_ASSIGNEE"
#: The producer assignee (``Map`` per ``AGENTS_GLOBAL.md``).
PRODUCER_ASSIGNEE_DEFAULT = "Map"

#: The herdr downtime dispatcher's claim identity (WL-0MW51OB1Y0073TLG).
#:
#: Deliberately NOT the producer sentinel: ``dispatcher`` is a claim ("an agent
#: is on it"), so :func:`should_release_assignee` releases it and
#: :func:`reap_stale_claims` reaps it like any other agent claim. The
#: TypeScript counterpart is ``DISPATCHER_ASSIGNEE`` in the herdr claim module
#: (ContextHub, ``packages/herdr/src/index.claim.ts``); both sides must name the
#: same value.
DISPATCHER_ASSIGNEE_DEFAULT = "dispatcher"

#: Environment variable overriding the reaper TTL (hours).
REAPER_TTL_ENV = "WORKLOG_CLAIM_REAPER_TTL_HOURS"
#: Default reaper TTL: a claim idle for longer than this is released.
DEFAULT_REAPER_TTL_HOURS = 24.0
#: Environment switch disabling the post-sync reaper hook (``0``/``false``).
REAPER_ENABLED_ENV = "WORKLOG_CLAIM_REAPER"

#: Environment variables carrying the session's own agent identity.
SESSION_AGENT_ENV_VARS = (
    "IMPLEMENT_AGENT_NAME",
    "WORKLOG_AGENT_NAME",
    "AGENT_NAME",
    "PI_AGENT_NAME",
)

#: Statuses that are terminal for reaping: their holds are historical and are
#: never rewritten (they are not dispatch-blocking, and the backlog is out of
#: scope — SA-0MW0F91KN004J06U AC4).
TERMINAL_STATUSES = frozenset({"completed", "done", "deleted", "cancelled"})


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Default command runner using ``subprocess.run``."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _run_wl(
    cmd: list[str],
    runner: Runner | None,
    worklog_dir: str | None,
) -> dict[str, Any]:
    """Run a ``wl`` command through the shared worklog-dir-aware runner."""
    # Imported lazily: ``status_lifecycle`` imports this module at module level,
    # so a top-level import would be circular. The relative import keeps the
    # package identity intact whether ``shared`` is imported as a top-level
    # package (skill dir on ``sys.path``) or as ``skill.shared``; the fallbacks
    # keep the direct-script CLI and the legacy top-level module layout
    # (``skill/shared`` itself on ``sys.path``) working.
    try:
        from .status_lifecycle import run_wl
    except ImportError:  # pragma: no cover - layout fallbacks
        try:
            from shared.status_lifecycle import run_wl  # type: ignore[no-redef]
        except ImportError:
            from status_lifecycle import run_wl  # type: ignore[no-redef]

    return run_wl(cmd, runner=runner or _default_runner, explicit_dir=worklog_dir)


def _normalise_status(status: object) -> str:
    """Return the canonical hyphenated status (``in_progress`` → ``in-progress``)."""
    if not isinstance(status, str):
        return ""
    return status.replace("_", "-").strip().lower()


def producer_assignee() -> str:
    """The producer identity whose hold must never be released."""
    return (os.environ.get(PRODUCER_ASSIGNEE_ENV) or PRODUCER_ASSIGNEE_DEFAULT).strip()


def is_producer_assignee(assignee: object) -> bool:
    """True when *assignee* names the producer (case-insensitive)."""
    if not isinstance(assignee, str):
        return False
    producer = producer_assignee()
    return bool(producer) and assignee.strip().casefold() == producer.casefold()


def is_dispatcher_assignee(assignee: object) -> bool:
    """True when *assignee* names the auto-dispatcher's claim."""
    if not isinstance(assignee, str):
        return False
    return assignee.strip().casefold() == DISPATCHER_ASSIGNEE_DEFAULT.casefold()


def is_deleted(item: dict[str, Any]) -> bool:
    """True when *item* is soft-deleted."""
    return bool(item.get("deletedBy")) or _normalise_status(item.get("status")) == (
        "deleted"
    )


def should_release_assignee(item: dict[str, Any]) -> bool:
    """True when *item*'s assignee is a non-producer claim that should be released.

    The single producer-aware predicate shared by every release path (the
    lifecycle transition, the session backstop and the reaper). Producer holds
    (``Map``) and ``needsProducerReview`` items are sacrosanct — they are the
    sanctioned persistent holds, not a lingering agent claim. The
    auto-dispatcher's ``dispatcher`` claim is NOT one of them: it is a claim
    (try :func:`is_dispatcher_assignee`) and is released like an agent claim.

    Args:
        item: A work item mapping (``wl show --json`` → ``workItem``).

    Returns:
        ``True`` when the assignee is non-empty and releasable.
    """
    assignee = item.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return False
    if bool(item.get("needsProducerReview")):
        return False
    # A ``dispatcher`` claim (the downtime dispatcher's identity) is deliberately
    # released here: it is a claim, not the producer's scheduling hold
    # (WL-0MW51OB1Y0073TLG).
    return not is_producer_assignee(assignee)


def _release_skip_reason(item: dict[str, Any]) -> str:
    """Explain why *item*'s assignee must not be released (empty when it should)."""
    assignee = item.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        return "no-claim"
    if is_deleted(item):
        return "deleted"
    if bool(item.get("needsProducerReview")):
        return "needs-producer-review"
    if is_producer_assignee(assignee):
        return "producer-hold"
    return ""


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a worklog ISO-8601 timestamp (``...Z`` tolerated)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def claim_age_hours(item: dict[str, Any], now: datetime | None = None) -> float | None:
    """Hours since the most recent recorded activity on *item*.

    ``activityAt`` and ``updatedAt`` are both consulted (any mutation refreshes
    them) and the most recent wins, so a genuinely long session that keeps
    mutating its item is never reaped. Returns ``None`` when no parseable
    timestamp exists — such an item is never released blind.
    """
    moment = now or datetime.now(timezone.utc)
    stamps = [
        stamp
        for stamp in (
            _parse_timestamp(item.get("activityAt")),
            _parse_timestamp(item.get("updatedAt")),
        )
        if stamp is not None
    ]
    if not stamps:
        return None
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - newest).total_seconds() / 3600.0)


def release_claim(
    work_item_id: str,
    *,
    reason: str = "session-end",
    author: str | None = None,
    expected_assignee: str | None = None,
    comment: bool = True,
    runner: Runner | None = None,
    worklog_dir: str | None = None,
) -> dict[str, Any]:
    """Release a non-producer claim on *work_item_id* and report the outcome.

    The release is a plain ``wl update <id> --assignee ""``; producer holds and
    ``needsProducerReview`` items are left untouched. With *comment* a worklog
    comment naming the previous assignee records the release, so an automated
    cleanup is always recoverable.

    Args:
        work_item_id: The work item whose claim should be released.
        reason: Short reason recorded in the comment (e.g. ``session-end``).
        author: Comment author. Defaults to the session agent identity or
            ``claim-release``.
        expected_assignee: When given, release only if the persisted assignee
            matches it (case-insensitive). A session backstop passes its own
            identity so it can never clear a *different* live session's claim;
            ``None`` means "any non-producer claim" (reaper / explicit admin).
        comment: Record the release as a worklog comment (default ``True``).
        runner: Optional injectable command runner (for tests).
        worklog_dir: Optional explicit ``.worklog`` directory.

    Returns:
        A machine-readable report — never raises for a missing item or a
        protected hold::

            {"success": true, "work_item_id": ..., "released": bool,
             "reason": "released" | "no-claim" | "producer-hold" |
                       "needs-producer-review" | "claim-not-owned" |
                       "deleted" | "not-found",
             "previous_assignee": str, "status": str, "comment": bool}
    """
    report: dict[str, Any] = {
        "success": True,
        "work_item_id": work_item_id,
        "released": False,
        "reason": "no-claim",
        "previous_assignee": "",
        "status": "",
        "comment": False,
    }
    try:
        data = _run_wl(["wl", "show", work_item_id, "--json"], runner, worklog_dir)
    except RuntimeError as exc:
        report.update(success=False, reason="not-found", error=str(exc))
        return report

    item = data.get("workItem") or {}
    if not item:
        report.update(success=False, reason="not-found")
        return report

    report["previous_assignee"] = str(item.get("assignee") or "")
    report["status"] = str(item.get("status") or "")

    skip = _release_skip_reason(item)
    if skip:
        report["reason"] = skip
        return report

    if expected_assignee is not None:
        persisted = str(item.get("assignee") or "")
        if persisted.strip().casefold() != expected_assignee.strip().casefold():
            report["reason"] = "claim-not-owned"
            return report

    author = author or current_agent_name() or "claim-release"
    try:
        _run_wl(
            ["wl", "update", work_item_id, "--assignee", "", "--json"],
            runner,
            worklog_dir,
        )
    except RuntimeError as exc:
        report.update(success=False, reason="release-failed", error=str(exc))
        return report

    report["released"] = True
    report["reason"] = "released"

    if comment:
        text = (
            f"Assignee released ({reason}): previous assignee "
            f"`{report['previous_assignee']}`. A non-empty assignee is a "
            "scheduling hold; re-claim the item when work resumes."
        )
        try:
            _run_wl(
                [
                    "wl", "comment", "add", work_item_id,
                    "--comment", text,
                    "--author", author,
                    "--json",
                ],
                runner,
                worklog_dir,
            )
            report["comment"] = True
        except RuntimeError as exc:  # the release itself already succeeded
            LOG.warning(
                "Claim released but the release comment failed for %s: %s",
                work_item_id,
                exc,
            )
    return report


def current_agent_name() -> str:
    """The session's own agent identity, or ``""`` when none is configured."""
    for var in SESSION_AGENT_ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return ""


def release_session_claim(
    work_item_id: str,
    *,
    reason: str = "session-end",
    author: str | None = None,
    expected_assignee: str | None = None,
    comment: bool = True,
    runner: Runner | None = None,
    worklog_dir: str | None = None,
) -> dict[str, Any]:
    """Session-end backstop: release this session's claim on *work_item_id*.

    The session's agent identity (see :data:`SESSION_AGENT_ENV_VARS`) is used
    as the ownership guard, so a finishing run never clears a concurrently
    live sibling session's claim. When no identity is configured the helper
    still releases any non-producer claim — the operator-approved rule for a
    terminal outcome — while producer holds and ``needsProducerReview`` items
    stay untouched.

    Best-effort by contract: callers are session-end hooks and abort paths, so
    a failure is logged and reported rather than raised.
    """
    owner = current_agent_name()
    if expected_assignee is None:
        expected_assignee = owner or None
    try:
        return release_claim(
            work_item_id,
            reason=reason,
            author=author or owner or None,
            expected_assignee=expected_assignee,
            comment=comment,
            runner=runner,
            worklog_dir=worklog_dir,
        )
    except Exception as exc:  # noqa: BLE001 - a backstop never breaks a session
        LOG.warning("Session-end claim release failed for %s: %s", work_item_id, exc)
        return {
            "success": False,
            "work_item_id": work_item_id,
            "released": False,
            "reason": "error",
            "error": str(exc),
        }


@contextlib.contextmanager
def signal_release(
    work_item_id: str,
    *,
    reason: str = "signal",
    author: str | None = None,
    expected_assignee: str | None = None,
    signs: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
    runner: Runner | None = None,
    worklog_dir: str | None = None,
) -> Iterator[None]:
    """Release the claim if the session is interrupted by a signal.

    Installs temporary ``SIGINT``/``SIGTERM`` handlers that release the claim
    (through :func:`release_session_claim`) and then terminate the process with
    the conventional exit code ``128 + signum``. Previous handlers are restored
    on a normal exit, so the guard composes with a caller's own handlers.

    A ``SIGKILL`` cannot be caught; those orphans are covered by
    :func:`reap_stale_claims` after the TTL.
    """
    previous: dict[int, Any] = {}

    def _handler(signum: int, _frame: object) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - defensive
            name = str(signum)
        try:
            release_session_claim(
                work_item_id,
                reason=f"{reason}:{name}",
                author=author,
                expected_assignee=expected_assignee,
                runner=runner,
                worklog_dir=worklog_dir,
            )
        except Exception as exc:  # noqa: BLE001 - never block the exit
            LOG.warning("Signal claim release failed for %s: %s", work_item_id, exc)
        finally:
            os._exit(128 + int(signum))

    for signum in signs:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handler)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def reaper_enabled() -> bool:
    """Whether the post-sync reaper hook is enabled (default ``True``)."""
    value = (os.environ.get(REAPER_ENABLED_ENV) or "").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def resolve_ttl_hours(ttl_hours: float | None = None) -> float:
    """Resolve the reaper TTL (explicit arg > environment > default)."""
    if ttl_hours is not None:
        return float(ttl_hours)
    raw = (os.environ.get(REAPER_TTL_ENV) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            LOG.warning(
                "Ignoring invalid %s=%r; using %s h",
                REAPER_TTL_ENV,
                raw,
                DEFAULT_REAPER_TTL_HOURS,
            )
    return DEFAULT_REAPER_TTL_HOURS


def reap_stale_claims(
    *,
    ttl_hours: float | None = None,
    dry_run: bool = False,
    author: str | None = None,
    runner: Runner | None = None,
    worklog_dir: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release non-producer claims older than the TTL on non-terminal items.

    Idempotent: a second pass finds nothing to release. Producer holds,
    ``needsProducerReview`` items, ``completed``/``done``/``deleted`` items and
    items without a parseable timestamp are never touched. Each release is
    reported (``id``, ``previous_assignee``, ``age_hours``) and commented on
    the item, so an automated cleanup is auditable and recoverable.

    Args:
        ttl_hours: Idle-hours threshold; defaults to
            :data:`DEFAULT_REAPER_TTL_HOURS` / ``WORKLOG_CLAIM_REAPER_TTL_HOURS``.
        dry_run: Report candidates without writing anything.
        author: Comment author for the release comments.
        runner: Optional injectable command runner (for tests).
        worklog_dir: Optional explicit ``.worklog`` directory.
        now: Injectable clock (for deterministic tests).

    Returns:
        ``{"success", "ttl_hours", "dry_run", "scanned",
        "released_count", "released": [...], "skipped": {reason: count}}``.
    """
    ttl = resolve_ttl_hours(ttl_hours)
    report: dict[str, Any] = {
        "success": True,
        "ttl_hours": ttl,
        "dry_run": dry_run,
        "scanned": 0,
        "released_count": 0,
        "released": [],
        "skipped": {},
    }
    try:
        listing = _run_wl(["wl", "list", "--json"], runner, worklog_dir)
    except RuntimeError as exc:
        report.update(success=False, error=str(exc))
        return report

    items = listing.get("workItems") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        items = []

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        report["scanned"] += 1

        if not str(item.get("assignee") or "").strip():
            skip("no-claim")
            continue
        reason = _release_skip_reason(item)
        if reason:
            skip(reason)
            continue
        if _normalise_status(item.get("status")) in TERMINAL_STATUSES:
            skip("terminal")
            continue
        age = claim_age_hours(item, now=now)
        if age is None:
            skip("unknown-age")
            continue
        if age <= ttl:
            skip("fresh")
            continue

        if dry_run:
            report["released"].append(
                {
                    "id": item_id,
                    "previous_assignee": str(item.get("assignee") or ""),
                    "age_hours": round(age, 2),
                    "released": False,
                }
            )
            report["released_count"] += 1
            continue

        outcome = release_claim(
            item_id,
            reason=f"stale-claim-reaper (ttl {ttl:g}h)",
            author=author or "claim-reaper",
            runner=runner,
            worklog_dir=worklog_dir,
        )
        if outcome.get("released"):
            report["released"].append(
                {
                    "id": item_id,
                    "previous_assignee": outcome.get("previous_assignee", ""),
                    "age_hours": round(age, 2),
                    "released": True,
                }
            )
            report["released_count"] += 1
        else:
            skip(str(outcome.get("reason") or "not-released"))

    report["skipped"] = skipped
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Release work-item claims: a session-end backstop, an interrupting "
            "signal guard, or a TTL-bounded stale-claim reaper."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    release = sub.add_parser("release", help="Release one work item's claim.")
    release.add_argument("work_item_id", help="Work item id to release.")
    release.add_argument(
        "--reason", default="session-end", help="Reason recorded in the comment."
    )
    release.add_argument("--author", default=None, help="Comment author.")
    release.add_argument(
        "--expected-assignee",
        default=None,
        help="Release only when the persisted assignee matches this value.",
    )
    release.add_argument(
        "--no-comment", action="store_true", help="Do not add a release comment."
    )
    release.add_argument("--worklog-dir", default=None, help="Explicit .worklog dir.")
    release.add_argument("--json", action="store_true", help="Machine-readable output.")

    reap = sub.add_parser("reap", help="Release stale claims older than the TTL.")
    reap.add_argument(
        "--ttl-hours",
        type=float,
        default=None,
        help=(
            "Idle-hours threshold "
            f"(default {DEFAULT_REAPER_TTL_HOURS:g}, "
            f"override with {REAPER_TTL_ENV})."
        ),
    )
    reap.add_argument(
        "--dry-run", action="store_true", help="Report without writing anything."
    )
    reap.add_argument("--author", default=None, help="Comment author.")
    reap.add_argument("--worklog-dir", default=None, help="Explicit .worklog dir.")
    reap.add_argument("--json", action="store_true", help="Machine-readable output.")
    return parser


def _print_report(payload: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if "released_count" in payload:
        print(
            f"Reaper: scanned {payload['scanned']} item(s), released "
            f"{payload['released_count']} claim(s) older than "
            f"{payload['ttl_hours']:g}h"
            + (" (dry run)" if payload.get("dry_run") else "")
        )
        for entry in payload.get("released", []):
            print(
                f"  - {entry['id']}: released {entry['previous_assignee']!r} "
                f"(idle {entry['age_hours']}h)"
            )
        for reason, count in sorted(payload.get("skipped", {}).items()):
            print(f"  - skipped {count} ({reason})")
        return
    item_id = payload.get("work_item_id")
    if payload.get("released"):
        print(
            f"{item_id}: released {payload['previous_assignee']!r} "
            f"({payload['reason']})"
        )
    else:
        print(f"{item_id}: nothing released ({payload.get('reason')})")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)

    if args.command == "release":
        payload = release_claim(
            args.work_item_id,
            reason=args.reason,
            author=args.author,
            expected_assignee=args.expected_assignee,
            comment=not args.no_comment,
            worklog_dir=args.worklog_dir,
        )
        _print_report(payload, args.json)
        return 0 if payload.get("success") else 1

    payload = reap_stale_claims(
        ttl_hours=args.ttl_hours,
        dry_run=args.dry_run,
        author=args.author,
        worklog_dir=args.worklog_dir,
    )
    _print_report(payload, args.json)
    return 0 if payload.get("success") else 1


if __name__ == "__main__":
    # Direct-script execution: make the ``shared`` package importable.
    if str(_SKILLS_ROOT) not in sys.path:
        sys.path.insert(0, str(_SKILLS_ROOT))
    sys.exit(main())
