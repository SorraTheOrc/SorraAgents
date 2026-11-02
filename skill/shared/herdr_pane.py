"""Herdr pane-title status indicator (SA-0MTFLQEQQ0083EW1).

When a work-item skill session ends, the operator watching a grid of herdr
panes needs to tell at a glance whether a session finished cleanly, left
something for their attention, or failed. This shared helper prepends a
status prefix to the current pane label:

======================  =========  =========
Status                  Icon       Text
======================  =========  =========
``done``                ``✅``     ``Done``
``note``                ``⚠️``     ``Note``
``attention``           ``🚫``     ``Attention``
======================  =========  =========

The text fallbacks are used when icons are unavailable (``--no-icons`` /
``WL_NO_ICONS=1``), mirroring the report renderer's convention.

Everything here is **fail-open**: outside a herdr pane, without a pane id, or
when the herdr CLI is unavailable, the functions return a "not updated" result
instead of raising, so a skill run is never broken by a pane-title failure.

The pane is identified by the ``HERDR_PANE_ID`` environment variable (present
in pi sessions launched via herdr) and renamed with
``herdr pane rename <pane_id> <label>``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping

#: Maximum pane-title length enforced by herdr (``pane-title.ts``). The bound
#: is expressed in UTF-16 code units because herdr (JavaScript) measures
#: ``String.length``, not Unicode code points.
MAX_PANE_TITLE_LENGTH = 60

#: Emoji prefixes per status.
STATUS_ICONS: dict[str, str] = {
    "done": "\u2705",       # ✅
    "note": "\u26a0\ufe0f",  # ⚠️
    "attention": "\U0001f6ab",  # 🚫
}

#: Bracketed-text fallbacks per status (no colour coding).
STATUS_TEXT: dict[str, str] = {
    "done": "Done",
    "note": "Note",
    "attention": "Attention",
}

#: Producer-action / note values that carry no substantive content.
ROUTINE_TEXTS: frozenset[str] = frozenset({
    "", "-", "--", "n/a", "na", "none", "none needed", "no action needed",
    "no actions needed", "no producer action needed",
})

#: Optional lead-in phrasings that introduce a *routine workflow-progression*
#: action ("next step: implement", "then run /skill:plan").
#:
#: Routine progression belongs in the report's ``Ready for <next_action>``
#: conclusion line, never in ``## Producer Actions`` (SA-0MVNQ3RLW006XEFW).
ROUTINE_STEP_LEADINS: tuple[str, ...] = ("next step", "then")

#: Imperative verbs that signal routine workflow progression when followed by
#: a workflow target.
ROUTINE_STEP_VERBS: tuple[str, ...] = (
    "run", "start", "begin", "proceed with", "continue with", "proceed to",
    "review and proceed", "review and continue",
)

#: Workflow targets that make a verb phrase routine progression: the next
#: skill (``/skill:<name>``, matched separately) or a known stage name.
ROUTINE_STEP_TARGETS: tuple[str, ...] = (
    "idea", "intake", "intake_complete", "plan", "planning",
    "plan_complete", "implement", "implementation", "in_progress", "audit",
    "review", "in_review", "release", "done", "the next stage",
)

#: A workflow target: the next skill (``/skill:<name>``), a known stage
#: name, or the phrase "the next stage". The trailing negative lookahead keeps
#: short stage names from matching inside longer words (``plan`` in
#: ``planner``, ``review`` in ``reviewer``).
_ROUTINE_TARGET_RE = (
    r"(?:/skill:[a-z][a-z0-9_-]*"
    r"|the next stage"
    r"|"
    + "|".join(
        re.escape(target)
        for target in sorted(ROUTINE_STEP_TARGETS, key=len, reverse=True)
    )
    + r")(?![a-z0-9_-])"
)

#: A workflow-progression verb (longest first so ``proceed with`` wins over a
#: bare ``proceed`` prefix — there is no bare ``proceed`` verb by design).
_ROUTINE_VERB_RE = (
    r"(?:review and proceed|review and continue|proceed with|proceed to"
    r"|continue with|run|start|begin)"
)

#: Clause shapes that count as routine workflow progression. A clause must
#: match one entirely: a verb + target, "next step[:] <target>", or a review of
#: the current stage's brief.
_ROUTINE_CLAUSE_RES = (
    re.compile(rf"{_ROUTINE_VERB_RE}\s+{_ROUTINE_TARGET_RE}"),
    re.compile(rf"next step\s*:?\s*{_ROUTINE_TARGET_RE}"),
    re.compile(rf"review\s+the\s+(?:{_ROUTINE_TARGET_RE}\s+)?brief"),
)

#: Clause separators: conjunction lead-ins and punctuation that join two
#: independently-classifiable clauses.
_ROUTINE_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*", re.IGNORECASE,
)

#: Optional lead-ins stripped from the start of a clause before matching.
_ROUTINE_LEADIN_RE = re.compile(r"^(?:then|next step\s*:?)\s+", re.IGNORECASE)

#: Conservative aliases accepted for the ``status`` argument.
_STATUS_ALIASES: dict[str, str] = {
    "success": "done", "succeeded": "done", "ok": "done", "complete": "done",
    "completed": "done", "pass": "done", "passed": "done",
    "warning": "note", "warn": "note", "info": "note",
    "error": "attention", "fail": "attention", "failed": "attention",
    "abort": "attention", "aborted": "attention", "blocked": "attention",
}

#: Values that request text fallbacks when set in the environment.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Leading icon prefix (optional trailing whitespace).
_ICON_PREFIX_RE = re.compile(r"^(?:\u2705|\u26a0\ufe0f|\u26a0|\U0001f6ab)\s*")
#: Leading text prefix (word boundary so "Notes" is not stripped).
_TEXT_PREFIX_RE = re.compile(r"^(?:Done|Note|Attention)(?=\s|$)", re.IGNORECASE)
#: Trailing work-item ID suffix, e.g. `` - SA-0MTFLQEQQ0083EW1``.
_ID_SUFFIX_RE = re.compile(r"\s+-\s+([A-Za-z][A-Za-z0-9]*-\S+)$")


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def is_substantive(value) -> bool:
    """Return True when *value* carries content worth the producer's attention."""
    if value is None:
        return False
    return str(value).strip().lower() not in ROUTINE_TEXTS


def _is_routine_clause(clause: str) -> bool:
    """Return True when *clause* is entirely routine workflow progression.

    The match is anchored at both ends so a routine-looking prefix followed by
    a blocking condition ("Run /skill:plan after you approve the migration")
    is NOT accepted: ambiguous text must fall through to the caller unchanged
    (prefer false negatives over hiding a real producer action).
    """
    text = clause.strip().lower().rstrip(".!?:;").strip()
    if not text:
        return False
    # Match the clause as written first (so "next step: implement" hits the
    # lead-in clause shape), then retry without a leading "then"/"next step"
    # so "Then run /skill:ship" reduces to the verb + target shape.
    candidates = [text]
    stripped = _ROUTINE_LEADIN_RE.sub("", text).strip()
    if stripped and stripped != text:
        candidates.append(stripped)
    return any(
        _clause_re.fullmatch(candidate)
        for candidate in candidates
        for _clause_re in _ROUTINE_CLAUSE_RES
    )


def is_routine_next_step(value) -> bool:
    """Return True when *value* is *only* routine workflow progression.

    Routine progression — "Proceed to /skill:plan", "Run /skill:audit",
    "Next step: implement", "Review the intake brief and then run
    /skill:plan" — duplicates the report Conclusion's ``Ready for
    <next_action>`` line and must never be presented as a producer action
    (SA-0MVNQ3RLW006XEFW).

    Conservative by design: **every** clause must match the routine
    vocabulary, otherwise the whole text is treated as non-routine so a real
    producer action is never hidden. A missing/empty value is not a next step
    (``False``), and the routine placeholders in :data:`ROUTINE_TEXTS` are not
    next steps either — callers keep their existing handling for those.
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    clauses = [c for c in _ROUTINE_CLAUSE_SPLIT_RE.split(text) if c.strip()]
    if not clauses:
        return False
    return all(_is_routine_clause(clause) for clause in clauses)


def normalise_producer_actions(value, default: str = "None needed") -> str:
    """Collapse routine-only producer actions to *default* (else verbatim).

    Returns the **original text unchanged** unless :func:`is_routine_next_step`
    accepts it, so genuine blocking actions ("Answer the OPEN QUESTION …") are
    never rewritten. ``None``/blank values already render as *default*.
    """
    if value is None:
        return default
    text = str(value)
    if is_routine_next_step(text):
        return default
    return text


def _verdict(row) -> str:
    """Extract and normalise the verdict from an AC row."""
    if isinstance(row, (list, tuple)) and len(row) >= 4:
        return str(row[3]).strip().lower()
    return ""


def derive_status(
    acceptance_criteria=None,
    producer_actions=None,
    notes=None,
) -> str:
    """Derive the pane status from the report inputs.

    * ``attention`` — any AC verdict other than ``met`` (unmet/partial/adjusted
      all count as not fully met);
    * ``note`` — all ACs met but there is substantive producer content
      (producer actions or notes);
    * ``done`` — all ACs met and nothing substantive for the producer.
    """
    for row in acceptance_criteria or []:
        if _verdict(row) != "met":
            return "attention"
    if is_substantive(normalise_producer_actions(producer_actions)) \
            or is_substantive(notes):
        return "note"
    return "done"


def _normalise_status(status) -> str:
    key = str(status or "").strip().lower()
    key = _STATUS_ALIASES.get(key, key)
    return key if key in STATUS_ICONS else "attention"


def resolve_prefix(status, use_icons: bool = True) -> str:
    """Return the prefix (icon or text) for *status*."""
    key = _normalise_status(status)
    return STATUS_ICONS[key] if use_icons else STATUS_TEXT[key]


def icons_enabled(no_icons: bool = False, env: Mapping | None = None) -> bool:
    """Whether emoji prefixes are enabled.

    Follows the report renderer's convention: ``--no-icons`` or
    ``WL_NO_ICONS=1`` (truthy) selects the text fallbacks.
    """
    if no_icons:
        return False
    env_map = os.environ if env is None else env
    flag = str(env_map.get("WL_NO_ICONS", "")).strip().lower()
    return flag not in _TRUTHY


# ---------------------------------------------------------------------------
# Pane-title composition
# ---------------------------------------------------------------------------


def js_length(value: str) -> int:
    """Return the herdr/JavaScript ``String.length`` of *value*."""
    return len(value.encode("utf-16-le")) // 2


def strip_status_prefix(label) -> str:
    """Remove any existing status prefixes so re-running never stacks them."""
    result = str(label or "").strip()
    while True:
        match = _ICON_PREFIX_RE.match(result) or _TEXT_PREFIX_RE.match(result)
        if not match:
            return result
        result = result[match.end():].lstrip()


def _truncate_to_js_length(value: str, budget: int) -> str:
    while value and js_length(value) > budget:
        value = value[:-1]
    return value


def truncate_pane_title(title: str) -> str:
    """Truncate *title* to herdr's limit, appending ``…`` when needed."""
    if js_length(title) <= MAX_PANE_TITLE_LENGTH:
        return title
    truncated = title
    while truncated and js_length(truncated) > MAX_PANE_TITLE_LENGTH - 1:
        truncated = truncated[:-1]
    return truncated + "\u2026"


def compose_pane_title(existing_label, status, use_icons: bool = True) -> str:
    """Compose the prefixed pane title, idempotently and within the herdr limit.

    Any pre-existing status prefix is removed first. When the combined title
    exceeds the herdr limit, a trailing `` - <work-item-id>`` suffix is
    preserved (truncating the descriptive part) so the hydrator can still match
    the pane to its work item; otherwise the combined title is truncated.
    """
    prefix = resolve_prefix(status, use_icons)
    base = strip_status_prefix(existing_label)
    combined = f"{prefix} {base}" if base else prefix
    if js_length(combined) <= MAX_PANE_TITLE_LENGTH:
        return combined

    match = _ID_SUFFIX_RE.search(base)
    if match:
        head = base[:match.start()].rstrip()
        suffix = f" - {match.group(1)}"
        prefix_part = f"{prefix} {head}".rstrip()
        budget = MAX_PANE_TITLE_LENGTH - js_length(suffix) - 1  # 1 for the ellipsis
        if budget > 0:
            return _truncate_to_js_length(prefix_part, budget) + "\u2026" + suffix

    return truncate_pane_title(combined)


# ---------------------------------------------------------------------------
# herdr CLI interaction
# ---------------------------------------------------------------------------


def _subprocess_env(env: Mapping | None) -> dict | None:
    """Environment for the herdr subprocess (None inherits the parent env)."""
    return None if env is None else dict(env)


def _resolve_herdr_bin(env_map: Mapping, which) -> str | None:
    override = env_map.get("HERDR_BIN_PATH")
    if override:
        return str(override)
    resolver = which or shutil.which
    return resolver("herdr")


def _stdout_indicates_error(stdout: str) -> bool:
    """True when the herdr CLI emitted a JSON ``error`` object."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict) and "error" in payload


def read_pane_label(
    pane_id: str,
    *,
    herdr_bin: str,
    runner: Callable | None = None,
    env: Mapping | None = None,
    timeout: int = 10,
) -> str | None:
    """Return the current label of *pane_id*, or ``None`` on any failure."""
    run = runner or subprocess.run
    try:
        proc = run(
            [herdr_bin, "pane", "list"],
            capture_output=True,
            text=True,
            env=_subprocess_env(env),
            timeout=timeout,
        )
        if getattr(proc, "returncode", 1) != 0:
            return None
        payload = json.loads(getattr(proc, "stdout", "") or "")
        panes = payload.get("result", {}).get("panes", [])
        for pane in panes:
            if pane.get("pane_id") == pane_id:
                return pane.get("label")
    except Exception:  # noqa: BLE001 - fail-open by design
        return None
    return None


def update_pane_title(
    status,
    *,
    label: str | None = None,
    pane_id: str | None = None,
    use_icons: bool | None = None,
    env: Mapping | None = None,
    runner: Callable | None = None,
    which: Callable | None = None,
    herdr_bin: str | None = None,
    timeout: int = 10,
) -> dict:
    """Prepend the *status* prefix to the current pane label.

    Fail-open: returns ``{"updated": False, "reason": ...}`` (never raises) when
    there is no pane, no herdr CLI, or the CLI call fails. Idempotent: a label
    that already carries the prefix is left untouched.
    """
    env_map = dict(os.environ) if env is None else dict(env)
    normalised = _normalise_status(status)
    pane = pane_id or env_map.get("HERDR_PANE_ID") or ""
    result: dict = {
        "updated": False,
        "status": normalised,
        "pane_id": pane or None,
        "reason": "",
    }

    if not pane:
        result["reason"] = "no herdr pane id"
        return result

    resolved_bin = herdr_bin or _resolve_herdr_bin(env_map, which)
    if not resolved_bin:
        result["reason"] = "herdr CLI unavailable"
        return result

    if use_icons is None:
        use_icons = icons_enabled(env=env_map)

    run = runner or subprocess.run
    if label is None:
        label = read_pane_label(
            pane, herdr_bin=resolved_bin, runner=run, env=env, timeout=timeout,
        )
        if label is None:
            result["reason"] = "pane label unavailable"
            return result

    new_label = compose_pane_title(label, normalised, use_icons=use_icons)
    if new_label == str(label).strip():
        result["reason"] = "already set"
        result["label"] = new_label
        return result

    try:
        proc = run(
            [resolved_bin, "pane", "rename", pane, new_label],
            capture_output=True,
            text=True,
            env=_subprocess_env(env),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open by design
        result["reason"] = f"herdr call failed: {exc}"
        return result

    if getattr(proc, "returncode", 1) != 0:
        result["reason"] = "herdr rename failed"
        return result
    if _stdout_indicates_error(getattr(proc, "stdout", "") or ""):
        result["reason"] = "herdr rename failed"
        return result

    result["updated"] = True
    result["label"] = new_label
    return result


def mark_aborted(**kwargs) -> dict:
    """Set the pane to the red (attention) state on abort/failure.

    Accepts the same keyword arguments as :func:`update_pane_title` and is
    fail-open in the same way.
    """
    return update_pane_title("attention", **kwargs)
