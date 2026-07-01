from __future__ import annotations

import json
from subprocess import CalledProcessError, check_output
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helper functions for comment operations
# ---------------------------------------------------------------------------


def _normalize_comment_ref(work_id: str, comment_id: str) -> str:
    """Normalize a comment reference to its full form.

    Accepts either a short tail (e.g. ``"C1"``) or a full ref
    (e.g. ``"SA-0XXX-C1"``) and returns the fully-qualified form.

    Args:
        work_id: The work item ID (e.g. ``"SA-0XXX"``).
        comment_id: The comment identifier (short or full).

    Returns:
        The fully-qualified comment reference.
    """
    if comment_id.startswith(work_id):
        return comment_id
    return f"{work_id}-{comment_id}"


def _extract_comments_from_response(
    w: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract a comments list from a ``wl show`` response dict.

    Handles multiple possible JSON structures from different WL CLI
    output formats: top-level ``comments`` key, ``workItem`` wrapper,
    ``data`` wrapper, ``items`` list, etc.

    Args:
        w: The parsed ``wl show`` response, or ``None``.

    Returns:
        A list of comment dicts (may be empty).
    """
    if not isinstance(w, dict):
        return []

    # Direct top-level comments
    if isinstance(w.get("comments"), list):
        return w["comments"]

    # Common wrappers: workItem, work_item, data, items
    for key in ("workItem", "work_item", "data", "items"):
        val = w.get(key)
        if isinstance(val, dict):
            cand = val.get("comments") or val.get("items") or val.get("data")
            if isinstance(cand, list):
                return cand
        if isinstance(val, list):
            return val

    return []


def _match_comment_id(comment: dict[str, Any], comment_id: str, ref: str = "") -> bool:
    """Check whether a comment dict matches a given comment identifier.

    Attempts to match against multiple possible key names that different
    WL CLI variants may use: ``id``, ``commentId``, ``comment_id``,
    ``ref``, ``reference``.

    Args:
        comment: A comment dict from the WL response.
        comment_id: The comment identifier to match (e.g. ``"C1"``).
        ref: The fully-qualified comment reference for comparison
             (e.g. ``"SA-0XXX-C1"``). If empty, ``comment_id`` is used.

    Returns:
        ``True`` if the comment matches, ``False`` otherwise.
    """
    cid = comment.get("id") or comment.get("commentId") or comment.get("comment_id")
    if cid and (
        str(cid) == comment_id
        or str(cid) == (ref or comment_id)
        or str(cid).endswith(str(comment_id))
    ):
        return True
    for key in ("ref", "reference"):
        v = comment.get(key)
        if v and (str(v) == (ref or comment_id) or str(v).endswith(str(comment_id))):
            return True
    return False


class WLAdapter:
    """Thin adapter around the `wl` CLI used by tests and local runs.

    This adapter shells out to the `wl` program. It keeps behavior permissive: when
    a command fails (wl missing or API not available) methods return None or an
    empty list rather than raising, so callers can decide how strict to be.
    """

    def _run(self, args: List[str]) -> Optional[str]:
        cmd = ["wl"] + args
        try:
            out = check_output(cmd, encoding="utf-8")
            return out
        except FileNotFoundError:
            return None
        except CalledProcessError:
            # permissive: return None on failure
            return None

    def list_children(self, parent: str) -> List[Dict[str, Any]]:
        out = self._run(["list", "--parent", parent, "--json"])
        if not out:
            return []
        try:
            return json.loads(out)
        except Exception:
            return []

    def dep_add(self, blocked: str, blocker: str) -> bool:
        # request machine-readable output from wl to make callers able to
        # parse/inspect responses if needed
        out = self._run(["dep", "add", blocked, blocker, "--json"])
        return out is not None

    def dep_rm(self, blocked: str, blocker: str) -> bool:
        out = self._run(["dep", "rm", blocked, blocker, "--json"])
        return out is not None

    def dep_list(self, id: str) -> List[Dict[str, Any]]:
        out = self._run(["dep", "list", id, "--json"])
        if not out:
            return []
        try:
            return json.loads(out)
        except Exception:
            return []

    def post_comment(self, id: str, text: str) -> bool:
        # quote the body and use wl comment add
        # wl comment add <id> --body "text"
        out = self._run(["comment", "add", id, "--body", text])
        return out is not None

    def show(self, id: str) -> Optional[Dict[str, Any]]:
        out = self._run(["show", id, "--json"])
        if not out:
            return None
        try:
            return json.loads(out)
        except Exception:
            return None

    def detect_existing_comment_exact(self, id: str, text: str) -> bool:
        w = self.show(id)
        if not w:
            return False
        comments = w.get("comments") or []
        for c in comments:
            if c.get("body") == text:
                return True
        return False

    def delete_comment(self, work_id: str, comment_id: str) -> bool:
        """Delete a comment and verify it is removed.

        Args:
            work_id: work item id (e.g. "SA-0XXX...")
            comment_id: comment identifier portion (e.g. "C1") or full form
                including work item prefix (e.g. "SA-0XXX-C1").

        Returns:
            True if deletion was successful and subsequent show no longer
            lists the comment, False otherwise.
        """
        ref = _normalize_comment_ref(work_id, comment_id)

        out = self._run(["comment", "delete", ref])
        if out is None:
            return False

        # Verify by fetching the work item and ensuring the comment is absent
        w = self.show(work_id)
        if not w:
            return False

        comments = _extract_comments_from_response(w)

        for c in comments or []:
            if _match_comment_id(c, comment_id, ref):
                return False
        return True
