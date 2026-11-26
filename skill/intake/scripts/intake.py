#!/usr/bin/env python3
"""Status lifecycle script for the intake skill.

Manages work-item status transitions for the intake process using the shared
StatusLifecycle context manager. The SKILL.md instructions reference this
script instead of using ad-hoc ``wl update --status`` commands.

Usage::

    python3 skill/intake/scripts/intake.py start <work-item-id> [--assignee <name>]
    python3 skill/intake/scripts/intake.py finish <work-item-id> [--description-file <path>]
    python3 skill/intake/scripts/intake.py auto-complete <work-item-id>
    python3 skill/intake/scripts/intake.py autoplan <work-item-id>
    python3 skill/intake/scripts/intake.py abort <work-item-id>

Exit codes:
    0 — success
    1 — error during execution

All commands produce JSON output for agent consumption.

Autoplan pre-check (``autoplan``): the closing step that runs *after* the
effort/risk estimate and *before* the finishing step. It consumes the
canonical decision logic from ``skill/plan/plan_helpers.py`` (no duplicated
threshold constants) and applies the fail-safe rules:

* decision ``skip`` → advance the stage to ``plan_complete`` with status
  ``open`` and the assignee released (no separate ``/plan`` session needed);
* decision ``plan``/``error``/unknown estimate → leave the stage untouched so
  planning still runs (never skip silently);
* the item is already at ``plan_complete`` or later → no-op (never regress).

Assignee release: ``start`` sets the assignee (a claim/hold marker); every
terminal command (``finish``, ``auto-complete``, ``abort``), the ``autoplan``
skip transition and every failure reset clears it, so a completed or released
item is never left on a scheduling hold (non-empty assignee = hold; clear to
release).

Every release here is an explicit ``assignee=""`` and must stay explicit:
these are direct ``StatusLifecycle.update_status`` durable writes, not
``StatusLifecycle`` context-manager exits, so the helper's
release-by-default (which applies on ``__exit__``) does not cover them.
Dropping the keyword would silently reintroduce the hold for any item
claimed with ``--assignee`` (pinned by
``tests/test_assignee_release_all_skills.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

# Ensure the skills root is on sys.path so top-level skill packages import work
_SKILLS_ROOT = Path(__file__).resolve().parents[2]  # <repo>/skill (or ~/.pi/agent/skills)
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from import_guard import guard_shared_import

try:
    from shared.status_lifecycle import StatusLifecycle, run_wl
    from shared.timing import Timer
except ModuleNotFoundError as _missing_shared:
    guard_shared_import(_missing_shared.name)

# Canonical autoplan decision logic, single-sourced with the plan skill
# (SA-0MWXJWZ3T0062HBI): the thresholds
# (DEFAULT_AUTOPLAN_EFFORT_SKIP / DEFAULT_AUTOPLAN_RISK_SKIP), the
# unknown→plan fallback and the idempotence check must not be duplicated here.
#
# The module is loaded **by file path** rather than by ``import plan...``:
# the repository root ships its own ``plan/`` package (detection.py /
# wl_adapter.py), so which ``plan`` wins depends on ``sys.path`` ordering —
# the full test suite deliberately puts the repo root first, while the
# changed-scope gate puts the skills root first. A path load is deterministic
# in both layouts (repo ``skill/plan`` tree and an installed skills tree) and
# still executes the plan skill's own module — no logic is copied. If the load
# fails the pre-check degrades to ``error`` (fail-safe: planning still runs)
# rather than breaking intake's other commands.
_PLAN_HELPER_MODULE_NAME = "intake_canonical_plan_helpers"


class _PlanHelperError(RuntimeError):
    """Raised when the canonical autoplan helper module cannot be loaded."""


def _load_plan_helpers_module() -> ModuleType:
    """Load ``skill/plan/plan_helpers.py`` (the canonical autoplan module).

    Returns:
        The executed module, cached in ``sys.modules`` so repeated calls share
        one instance.

    Raises:
        _PlanHelperError: If the module file is missing or cannot be executed.
    """
    cached = sys.modules.get(_PLAN_HELPER_MODULE_NAME)
    if cached is not None:
        return cached

    path = _SKILLS_ROOT / "plan" / "plan_helpers.py"
    spec = importlib.util.spec_from_file_location(_PLAN_HELPER_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise _PlanHelperError(f"cannot load the canonical autoplan helper from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLAN_HELPER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(_PLAN_HELPER_MODULE_NAME, None)
        raise _PlanHelperError(
            f"cannot execute the canonical autoplan helper {path}: {exc}"
        ) from exc
    return module


LOG = logging.getLogger("intake.scripts.intake")

try:
    _PLAN_HELPERS: ModuleType | None = _load_plan_helpers_module()
    plan_if_needed = _PLAN_HELPERS.plan_if_needed
except Exception as _plan_helper_error:  # noqa: BLE001 -- fail-safe by design
    LOG.error("intake.autoplan.helper_unavailable: %s", _plan_helper_error)
    _PLAN_HELPERS = None
    plan_if_needed = None

# Stage progression used by the closing autoplan pre-check. An item already at
# ``plan_complete`` or later must never be regressed (idempotence), so stages
# are compared by rank rather than by equality.
_STAGE_SEQUENCE: tuple[str, ...] = (
    "idea",
    "intake_complete",
    "plan_complete",
    "in_review",
    "done",
)

# Author recorded on the autoplan decision comment (distinct from the plan
# skill's "auto-plan" author so the two decision trails stay separable).
_AUTOPLAN_AUTHOR = "intake-autoplan"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_start(item_id: str, assignee: str | None = "Intake") -> dict:
    """Claim the work item by setting status to in_progress.

    Uses StatusLifecycle.update_status to set the status without the
    overhead of a full context manager (the context manager is more
    appropriate when wrapping a block of code).

    Args:
        item_id: The work item ID.
        assignee: Optional assignee name.

    Returns:
        A dict with action and item_id keys.
    """
    kwargs: dict = {"status": "in_progress"}
    if assignee:
        kwargs["assignee"] = assignee
    StatusLifecycle.update_status(item_id, **kwargs)
    LOG.info("Intake started for %s (assignee=%s)", item_id, assignee)
    return {"success": True, "action": "started", "item_id": item_id}


def cmd_finish(item_id: str, description_file: str | None = None) -> dict:
    """Complete the intake process: set status to open, stage to intake_complete.

    Optionally applies a description file before the status update. The
    terminal transition also clears the assignee (assignee-hold convention:
    non-empty assignee = scheduling hold; clear to release) so a finished
    item is never left held.

    Args:
        item_id: The work item ID.
        description_file: Optional path to a markdown description file to apply
            via ``wl update --description-file`` before the status transition.

    Returns:
        A dict with action and item_id keys.
    """
    if description_file:
        try:
            _run_wl_update_description(item_id, description_file)
        except RuntimeError:
            # The item was claimed (in_progress) by `start`; reset to open so
            # it is never left stuck in in_progress on failure, and release
            # the assignee hold. The explicit ``assignee=""`` is required:
            # this is a direct durable write, not a context-manager exit, so
            # the helper's release-by-default does not run here.
            try:
                StatusLifecycle.update_status(item_id, "open", assignee="")
            except RuntimeError:
                LOG.error("Failed to reset work item %s status to open", item_id)
            raise

    # Direct durable write (not a StatusLifecycle exit): the assignee must be
    # released explicitly, because the helper's release-by-default only runs
    # on context-manager exit (assignee-hold convention).
    StatusLifecycle.update_status(
        item_id, "open", stage="intake_complete", assignee=""
    )
    LOG.info("Intake finished for %s", item_id)
    return {"success": True, "action": "finished", "item_id": item_id}


def cmd_auto_complete(item_id: str) -> dict:
    """Auto-complete a well-defined work item without a full intake interview.

    Transitions: in_progress → open with stage=intake_complete. The terminal
    transition clears the assignee so the item is released for the next
    lifecycle step (assignee-hold convention).

    Args:
        item_id: The work item ID.

    Returns:
        A dict with action and item_id keys.
    """
    StatusLifecycle.update_status(item_id, "in_progress")
    try:
        # Direct durable write: see the module docstring — the explicit
        # ``assignee=""`` cannot be dropped as redundant.
        StatusLifecycle.update_status(
            item_id, "open", stage="intake_complete", assignee=""
        )
    except RuntimeError:
        # Reset back to open so the item is never left stuck in in_progress
        # (and release the assignee hold; direct durable write again).
        try:
            StatusLifecycle.update_status(item_id, "open", assignee="")
        except RuntimeError:
            LOG.error("Failed to reset work item %s status to open", item_id)
        raise
    LOG.info("Intake auto-completed for %s", item_id)
    return {"success": True, "action": "auto_completed", "item_id": item_id}


def cmd_autoplan(item_id: str) -> dict:
    """Apply the canonical autoplan pre-check as intake's closing step.

    Runs *after* the effort/risk estimate and *before* the finishing step. The
    decision comes from the plan skill's canonical helper
    (:func:`plan.plan_helpers.plan_if_needed`) so the thresholds and the
    unknown→plan fallback are single-sourced (AC5).

    Behaviour (fail-safe by design):

    * ``skip`` → advance ``stage=plan_complete`` with ``status=open`` and the
      assignee released, and record the decision as a comment (AC2);
    * ``plan`` / ``error`` / unknown effort-risk → leave the stage untouched so
      planning still runs (AC3);
    * the item is already at ``plan_complete`` or later → no-op, never regress
      and never call the helper again (AC4).

    Args:
        item_id: The work item ID.

    Returns:
        A JSON-serializable dict describing the decision and the resulting
        stage.
    """
    stage = _fetch_stage(item_id)
    if stage is None:
        # Fail safe: an unreadable item is never mutated; planning still runs.
        return {
            "success": True,
            "action": "unchanged",
            "item_id": item_id,
            "decision": "error",
            "effort": "",
            "risk": "",
            "stage": "",
            "reason": f"could not fetch work item {item_id}",
        }

    rank = _stage_rank(stage)
    if rank is None:
        # Unrecognised stage: never guess and never write (fail safe).
        return {
            "success": True,
            "action": "no_op",
            "item_id": item_id,
            "decision": "error",
            "effort": "",
            "risk": "",
            "stage": stage,
            "reason": f"unrecognised stage {stage!r}",
        }

    if rank >= _stage_rank("plan_complete"):
        # Idempotence: already planned (or beyond) — leave everything untouched.
        return {
            "success": True,
            "action": "no_op",
            "item_id": item_id,
            "decision": "not_required",
            "effort": "",
            "risk": "",
            "stage": stage,
            "reason": "stage already at or past plan_complete",
        }

    decision = _autoplan_decision(item_id)
    outcome = decision.get("decision", "error")
    effort = decision.get("effort", "") or ""
    risk = decision.get("risk", "") or ""

    if outcome != "skip":
        # Fail safe (AC3): ``plan``/``error``/unknown keeps the item at
        # ``intake_complete`` so a planning session still runs.
        _append_autoplan_comment(item_id, outcome, effort, risk)
        return {
            "success": True,
            "action": "unchanged",
            "item_id": item_id,
            "decision": outcome,
            "effort": effort,
            "risk": risk,
            "stage": stage,
            "reason": "planning required (fail-safe)",
        }

    # Decision ``skip``: planning is not required, so advance past it.
    # Direct durable write (not a StatusLifecycle exit): the explicit
    # ``assignee=""`` releases the claim per the assignee-release invariant.
    StatusLifecycle.update_status(
        item_id, "open", stage="plan_complete", assignee=""
    )
    _append_autoplan_comment(item_id, outcome, effort, risk)
    LOG.info(
        "Intake autoplan pre-check advanced %s to plan_complete (effort=%s risk=%s)",
        item_id, effort, risk,
    )
    return {
        "success": True,
        "action": "advanced",
        "item_id": item_id,
        "decision": outcome,
        "effort": effort,
        "risk": risk,
        "stage": "plan_complete",
        "reason": "effort and risk below the planning threshold",
    }


def cmd_abort(item_id: str) -> dict:
    """Reset work item status to open on abort/interrupt.

    Also clears the assignee so an aborted item is never left on a
    scheduling hold.

    Args:
        item_id: The work item ID.

    Returns:
        A dict with action and item_id keys.
    """
    # Direct durable write: the abort path never enters a StatusLifecycle
    # context manager, so the release-by-default exit hook cannot clear the
    # claim — keep the explicit release (assignee-hold convention).
    StatusLifecycle.update_status(item_id, "open", assignee="")
    _mark_pane_aborted()
    LOG.info("Intake aborted for %s", item_id)
    return {"success": True, "action": "aborted", "item_id": item_id}


def _mark_pane_aborted() -> None:
    """Set the herdr pane title to the red state (fail-open).

    Abort paths surface the failure even when no final report is rendered.
    """
    try:
        from shared.herdr_pane import mark_aborted

        mark_aborted()
    except Exception:  # pane updates must never break abort
        LOG.debug("herdr pane abort status update failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_rank(stage: str) -> int | None:
    """Return the positional rank of ``stage``, or ``None`` when unknown.

    Args:
        stage: A stage name (``idea``, ``intake_complete``, ``plan_complete``,
            ``in_review``, ``done``).

    Returns:
        The zero-based rank inside :data:`_STAGE_SEQUENCE`, or ``None`` for a
        value that is not part of the known progression.
    """
    try:
        return _STAGE_SEQUENCE.index((stage or "").strip())
    except ValueError:
        return None


def _fetch_stage(item_id: str) -> str | None:
    """Return the item's current stage, or ``None`` when it cannot be read.

    Args:
        item_id: The work item ID.

    Returns:
        The trimmed stage value, or ``None`` when ``wl show`` fails or returns
        no work item (callers must treat ``None`` as "do not touch").
    """
    try:
        data = run_wl(["wl", "show", item_id, "--json"])
    except RuntimeError as exc:
        LOG.warning("Autoplan pre-check could not fetch %s: %s", item_id, exc)
        return None
    item = data.get("workItem") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        LOG.warning("Autoplan pre-check got no work item for %s", item_id)
        return None
    return (item.get("stage") or "").strip()


def _autoplan_decision(item_id: str) -> dict:
    """Run the canonical autoplan helper, degrading to ``error`` on failure.

    Any failure (unexpected exception, non-dict result) is mapped to the
    ``error`` decision, which callers treat as ``plan`` (fail-safe: never skip
    planning silently).

    Args:
        item_id: The work item ID.

    Returns:
        The helper's decision dict (``decision``/``effort``/``risk``).
    """
    try:
        if plan_if_needed is None:
            raise _PlanHelperError(
                "the canonical autoplan helper is unavailable in this install"
            )
        result = plan_if_needed(item_id)
    except Exception as exc:  # noqa: BLE001 -- fail-safe: never skip on error
        LOG.warning("Autoplan pre-check failed for %s: %s", item_id, exc)
        return {"decision": "error", "effort": "", "risk": "", "error": str(exc)}
    if not isinstance(result, dict):
        LOG.warning("Autoplan pre-check returned a non-dict for %s", item_id)
        return {"decision": "error", "effort": "", "risk": ""}
    return result


def _append_autoplan_comment(
    item_id: str, decision: str, effort: str, risk: str
) -> None:
    """Record the autoplan decision as a comment, idempotently (AC4).

    A deterministic hash marker derived from the decision values is embedded in
    the comment; a comment carrying the same marker means the record already
    exists and nothing is posted. Comment failures are logged and swallowed —
    the stage transition is the primary effect and must not be lost.

    Args:
        item_id: The work item ID.
        decision: The helper's decision (``skip``/``plan``/``error``).
        effort: The effort label reported by the helper.
        risk: The risk level reported by the helper.
    """
    marker_key = f"intake-autoplan:{decision}:{effort}:{risk}"
    marker_hash = hashlib.sha256(marker_key.encode("utf-8")).hexdigest()[:16]
    marker = f"intake-autoplan-decision:{marker_hash}"

    try:
        existing = run_wl(["wl", "comment", "list", item_id, "--json"])
    except RuntimeError as exc:
        LOG.warning("Autoplan comment lookup failed for %s: %s", item_id, exc)
        existing = {}
    for comment in (existing or {}).get("comments", []) or []:
        if marker in (comment.get("comment") or ""):
            LOG.debug("Autoplan decision comment already present for %s", item_id)
            return

    if decision == "skip":
        summary = (
            "effort and risk are below the planning threshold — planning is "
            "not required, stage advanced to `plan_complete`"
        )
    elif decision == "error":
        summary = "the pre-check could not decide — planning still runs"
    else:
        summary = "effort/risk above the skip threshold — planning still runs"

    body = "\n".join([
        marker,
        "# Intake Autoplan Pre-check",
        "",
        f"Decision: {decision}",
        f"Effort: {effort or 'unknown'}",
        f"Risk: {risk or 'unknown'}",
        f"Outcome: {summary}",
    ])
    try:
        run_wl([
            "wl", "comment", "add", item_id,
            "--author", _AUTOPLAN_AUTHOR,
            "--comment", body,
            "--json",
        ])
    except RuntimeError as exc:
        LOG.warning("Autoplan decision comment failed for %s: %s", item_id, exc)


def _run_wl_update_description(item_id: str, description_file: str) -> None:
    """Run ``wl update --description-file`` to apply the intake draft.

    Args:
        item_id: The work item ID.
        description_file: Path to the description file.

    Raises:
        RuntimeError: If the wl command fails (includes the underlying wl
            error detail). The command is routed through the shared
            ``run_wl`` helper so it works regardless of the caller's cwd.
    """
    cmd = [
        "wl", "update", item_id,
        "--description-file", description_file,
        "--json",
    ]
    LOG.debug("Running: %s", " ".join(cmd))
    run_wl(cmd)
    LOG.info("Description file applied for %s: %s", item_id, description_file)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        description="Intake skill: StatusLifecycle CLI for the intake process",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    start_parser = subparsers.add_parser("start", help="Claim the work item")
    start_parser.add_argument("item_id", help="Work item ID (e.g. SA-XXXX)")
    start_parser.add_argument(
        "--assignee", "-a",
        default=None,
        help="Optional assignee name",
    )

    # finish
    finish_parser = subparsers.add_parser("finish", help="Complete intake")
    finish_parser.add_argument("item_id", help="Work item ID")
    finish_parser.add_argument(
        "--description-file", "-d",
        default=None,
        help="Path to the intake draft description file",
    )

    # auto-complete
    auto_parser = subparsers.add_parser("auto-complete", help="Auto-complete intake")
    auto_parser.add_argument("item_id", help="Work item ID")

    # autoplan (closing pre-check: skip planning when below threshold)
    autoplan_parser = subparsers.add_parser(
        "autoplan",
        help=(
            "Apply the autoplan pre-check and advance to plan_complete "
            "when planning is not required"
        ),
    )
    autoplan_parser.add_argument("item_id", help="Work item ID")


    # abort
    abort_parser = subparsers.add_parser("abort", help="Abort intake")
    abort_parser.add_argument("item_id", help="Work item ID")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for success, 1 for error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    with Timer("intake") as timer:
        try:
            with Timer(f"cmd_{args.command}"):
                if args.command == "start":
                    result = cmd_start(args.item_id, assignee=args.assignee)
                elif args.command == "finish":
                    result = cmd_finish(args.item_id, description_file=args.description_file)
                elif args.command == "auto-complete":
                    result = cmd_auto_complete(args.item_id)
                elif args.command == "autoplan":
                    result = cmd_autoplan(args.item_id)
                elif args.command == "abort":
                    result = cmd_abort(args.item_id)
                else:
                    print(json.dumps({"success": False, "error": f"Unknown command: {args.command}"}))
                    return 1
        except Exception as exc:  # noqa: BLE001 -- command failure logged
            LOG.error("Command failed: %s", exc)
            print(json.dumps({"success": False, "error": str(exc)}))
            return 1

        # Timing report: human-readable on stderr, structured in JSON output
        # (additive only — existing keys unchanged, SA-0MT319YGQ002E801 AC5).
        print(timer.render(), file=sys.stderr)
        result["timing"] = timer.to_dict()

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    sys.exit(main())
