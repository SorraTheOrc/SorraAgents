"""Tests for the linter-derived re-verification sweep (SA-0MW7Z1JCE009G6J3).

Covers the acceptance criteria:

- AC2: per-item classification ``still-reproduces`` / ``no-longer-reproduces``
  / ``target-gone`` from the recorded verification command.
- AC3: ``target-gone`` / ``no-longer-reproduces`` items are closed with an
  evidence comment and a recorded rationale — never silently.
- AC4: only linter-family items are touched; human-authored bug/feature items
  are never closed.
- AC5: dry-run makes ZERO worklog mutations; a completed item is skipped, so
  re-running is idempotent.
- AC6: the five scenario tests (deleted target, fixed diagnostic, still-firing
  diagnostic, human-authored untouched, dry-run no-writes).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from refactor.scripts import reverify as rv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_runner(returncode: int = 0, stdout: str = "[]"):
    calls: list[list[str]] = []

    def run(argv, cwd):
        calls.append(list(argv))
        return _cp(returncode=returncode, stdout=stdout)

    return run, calls


class FakeWl:
    """Records ``wl`` commands and returns configurable JSON responses."""

    def __init__(self, comment_ok: bool = True, close_ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.comment_ok = comment_ok
        self.close_ok = close_ok

    def __call__(self, cmd, worklog_dir=None):
        self.calls.append(list(cmd))
        if cmd[:3] == ["wl", "comment", "add"]:
            return {"success": True} if self.comment_ok else None
        if cmd[:2] == ["wl", "close"]:
            if not self.close_ok:
                return {"closed": 0, "failed": 1}
            return {"closed": 1, "failed": 0}
        return {"workItems": []}

    def close_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["wl", "close"]]

    def comment_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["wl", "comment", "add"]]


def _refactor_item(
    item_id: str = "SA-0MREF1",
    *,
    code: str = "S112",
    path: str = "skill/x.py",
    status: str = "open",
    issue_type: str = "task",
    description: str | None = None,
    title: str | None = None,
) -> dict:
    """A minimal Refactor-family item carrying the identity marker."""
    return {
        "id": item_id,
        "title": title or f"Refactor: Lint in {path}",
        "description": description
        if description is not None
        else f"Refactor-Finding-Key: {code}:{path}\n\nbody",
        "status": status,
        "issueType": issue_type,
        "tags": ["Refactor"],
    }


def _audit_item(
    item_id: str = "SA-0MAUD1",
    *,
    code: str = "S112",
    path: str = "skill/x.py",
    line: int = 5,
    description: str | None = None,
) -> dict:
    return {
        "id": item_id,
        "title": f"ruff {code} in {path}:{line} — candidate false positive — producer decision required",
        "description": description
        if description is not None
        else f"CFP-Chore-Key: ruff:{code}:{path}:{line}",
        "status": "open",
        "issueType": "chore",
        "tags": [],
    }


def _write(path: Path, text: str = "a = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# AC6: classification scenarios
# ---------------------------------------------------------------------------


class TestClassification:
    def test_target_deleted_classified_target_gone(self, tmp_path: Path):
        """A missing target file is ``target-gone`` and no command runs."""
        item = _refactor_item(path="skill/gone.py")
        run, calls = _fake_runner()

        result = rv.classify_item(item, str(tmp_path), run_command=run)

        assert result is not None
        assert result.classification == rv.CLASS_GONE
        assert "not found" in result.reason
        assert calls == [], "a deleted target must not run the linter"

    def test_diagnostic_fixed_classified_no_longer_reproduces(
        self, tmp_path: Path
    ):
        """A clean ruff run (exit 0, empty list) is ``no-longer-reproduces``."""
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(code="S112", path="skill/x.py")
        run, calls = _fake_runner(returncode=0, stdout="[]")

        result = rv.classify_item(item, str(tmp_path), run_command=run)

        assert result is not None
        assert result.classification == rv.CLASS_FIXED
        assert result.hit_count == 0
        assert calls, "the verification command must be re-run"
        assert "--output-format" in calls[0]

    def test_diagnostic_still_firing_classified_still_reproduces(
        self, tmp_path: Path
    ):
        """A ruff hit means ``still-reproduces``."""
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(code="S112", path="skill/x.py")
        stdout = (
            '[{"code": "S112", "filename": "skill/x.py", '
            '"location": {"row": 1}, "message": "m"}]'
        )
        run, _ = _fake_runner(returncode=1, stdout=stdout)

        result = rv.classify_item(item, str(tmp_path), run_command=run)

        assert result is not None
        assert result.classification == rv.CLASS_STILL
        assert result.hit_count == 1

    def test_recorded_command_is_reused_verbatim(self, tmp_path: Path):
        """The sweep prefers the command recorded at creation."""
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(
            code="S112",
            path="skill/x.py",
            description=(
                "Refactor-Finding-Key: S112:skill/x.py\n"
                "Refactor-Verification-Command: ruff check --isolated "
                "--select S112 skill/x.py"
            ),
        )
        run, calls = _fake_runner(returncode=0, stdout="[]")

        result = rv.classify_item(item, str(tmp_path), run_command=run)

        assert result is not None
        assert result.command == "ruff check --isolated --select S112 skill/x.py"
        assert calls[0][:2] == ["ruff", "check"]

    def test_unverifiable_without_a_command(self, tmp_path: Path):
        """A non-ruff / codeless finding is ``unverifiable`` and untouched."""
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(code="security", path="skill/x.py")
        run, calls = _fake_runner()

        result = rv.classify_item(item, str(tmp_path), run_command=run)

        assert result is not None
        assert result.classification == rv.CLASS_UNVERIFIABLE
        assert calls == []


# ---------------------------------------------------------------------------
# AC4: attribution safety
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_human_authored_bug_is_never_attributed(self):
        item = _refactor_item(issue_type="bug")
        assert rv.attribute_item(item, "/tmp") is None

    def test_human_authored_feature_is_never_attributed(self):
        item = _refactor_item(issue_type="feature")
        assert rv.attribute_item(item, "/tmp") is None

    def test_untagged_human_task_is_never_attributed(self):
        item = {
            "id": "SA-0MHUMAN",
            "title": "Write the release notes",
            "description": "A normal task",
            "status": "open",
            "issueType": "task",
            "tags": [],
        }
        assert rv.attribute_item(item, "/tmp") is None

    def test_audit_chore_attributed_from_key(self):
        item = _audit_item()
        evidence = rv.attribute_item(item, "/tmp")
        assert evidence is not None
        assert evidence.family == "audit"
        assert evidence.code == "S112"
        assert evidence.path == "skill/x.py"

    def test_quality_title_attributed(self):
        item = {
            "id": "SA-0MQUAL",
            "title": "[MEDIUM] /abs/skill/x.py:5 — message here (SIM117)",
            "description": "",
            "status": "open",
            "issueType": "task",
            "tags": ["Refactor"],
        }
        evidence = rv.attribute_item(item, "/")
        assert evidence is not None
        assert evidence.family == "quality"
        assert evidence.code == "SIM117"
        assert evidence.path == "abs/skill/x.py"

    def test_completed_item_is_never_attributed(self):
        item = _refactor_item(status="completed")
        assert rv.attribute_item(item, "/tmp") is None


# ---------------------------------------------------------------------------
# AC3 / AC5: apply semantics + dry-run
# ---------------------------------------------------------------------------


class TestSweep:
    def test_dry_run_makes_no_writes(self, tmp_path: Path):
        """Dry-run reports the action but performs ZERO worklog mutations."""
        item = _refactor_item(path="skill/gone.py")
        fake_wl = FakeWl()
        run, _ = _fake_runner()

        report = rv.run_sweep(
            [item], str(tmp_path), apply=False, run_command=run, wl_run=fake_wl
        )

        assert report["dry_run"] is True
        assert report["counts"][rv.CLASS_GONE] == 1
        assert fake_wl.calls == [], f"dry-run mutated the worklog: {fake_wl.calls}"
        assert report["applied"] == []

    def test_apply_closes_target_gone_with_evidence(self, tmp_path: Path):
        item = _refactor_item(item_id="SA-0MGONE", path="skill/gone.py")
        fake_wl = FakeWl()
        run, _ = _fake_runner()

        report = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )

        assert report["counts"][rv.CLASS_GONE] == 1
        comments = fake_wl.comment_calls()
        closes = fake_wl.close_calls()
        assert len(comments) == 1 and len(closes) == 1
        comment_text = " ".join(comments[0])
        assert rv.EVIDENCE_MARKER in comment_text
        assert "target-gone" in comment_text
        # The rationale names the missing target and never claims a silent close.
        assert "no longer exists" in " ".join(closes[0])
        # Comment is recorded BEFORE the close.
        assert fake_wl.calls.index(comments[0]) < fake_wl.calls.index(closes[0])

    def test_apply_closes_fixed_with_evidence(self, tmp_path: Path):
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(item_id="SA-0MFIXED", code="S112", path="skill/x.py")
        fake_wl = FakeWl()
        run, _ = _fake_runner(returncode=0, stdout="[]")

        report = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )

        assert report["counts"][rv.CLASS_FIXED] == 1
        assert len(fake_wl.close_calls()) == 1
        assert "no longer reproduces" in " ".join(fake_wl.close_calls()[0])

    def test_apply_leaves_still_reproduces_untouched(self, tmp_path: Path):
        """A live diagnostic keeps its item and gains no comment."""
        _write(tmp_path / "skill" / "x.py")
        item = _refactor_item(item_id="SA-0MLIVE", code="S112", path="skill/x.py")
        fake_wl = FakeWl()
        run, _ = _fake_runner(returncode=1, stdout='[{"code": "S112"}]')

        report = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )

        assert report["counts"][rv.CLASS_STILL] == 1
        assert fake_wl.calls == [], "a live finding must be left untouched"
        assert report["applied"] == []

    def test_apply_never_closes_human_authored_items(self, tmp_path: Path):
        item = _refactor_item(item_id="SA-0MHUMAN", issue_type="bug")
        fake_wl = FakeWl()
        run, _ = _fake_runner()

        report = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )

        assert report["attributed"] == 0
        assert fake_wl.calls == []

    def test_idempotent_re_run_skips_closed_items(self, tmp_path: Path):
        """A terminal item is not re-attributed, so a second sweep is a no-op."""
        item = _refactor_item(item_id="SA-0MDONE", path="skill/gone.py")
        fake_wl = FakeWl()
        run, _ = _fake_runner()

        first = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )
        assert first["applied"][0]["action"] == "closed"

        # The worklog now reports the item as completed; the next load+run
        # must neither attribute nor touch it again.
        item["status"] = "completed"
        before = len(fake_wl.calls)
        second = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )
        assert second["attributed"] == 0
        assert second["applied"] == []
        assert len(fake_wl.calls) == before

    def test_failed_comment_blocks_the_close(self, tmp_path: Path):
        """An item is never closed without its evidence comment recorded."""
        item = _refactor_item(item_id="SA-0MNOCMT", path="skill/gone.py")
        fake_wl = FakeWl(comment_ok=False)
        run, _ = _fake_runner()

        report = rv.run_sweep(
            [item], str(tmp_path), apply=True, run_command=run, wl_run=fake_wl
        )

        assert report["applied"][0]["action"] == "error"
        assert report["applied"][0]["stage"] == "comment"
        assert fake_wl.close_calls() == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_apply_defaults_to_false(self):
        args = rv.parse_args([])
        assert args.apply is False

    def test_apply_flag_parsed(self):
        args = rv.parse_args(["--apply", "--json"])
        assert args.apply is True
        assert args.json is True
