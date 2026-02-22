"""Context helpers — stage-to-action mapping and dispatch command building.

Maps work item stages to delegation actions and builds the shell commands
used to spawn agent sessions.

Usage::

    from ampa.engine.context import stage_to_action, build_dispatch_command

    action = stage_to_action("plan_complete")       # -> "implement"
    cmd = build_dispatch_command("WL-123", action)   # -> 'opencode run "work on WL-123 ..."'
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Shell command builder
# ---------------------------------------------------------------------------

# Stage-to-action mapping from engine PRD Section 5.2
STAGE_ACTION_MAP: dict[str, str] = {
    "idea": "intake",
    "intake_complete": "plan",
    "plan_complete": "implement",
}

# Action-to-shell-command templates
ACTION_COMMAND_MAP: dict[str, str] = {
    "intake": 'opencode run "/intake {id} do not ask questions"',
    "plan": 'opencode run "/plan {id}"',
    "implement": 'opencode run "work on {id} using the implement skill"',
}


def stage_to_action(stage: str) -> str | None:
    """Map a work item stage to a delegation action.

    Returns ``None`` if the stage has no delegation action.
    """
    return STAGE_ACTION_MAP.get(stage)


def build_dispatch_command(work_item_id: str, action: str) -> str | None:
    """Build the shell command string for dispatching a delegation.

    Parameters
    ----------
    work_item_id:
        The work item ID to include in the command.
    action:
        The delegation action (``intake``, ``plan``, ``implement``).

    Returns
    -------
    str or None
        The shell command string, or ``None`` if the action is unknown.
    """
    template = ACTION_COMMAND_MAP.get(action)
    if template is None:
        return None
    return template.format(id=work_item_id)
