from __future__ import annotations

import json
import shlex
from subprocess import CalledProcessError, check_output
from typing import Any, Dict, List, Optional


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
        out = self._run(["dep", "add", blocked, blocker])
        return out is not None

    def dep_rm(self, blocked: str, blocker: str) -> bool:
        out = self._run(["dep", "rm", blocked, blocker])
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
