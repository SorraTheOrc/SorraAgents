
# <!-- REFACTOR-SA-0MW0UKUY7002DRJW
# smell: unknown
# severity: medium
# description: `subprocess.run` without explicit `check` argument
# -->
"""Claim release helpers: session-end backstop, reaper and signal safety.

A non-empty ``assignee`` is a **scheduling hold** (``wl next`` skips a held
item), so a claim left behind after a session ends makes finished work
invisible to dispatch. Two gaps are covered here (SA-0MW0F91KN004J06U):

1. **Session-end / signal backstop.** ``release_session_claim`` and the
   ``signal_release`` guard release the session's own claim even when the run
   never reaches a terminal lifecycle step (SIGINT/SIGTERM, timeout, abort).
2. **Bounded stale-claim reaper.** ``reap_stale_claims`` releases
   non-producer claims on non-terminal items older than a TTL, records each
   release and is idempotent.

The producer hold (``Map``) and ``needsProducerReview`` items are sacrosanct;
``completed``/``deleted`` items are never touched by the reaper.

Most tests drive the real production functions through an in-memory ``wl``
runner. The persistence assertions that matter most (a session-end release and
an interrupting signal) are additionally exercised end-to-end against a
throwaway ``wl`` store, so "the persisted assignee is empty" is read back
through the real CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_SKILLS_ROOT = REPO_ROOT / "skill"
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.append(str(_SKILLS_ROOT))

from shared import claim_release

_AGENT = "matrix-agent"


# ---------------------------------------------------------------------------
# In-memory wl store (the injected runner seam)
# ---------------------------------------------------------------------------


def _iso(hours_ago: float) -> str:
    """An ISO-8601 UTC timestamp *hours_ago* hours before now."""
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return stamp.isoformat().replace("+00:00", "Z")


def _held(assignee: str, *, age_hours: float = 0.0, **extra) -> dict:
    """An item held by *assignee* whose last recorded activity was *age_hours* ago."""
    stamp = _iso(age_hours)
    return {"assignee": assignee, "activityAt": stamp, "updatedAt": stamp, **extra}


class MemoryWl:
    """Minimal stateful ``wl`` CLI over an in-memory item store."""

    def __init__(self, items: dict[str, dict]) -> None:
        self.items = {
            item_id: {
                "id": item_id,
                "title": item_id,
                "status": "open",
                "stage": "plan_complete",
                "assignee": "",
                "needsProducerReview": False,
                "deletedBy": "",
                "updatedAt": _iso(0),
                "activityAt": _iso(0),
                **item,
            }
            for item_id, item in items.items()
        }
        self.commands: list[list[str]] = []
        self.comments: list[dict] = []

    def runner(self, cmd: list[str]) -> SimpleNamespace:
        command = [str(token) for token in cmd]
        self.commands.append(command)
        if "list" in command:
            return self._ok({"success": True, "workItems": list(self.items.values())})
        item_id = self._item_id(command)
        if "show" in command and item_id is not None:
            item = self.items.get(item_id)
            if item is None:
                return self._fail(f"work item {item_id} not found")
            return self._ok({"success": True, "workItem": dict(item)})
        if "comment" in command and item_id is not None:
            self.comments.append(
                {
                    "item": item_id,
                    "comment": self._flag(command, "--comment"),
                }
            )
            return self._ok({"success": True})
        if "update" in command and item_id is not None:
            item = self.items[item_id]
            status = self._flag(command, "--status")
            if status is not None:
                item["status"] = status
            stage = self._flag(command, "--stage")
            if stage is not None:
                item["stage"] = stage
            assignee = self._flag(command, "--assignee")
            if assignee is not None:
                item["assignee"] = assignee
            review = self._flag(command, "--needs-producer-review")
            if review is not None:
                item["needsProducerReview"] = review == "yes"
            return self._ok({"success": True})
        return self._ok({"success": True})

    def assignee(self, item_id: str) -> str:
        return str(self.items[item_id]["assignee"])

    def update_commands(self, item_id: str) -> list[list[str]]:
        return [c for c in self.commands if "update" in c and item_id in c]

    @staticmethod
    def _ok(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    @staticmethod
    def _fail(message: str) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"success": False, "error": message}),
            stderr="",
        )

    @staticmethod
    def _item_id(command: list[str]) -> str | None:
        for token in command:
            if token.startswith("QZ-"):
                return token
        return None

    @staticmethod
    def _flag(command: list[str], name: str) -> str | None:
        try:
            return command[command.index(name) + 1]
        except (ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


class TestShouldRelease:
    """The one producer-aware predicate shared by every release path."""

    @pytest.mark.parametrize(
        ("item", "expected"),
        (
            ({"assignee": "agent"}, True),
            ({"assignee": "IMPLEMENT"}, True),
            ({"assignee": ""}, False),
            ({"assignee": "  "}, False),
            ({"assignee": "Map"}, False),
            ({"assignee": "map"}, False),
            ({"assignee": "MAP"}, False),
            (
                {"assignee": "agent", "needsProducerReview": True},
                False,
            ),
        ),
    )
    def test_predicate_is_producer_aware(self, item, expected) -> None:
        assert claim_release.should_release_assignee(item) is expected

    def test_producer_assignee_is_configurable(self, monkeypatch) -> None:
        monkeypatch.setenv(claim_release.PRODUCER_ASSIGNEE_ENV, "Producer")
        assert claim_release.should_release_assignee({"assignee": "Producer"}) is False
        assert claim_release.should_release_assignee({"assignee": "Map"}) is True


# ---------------------------------------------------------------------------
# Session-end release
# ---------------------------------------------------------------------------


class TestReleaseClaim:
    """``release_claim`` releases a non-producer claim and names the previous one."""

    def test_releases_a_non_producer_claim(self) -> None:
        store = MemoryWl({"QZ-1": {"assignee": _AGENT}})

        result = claim_release.release_claim(
            "QZ-1",
            reason="session-end",
            author=_AGENT,
            runner=store.runner,
        )

        assert result["released"] is True
        assert result["reason"] == "released"
        assert result["previous_assignee"] == _AGENT
        assert store.assignee("QZ-1") == ""
        assert store.comments and store.comments[0]["item"] == "QZ-1"
        assert _AGENT in store.comments[0]["comment"]

    def test_is_idempotent(self) -> None:
        store = MemoryWl({"QZ-1": {"assignee": _AGENT}})
        claim_release.release_claim(
            "QZ-1", reason="session-end", author=_AGENT, runner=store.runner
        )
        updates_after_first = len(store.update_commands("QZ-1"))

        second = claim_release.release_claim(
            "QZ-1", reason="session-end", author=_AGENT, runner=store.runner
        )

        assert second["released"] is False
        assert second["reason"] == "no-claim"
        assert len(store.update_commands("QZ-1")) == updates_after_first
        assert len(store.comments) == 1

    def test_preserves_the_producer_hold(self) -> None:
        store = MemoryWl({"QZ-1": {"assignee": "Map"}})

        result = claim_release.release_claim(
            "QZ-1", reason="session-end", author=_AGENT, runner=store.runner
        )

        assert result["released"] is False
        assert result["reason"] == "producer-hold"
        assert store.assignee("QZ-1") == "Map"
        assert store.update_commands("QZ-1") == []

    def test_preserves_a_needs_producer_review_item(self) -> None:
        store = MemoryWl(
            {"QZ-1": {"assignee": _AGENT, "needsProducerReview": True}}
        )

        result = claim_release.release_claim(
            "QZ-1", reason="session-end", author=_AGENT, runner=store.runner
        )

        assert result["released"] is False
        assert result["reason"] == "needs-producer-review"
        assert store.assignee("QZ-1") == _AGENT

    def test_expected_assignee_guards_against_other_sessions(self) -> None:
        """A session may only release the claim it actually owns."""
        store = MemoryWl({"QZ-1": {"assignee": "audit-agent"}})

        result = claim_release.release_claim(
            "QZ-1",
            reason="session-end",
            author=_AGENT,
            expected_assignee=_AGENT,
            runner=store.runner,
        )

        assert result["released"] is False
        assert result["reason"] == "claim-not-owned"
        assert store.assignee("QZ-1") == "audit-agent"

    def test_missing_item_is_reported_not_raised(self) -> None:
        store = MemoryWl({})

        result = claim_release.release_claim(
            "QZ-MISSING", reason="session-end", author=_AGENT, runner=store.runner
        )

        assert result["success"] is False
        assert result["released"] is False
        assert result["reason"] == "not-found"

    def test_deleted_item_is_never_touched(self) -> None:
        store = MemoryWl({"QZ-1": {"assignee": _AGENT, "deletedBy": "operator"}})

        result = claim_release.release_claim(
            "QZ-1", reason="session-end", author=_AGENT, runner=store.runner
        )

        assert result["released"] is False
        assert result["reason"] == "deleted"
        assert store.assignee("QZ-1") == _AGENT


class TestSessionBackstop:
    """The session-end helper resolves the session's own agent identity."""

    def test_uses_the_session_agent_name_from_the_environment(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("IMPLEMENT_AGENT_NAME", _AGENT)
        store = MemoryWl({"QZ-1": {"assignee": _AGENT}})

        result = claim_release.release_session_claim(
            "QZ-1", runner=store.runner
        )

        assert result["released"] is True
        assert store.assignee("QZ-1") == ""

    def test_does_not_release_a_sibling_sessions_claim(self, monkeypatch) -> None:
        monkeypatch.setenv("IMPLEMENT_AGENT_NAME", _AGENT)
        store = MemoryWl({"QZ-1": {"assignee": "audit-agent"}})

        result = claim_release.release_session_claim(
            "QZ-1", runner=store.runner
        )

        assert result["released"] is False
        assert store.assignee("QZ-1") == "audit-agent"

    def test_without_an_agent_identity_releases_any_non_producer_claim(
        self, monkeypatch
    ) -> None:
        for var in claim_release.SESSION_AGENT_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        store = MemoryWl({"QZ-1": {"assignee": "some-agent"}})

        result = claim_release.release_session_claim(
            "QZ-1", runner=store.runner
        )

        assert result["released"] is True
        assert store.assignee("QZ-1") == ""


# ---------------------------------------------------------------------------
# Stale-claim reaper
# ---------------------------------------------------------------------------


class TestReaper:
    """TTL-bounded, producer-aware, idempotent stale-claim release."""

    def test_releases_a_claim_older_than_the_ttl(self) -> None:
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=48)})

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["released_count"] == 1
        released = report["released"][0]
        assert released["id"] == "QZ-1"
        assert released["previous_assignee"] == _AGENT
        assert released["age_hours"] >= 24
        assert store.assignee("QZ-1") == ""
        assert store.comments and _AGENT in store.comments[0]["comment"]

    def test_leaves_a_fresh_claim_alone(self) -> None:
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=0.5)})

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["released_count"] == 0
        assert store.assignee("QZ-1") == _AGENT

    def test_is_idempotent(self) -> None:
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=48)})
        claim_release.reap_stale_claims(runner=store.runner, ttl_hours=24)
        updates_after_first = len(store.update_commands("QZ-1"))

        second = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert second["released_count"] == 0
        assert len(store.update_commands("QZ-1")) == updates_after_first

    @pytest.mark.parametrize(
        "item",
        (
            {"assignee": "Map", "activityAt": _iso(1000)},
            {
                "assignee": _AGENT,
                "needsProducerReview": True,
                "activityAt": _iso(1000),
            },
            {"assignee": _AGENT, "status": "completed", "activityAt": _iso(1000)},
            {"assignee": _AGENT, "status": "done", "activityAt": _iso(1000)},
            {"assignee": _AGENT, "deletedBy": "operator", "activityAt": _iso(1000)},
        ),
        ids=("producer", "needs-review", "completed", "done", "deleted"),
    )
    def test_never_touches_pinned_or_terminal_items(self, item) -> None:
        store = MemoryWl({"QZ-1": dict(item)})

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["released_count"] == 0
        assert store.update_commands("QZ-1") == []
        assert store.assignee("QZ-1") == item["assignee"]

    def test_unknown_timestamp_is_never_released_blind(self) -> None:
        store = MemoryWl(
            {"QZ-1": {"assignee": _AGENT, "activityAt": "", "updatedAt": ""}}
        )

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["released_count"] == 0
        assert store.assignee("QZ-1") == _AGENT

    def test_dry_run_reports_without_writing(self) -> None:
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=48)})

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24, dry_run=True
        )

        assert report["released_count"] == 1
        assert report["released"][0]["id"] == "QZ-1"
        assert store.assignee("QZ-1") == _AGENT
        assert store.update_commands("QZ-1") == []
        assert store.comments == []

    def test_ttl_is_configurable_by_environment(self, monkeypatch) -> None:
        monkeypatch.setenv(claim_release.REAPER_TTL_ENV, "2")
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=5)})

        report = claim_release.reap_stale_claims(runner=store.runner)

        assert report["ttl_hours"] == 2.0
        assert report["released_count"] == 1

    def test_uses_the_most_recent_activity_timestamp(self) -> None:
        """A claim refreshed by a mutation is not stale despite an old update."""
        store = MemoryWl(
            {
                "QZ-1": {
                    "assignee": _AGENT,
                    "updatedAt": _iso(100),
                    "activityAt": _iso(1),
                }
            }
        )

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["released_count"] == 0

    def test_reports_every_skipped_candidate(self) -> None:
        store = MemoryWl(
            {
                "QZ-1": {"assignee": "Map", "activityAt": _iso(100)},
                "QZ-2": {"assignee": _AGENT, "activityAt": _iso(1)},
            }
        )

        report = claim_release.reap_stale_claims(
            runner=store.runner, ttl_hours=24
        )

        assert report["scanned"] == 2
        assert report["released_count"] == 0
        assert set(report["skipped"]) == {"producer-hold", "fresh"}


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCli:
    """Both subcommands emit machine-readable JSON."""

    def test_release_subcommand(self, monkeypatch, capsys) -> None:
        store = MemoryWl({"QZ-1": {"assignee": _AGENT}})
        monkeypatch.setattr(claim_release, "_default_runner", store.runner)
        monkeypatch.setenv("IMPLEMENT_AGENT_NAME", _AGENT)

        rc = claim_release.main(
            ["release", "QZ-1", "--reason", "session-end", "--json"]
        )

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released"] is True
        assert store.assignee("QZ-1") == ""

    def test_reap_subcommand(self, monkeypatch, capsys) -> None:
        store = MemoryWl({"QZ-1": _held(_AGENT, age_hours=48)})
        monkeypatch.setattr(claim_release, "_default_runner", store.runner)

        rc = claim_release.main(["reap", "--ttl-hours", "24", "--json"])

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["released_count"] == 1
        assert payload["ttl_hours"] == 24.0


# ---------------------------------------------------------------------------
# Real-store integration: persistence and a real interrupting signal
# ---------------------------------------------------------------------------

REAL_GIT = shutil.which("git")
REAL_WL = shutil.which("wl")
requires_tooling = pytest.mark.skipif(
    REAL_GIT is None or REAL_WL is None,
    reason="requires git and wl on PATH to build a real worklog fixture",
)


def _fixture_project(base: Path) -> Path:
    """Create a throwaway repo with its own initialised ``QZ`` worklog store."""
    root = base / "repo"
    root.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "IMPLEMENT_", "PI_"))
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True, env=env, timeout=120,
            check=False,
        )
        assert proc.returncode == 0, f"{cmd}\n{proc.stdout}\n{proc.stderr}"
        return proc

    run([REAL_GIT, "init", "-b", "dev"])
    run([REAL_GIT, "config", "user.email", "claim-release@example.com"])
    run([REAL_GIT, "config", "user.name", "claim release test"])
    (root / ".worklog").mkdir()
    run(
        [
            REAL_WL, "init",
            "--worklog-dir", str(root / ".worklog"),
            "--prefix", "QZ",
            "--project-name", "claim release fixture",
            "--auto-export", "no",
            "--auto-sync", "no",
            "--agents-template", "skip",
            "--json",
        ]
    )
    return root


def _create_claimed_item(root: Path, assignee: str) -> str:
    """Create one item in the fixture store held by *assignee*."""
    worklog_dir = root / ".worklog"
    proc = subprocess.run(
        [
            REAL_WL, "--worklog-dir", str(worklog_dir), "create",
            "--title", "stale claim fixture",
            "--description", "Claim release regression fixture.",
            "--assignee", assignee,
            "--status", "in-progress",
            "--json",
        ],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)["workItem"]["id"]


def _read_assignee(root: Path, item_id: str) -> str:
    proc = subprocess.run(
        [
            REAL_WL, "--worklog-dir", str(root / ".worklog"),
            "show", item_id, "--json",
        ],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return str(json.loads(proc.stdout)["workItem"].get("assignee") or "")


@requires_tooling
def test_session_release_persists_an_empty_assignee(tmp_path: Path) -> None:
    """The normal end-of-session path clears the claim in a real store."""
    root = _fixture_project(tmp_path)
    item_id = _create_claimed_item(root, _AGENT)
    assert _read_assignee(root, item_id) == _AGENT

    result = claim_release.release_session_claim(
        item_id,
        author=_AGENT,
        expected_assignee=_AGENT,
        worklog_dir=str(root / ".worklog"),
    )

    assert result["released"] is True
    assert _read_assignee(root, item_id) == ""


@requires_tooling
@pytest.mark.parametrize("signum", (signal.SIGTERM,), ids=("SIGTERM",))
def test_interrupting_signal_releases_the_claim(tmp_path: Path, signum) -> None:
    """A signal-killed session still persists an empty assignee."""
    root = _fixture_project(tmp_path)
    item_id = _create_claimed_item(root, _AGENT)

    script = textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(_SKILLS_ROOT)!r})
        from shared import claim_release

        with claim_release.signal_release(
            {item_id!r},
            author={_AGENT!r},
            expected_assignee={_AGENT!r},
            worklog_dir={str(root / '.worklog')!r},
        ):
            os.kill(os.getpid(), {int(signum)})
            raise SystemExit("signal was not delivered")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(root), capture_output=True, text=True, timeout=120, check=False,
    )

    assert proc.returncode == 128 + int(signum), proc.stderr
    assert _read_assignee(root, item_id) == ""
