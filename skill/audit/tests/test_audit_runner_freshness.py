from __future__ import annotations

import contextlib
import datetime
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from audit.scripts import audit_runner


def _proc(returncode: int, stdout: str = ""):
    """Build a canned CompletedProcess for git subprocess calls."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="",
    )


_COMMIT_MARKER = audit_runner._TOUCHED_FILES_COMMIT_MARKER


class _FakeGitRepo:
    """In-memory git double for the touched-file fingerprint tests.

    Models only the queries the production resolver makes:

    * ``git log --all --grep`` (files in commits referencing the item);
    * ``git log --no-walk`` (files in comment-recorded commits);
    * ``git status --porcelain -- <paths>`` and
      ``git diff --name-only HEAD -- <paths>`` (narrowed worktree state);
    * ``git rev-parse HEAD:<path>`` (per-path blob hash);
    * ``git log -1 --format=%H -- <path>`` (latest touching commit).

    Unrelated repository state (``head_sha``, files outside the requested
    path set) is tracked so tests can prove it does NOT affect the
    fingerprint. Every command is recorded in ``calls``.
    """

    def __init__(self, *, head_sha: str = "h" * 40,
                 grep_files: list[str] | None = None, grep_rc: int = 0,
                 blobs: dict[str, str] | None = None,
                 touched_commits: dict[str, str] | None = None,
                 status: list[str] | None = None,
                 diff: list[str] | None = None, worktree_rc: int = 0,
                 comment_commits: dict[str, list[str]] | None = None):
        self.head_sha = head_sha
        self.grep_files = list(grep_files or [])
        self.grep_rc = grep_rc
        self.blobs = dict(blobs or {})
        self.touched_commits = dict(touched_commits or {})
        self.status = list(status or [])
        self.diff = list(diff or [])
        self.worktree_rc = worktree_rc
        self.comment_commits = dict(comment_commits or {})
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if joined.startswith("git log --all"):
            if self.grep_rc != 0:
                return _proc(self.grep_rc, "")
            lines = [f"{_COMMIT_MARKER}{self.head_sha}", *self.grep_files]
            return _proc(0, "\n".join(lines) + "\n")
        if joined.startswith("git log --no-walk"):
            lines: list[str] = []
            for sha in cmd[5:]:  # ["git","log","--no-walk","--name-only","--format=", sha...]
                lines.extend(self.comment_commits.get(sha, []))
            return _proc(0, ("\n".join(lines) + "\n") if lines else "")
        if joined.startswith("git status"):
            if self.worktree_rc != 0:
                return _proc(self.worktree_rc, "")
            requested = set(cmd[cmd.index("--") + 1:])
            lines = [
                line for line in self.status
                if audit_runner._parse_porcelain_path(line) in requested
            ]
            return _proc(0, ("\n".join(lines) + "\n") if lines else "")
        if joined.startswith("git diff"):
            if self.worktree_rc != 0:
                return _proc(self.worktree_rc, "")
            requested = set(cmd[cmd.index("--") + 1:])
            lines = [line for line in self.diff if line in requested]
            return _proc(0, ("\n".join(lines) + "\n") if lines else "")
        if cmd[:2] == ["git", "rev-parse"] and cmd[2] == "HEAD":
            return _proc(0, self.head_sha + "\n")
        if cmd[:2] == ["git", "rev-parse"] and cmd[2].startswith("HEAD:"):
            path = cmd[2][len("HEAD:"):]
            if path in self.blobs:
                return _proc(0, self.blobs[path] + "\n")
            return _proc(128, "")
        if joined.startswith("git log -1 --format=%H --"):
            sha = self.touched_commits.get(cmd[-1], "")
            return _proc(0, (sha + "\n") if sha else "")
        raise AssertionError(f"unexpected git command: {joined}")


@pytest.fixture(autouse=True)
def _free_audit_slot():
    """Neutralize the host-wide audit semaphore for deterministic unit tests.

    ``_call_pi`` acquires the real cross-process audit semaphore before
    launching the (mocked) subprocess. Under concurrent audit load the
    semaphore can saturate, making these timing-path unit tests flaky (see
    SA-0MSCDC4750019G9Y, SA-0MSCDC76A007JCJK). Replace it with a
    null-context so the mocked return paths are exercised directly.

    The real semaphore behavior is covered separately by
    ``test_audit_runner_concurrency.py``.
    """
    with mock.patch.object(
        audit_runner, "_acquire_audit_slot", return_value=contextlib.nullcontext()
    ):
        yield

class TestSharedFreshnessHelpers:
    """Unit tests for the shared ISO-8601 freshness helpers
    (SA-0MSL1Z70C007B9VZ): _parse_iso_utc and _audit_time_is_fresh.

    Both _check_audit_freshness and _get_child_audit_verdict previously
    inlined ~25 identical lines of Z-normalize → fromisoformat →
    tz-aware-ify → threshold-compare; the helpers below are the single
    implementation both call sites delegate to.
    """

    # ------------------------------------------------------------------
    # _parse_iso_utc
    # ------------------------------------------------------------------

    def test_parse_iso_utc_z_suffix_becomes_aware_utc(self):
        """'Z' suffix normalizes to +00:00 and stays tz-aware."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00.000Z")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 1
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_parse_iso_utc_naive_timestamp_gets_utc(self):
        """Naive timestamps (no offset) fall back to UTC."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00.000")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_parse_iso_utc_explicit_offset_preserved(self):
        """An explicit non-UTC offset is preserved, not rewritten."""
        parsed = audit_runner._parse_iso_utc("2026-08-01T00:00:00+02:00")
        assert parsed is not None
        assert parsed.utcoffset() == datetime.timedelta(hours=2)

    def test_parse_iso_utc_invalid_returns_none(self):
        """Unparseable values return None instead of raising."""
        assert audit_runner._parse_iso_utc("not-a-date") is None
        assert audit_runner._parse_iso_utc(None) is None
        assert audit_runner._parse_iso_utc(42) is None

    # ------------------------------------------------------------------
    # _audit_time_is_fresh
    # ------------------------------------------------------------------

    def test_audit_time_is_fresh_after_buffer(self):
        """auditedAt > updatedAt + buffer → fresh."""
        audited = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        updated = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        assert audit_runner._audit_time_is_fresh(audited, updated) is True

    def test_audit_time_is_fresh_within_buffer(self):
        """auditedAt within updatedAt + buffer → not fresh (needs the
        persistence-tolerance check in the child gate)."""
        updated = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.timezone.utc)
        audited = updated + datetime.timedelta(seconds=10)  # 10s < 60s buffer
        assert audit_runner._audit_time_is_fresh(audited, updated) is False

    def test_audit_time_is_fresh_before_update(self):
        """auditedAt before updatedAt → not fresh."""
        audited = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        updated = datetime.datetime(2026, 8, 2, tzinfo=datetime.timezone.utc)
        assert audit_runner._audit_time_is_fresh(audited, updated) is False


class TestTouchedFileResolution:
    """Unit tests for the per-work-item touched-file resolver
    (SA-0MSPZDALB000S18P AC1/AC4).

    The resolver is the foundation of the narrowed fingerprint: it decides
    WHICH files the gate is allowed to care about, so an unrelated commit or
    working-tree change elsewhere cannot invalidate a stored audit.
    """

    def test_normalise_repo_path_strips_quotes_and_dot_slash(self):
        """git-quoted and Key-Files paths normalise to the same value."""
        assert audit_runner._normalise_repo_path("./src/a.py") == "src/a.py"
        assert audit_runner._normalise_repo_path("`src/a.py`") == "src/a.py"
        assert audit_runner._normalise_repo_path('"src/a b.py"') == "src/a b.py"
        assert audit_runner._normalise_repo_path("  src/a.py  ") == "src/a.py"
        assert audit_runner._normalise_repo_path("") == ""

    def test_extract_comment_commit_hashes_dedupes_and_sorts(self):
        """7-40 char hex tokens in comments become candidate commit shas."""
        comments = [
            {"comment": "SA-1: abc1234 done"},
            {"comment": "commit DEADBEEF1234567890abcdef1234567890abcdef"},
            {"comment": "no sha here"},
            {"comment": "abc1234 repeated"},
        ]
        assert audit_runner._extract_comment_commit_hashes(comments) == [
            "abc1234",
            "deadbeef1234567890abcdef1234567890abcdef",
        ]

    def test_extract_comment_commit_hashes_empty_for_no_comments(self):
        assert audit_runner._extract_comment_commit_hashes(None) == []
        assert audit_runner._extract_comment_commit_hashes([]) == []

    def test_parse_porcelain_path_plain_and_rename(self):
        """Porcelain worktree entries yield the path (rename → destination)."""
        assert audit_runner._parse_porcelain_path(" M src/a.py") == "src/a.py"
        assert audit_runner._parse_porcelain_path("?? src/new.py") == "src/new.py"
        assert audit_runner._parse_porcelain_path("R  old.py -> new.py") == "new.py"

    def test_resolve_touched_files_unions_grep_comments_and_key_files(self):
        """AC1/AC4: the set is the union of grep, comment, and Key-Files paths."""
        repo = _FakeGitRepo(
            grep_files=["src/a.py", "src/b.py"],
            comment_commits={"c" * 40: ["src/c.py"]},
        )
        desc = "## Acceptance Criteria\n- AC1\n\n## Key Files\n- `src/d.py`"
        paths = audit_runner._resolve_touched_files(
            repo, "TEST-1", work_item={"description": desc},
            comments=[{"comment": f"commit {'c' * 40}"}],
        )
        assert paths == ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    def test_resolve_touched_files_sorted_and_deduped(self):
        """Paths are normalised, de-duplicated, and sorted."""
        repo = _FakeGitRepo(grep_files=["src/b.py", "./src/a.py", "src/a.py"])
        paths = audit_runner._resolve_touched_files(
            repo, "TEST-1",
            work_item={"description": "## Key Files\n- `src/a.py`"},
        )
        assert paths == ["src/a.py", "src/b.py"]

    def test_resolve_touched_files_ignores_comments_when_not_supplied(self):
        """Comment hashes are only used when the caller supplies the comments
        (determinism between audit-time and check-time computation)."""
        repo = _FakeGitRepo(
            grep_files=["src/a.py"],
            comment_commits={"c" * 40: ["src/c.py"]},
        )
        paths = audit_runner._resolve_touched_files(
            repo, "TEST-1", work_item={"description": ""},
        )
        assert paths == ["src/a.py"]

    def test_resolve_touched_files_fail_open_on_git_failure(self):
        """AC4: a failing git log → None (caller re-runs the pipeline)."""
        repo = _FakeGitRepo(grep_rc=128)
        assert audit_runner._resolve_touched_files(
            repo, "TEST-1", work_item={"description": ""},
        ) is None

    def test_resolve_touched_files_fail_open_on_runner_exception(self):
        """AC4: a runner that raises (git missing) → None."""
        def _boom(cmd):
            raise OSError("git not found")

        assert audit_runner._resolve_touched_files(
            _boom, "TEST-1", work_item={"description": ""},
        ) is None

    def test_resolve_touched_files_fail_open_when_nothing_recorded(self):
        """AC4: no grep files, no comments, no Key Files → None.

        Returning an empty set would make the fingerprint blind to any file
        change, so the resolver must fail stale instead.
        """
        repo = _FakeGitRepo(grep_files=[])
        assert audit_runner._resolve_touched_files(
            repo, "TEST-1", work_item={"description": "no key files here"},
        ) is None

    def test_resolve_touched_files_bad_comment_sha_is_swallowed(self):
        """A comment sha that does not resolve does not fail the resolver
        when the grep/Key-Files sources still determine the set."""
        def _runner(cmd):
            joined = " ".join(str(c) for c in cmd)
            if joined.startswith("git log --all"):
                return _proc(0, f"{_COMMIT_MARKER}{'h' * 40}\nsrc/a.py\n")
            if joined.startswith("git log --no-walk"):
                return _proc(128, "")  # bad object → best-effort source fails
            raise AssertionError(joined)

        assert audit_runner._resolve_touched_files(
            _runner, "TEST-1", work_item={"description": ""},
            comments=[{"comment": "0" * 40}],
        ) == ["src/a.py"]

    def test_compute_path_fingerprints_captures_head_and_worktree(self):
        """Per-path state carries the HEAD blob and the narrowed worktree state."""
        repo = _FakeGitRepo(
            blobs={"src/a.py": "blob-a", "src/b.py": "blob-b"},
            status=[" M src/a.py", " M src/unrelated.py"],
        )
        states = audit_runner._compute_path_fingerprints(repo, ["src/a.py", "src/b.py"])
        assert states is not None
        assert states["src/a.py"]["head"] == "blob-a"
        assert "M src/a.py" in states["src/a.py"]["worktree"]
        assert states["src/b.py"] == {"head": "blob-b", "worktree": ""}
        # Unrelated dirty files never enter the payload.
        assert "src/unrelated.py" not in states

    def test_compute_path_fingerprints_falls_back_for_untracked_paths(self):
        """A path absent at HEAD uses its latest touching commit (else "")."""
        repo = _FakeGitRepo(
            blobs={},
            touched_commits={"src/new.py": "c" * 40},
            status=["?? src/never.py"],
        )
        states = audit_runner._compute_path_fingerprints(
            repo, ["src/new.py", "src/never.py"],
        )
        assert states is not None
        assert states["src/new.py"]["head"] == "c" * 40
        assert states["src/never.py"]["head"] == ""

    def test_compute_path_fingerprints_fail_open_on_worktree_failure(self):
        """AC4: a failing status/diff call → None (fail open)."""
        repo = _FakeGitRepo(worktree_rc=1)
        assert audit_runner._compute_path_fingerprints(repo, ["src/a.py"]) is None

    def test_compute_path_fingerprints_empty_paths_returns_none(self):
        assert audit_runner._compute_path_fingerprints(_FakeGitRepo(), []) is None


class TestContentFreshnessGate:
    """Tests for the per-touched-file content freshness gate
    (SA-0MSPZDALB000S18P AC1-AC7).

    The gate fingerprints the work item's OWN files (not the whole repo), so
    re-auditing an item whose touched files, description, and Key Files are
    unchanged returns the existing report in seconds — even when unrelated
    commits or working-tree changes happened elsewhere. A change to any
    touched file (committed or uncommitted) or to the item's description /
    Key Files invalidates freshness. Legacy audits without a fingerprint fall
    back to the 60s time floor.
    """

    _DESC = "## Acceptance Criteria\n- AC1: do the thing\n\n## Key Files\n- `src/a.py`"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo(self, **kwargs) -> _FakeGitRepo:
        """Build a repo touching ``src/a.py`` by default."""
        defaults: dict = {
            "grep_files": ["src/a.py"], "blobs": {"src/a.py": "blob-a"},
        }
        defaults.update(kwargs)
        return _FakeGitRepo(**defaults)

    def _fp(self, repo, desc=None, comments=None) -> str | None:
        """Compute the fingerprint against *repo* for the default item."""
        return audit_runner._compute_content_fingerprint(
            repo, "TEST-1",
            work_item={"description": desc if desc is not None else self._DESC},
            comments=comments,
        )

    def _make_run_wl(self, audit_raw=None, audit_audited_at="2026-08-01T00:00:00.000Z",
                     work_item_desc=_DESC, work_item_updated_at="2026-07-01T00:00:00.000Z"):
        """Build a ``_run_wl`` fake returning a stored audit + work item."""
        def _fake(runner, cmd, worklog_dir=None):
            cmd_str = " ".join(cmd)
            if "audit-show" in cmd_str:
                return {
                    "success": True,
                    "audit": {
                        "auditedAt": audit_audited_at,
                        "rawOutput": audit_raw or "",
                    },
                }
            if "show" in cmd_str and "--children" not in cmd_str:
                return {
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "description": work_item_desc,
                        "updatedAt": work_item_updated_at,
                    },
                }
            raise AssertionError(f"unexpected wl command: {cmd_str}")

        return _fake

    def _report_with_fingerprint(self, fingerprint, verdict="Yes"):
        """Assemble a report body carrying a fingerprint line."""
        return (
            f"Ready to close: {verdict}\n\n"
            f"Audit report for work item TEST-1\n\n"
            f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
            "## Summary\nAll criteria acceptable."
        )

    def _check(self, run_wl_fake, repo):
        """Run _check_audit_freshness with mocked wl and a fake git repo."""
        with mock.patch.object(audit_runner, "_run_wl", side_effect=run_wl_fake):
            return audit_runner._check_audit_freshness(repo, "TEST-1")

    # ------------------------------------------------------------------
    # Fingerprint computation — AC1: unrelated repo state is ignored
    # ------------------------------------------------------------------

    def test_fingerprint_stable_for_unrelated_commit(self):
        """AC1: a commit that changes only unrelated files (different HEAD,
        untouched files) leaves the fingerprint unchanged."""
        f1 = self._fp(self._repo(head_sha="a" * 40))
        f2 = self._fp(self._repo(head_sha="b" * 40))
        assert f1 is not None and len(f1) == 64
        assert f1 == f2

    def test_fingerprint_ignores_unrelated_dirty_files(self):
        """AC1: a dirty file outside the touched set does not invalidate."""
        f_clean = self._fp(self._repo(status=[]))
        f_unrelated = self._fp(self._repo(status=[" M src/unrelated.py"]))
        assert f_clean == f_unrelated

    def test_fingerprint_does_not_query_bare_head(self):
        """AC1: the whole-repo HEAD sha is no longer a fingerprint input."""
        repo = self._repo()
        self._fp(repo)
        assert ["git", "rev-parse", "HEAD"] not in [
            [str(a) for a in call] for call in repo.calls
        ]

    # ------------------------------------------------------------------
    # Fingerprint computation — AC2: touched-file changes invalidate
    # ------------------------------------------------------------------

    def test_fingerprint_changes_when_touched_file_committed_change(self):
        """AC2: a new commit changing a touched file changes its blob hash."""
        f1 = self._fp(self._repo(blobs={"src/a.py": "blob-a"}))
        f2 = self._fp(self._repo(blobs={"src/a.py": "blob-b"}))
        assert f1 != f2

    def test_fingerprint_changes_when_touched_file_dirty(self):
        """AC2: an uncommitted change to a touched file invalidates."""
        f_clean = self._fp(self._repo(status=[]))
        f_dirty = self._fp(self._repo(status=[" M src/a.py"]))
        assert f_clean != f_dirty

    def test_fingerprint_changes_when_touched_staged_change(self):
        """AC2: a staged change reported by git diff --name-only counts too."""
        f_clean = self._fp(self._repo(diff=[]))
        f_staged = self._fp(self._repo(diff=["src/a.py"]))
        assert f_clean != f_staged

    def test_fingerprint_stable_for_untouched_tree(self):
        """AC2 (control): an untouched touched-file set keeps its fingerprint."""
        assert self._fp(self._repo()) == self._fp(self._repo())

    # ------------------------------------------------------------------
    # Fingerprint computation — AC3: description / Key Files invalidate
    # ------------------------------------------------------------------

    def test_fingerprint_changes_with_description(self):
        """AC3: a different description (ACs) yields a different fingerprint."""
        f1 = self._fp(self._repo(), desc=self._DESC)
        f2 = self._fp(self._repo(), desc=self._DESC + "\n- AC2: more")
        assert f1 != f2

    def test_fingerprint_changes_with_key_files(self):
        """AC3: a different Key Files list yields a different fingerprint."""
        f1 = self._fp(self._repo(), desc=self._DESC)
        f2 = self._fp(self._repo(), desc=self._DESC + "\n- `src/b.py`")
        assert f1 != f2

    # ------------------------------------------------------------------
    # Fingerprint computation — AC4: fail-open
    # ------------------------------------------------------------------

    def test_fingerprint_none_when_git_unavailable(self):
        """AC4: git log failure → no fingerprint (pipeline re-runs)."""
        assert self._fp(self._repo(grep_rc=128)) is None

    def test_fingerprint_none_when_no_touched_files(self):
        """AC4: no recorded commits and no Key Files → no fingerprint."""
        repo = _FakeGitRepo(grep_files=[])
        assert audit_runner._compute_content_fingerprint(
            repo, "TEST-1", work_item={"description": "no key files"},
        ) is None

    def test_fingerprint_none_when_worktree_query_fails(self):
        """AC4: a failing status/diff call → no fingerprint (pipeline re-runs)."""
        assert self._fp(self._repo(worktree_rc=1)) is None

    # ------------------------------------------------------------------
    # Fingerprint computation — AC5: tool artefacts are always-invalidating
    # ------------------------------------------------------------------

    def test_tool_artefact_rewrite_invalidates(self):
        """AC5: a touched Unity ProjectSettings artefact has NO ignore-list
        exemption — a rewrite is treated as a genuine change (fail-stale)."""
        desc = "## Key Files\n- `ProjectSettings/ProjectSettings.asset`"
        f_clean = audit_runner._compute_content_fingerprint(
            _FakeGitRepo(grep_files=["ProjectSettings/ProjectSettings.asset"],
                         blobs={"ProjectSettings/ProjectSettings.asset": "blob-1"},
                         status=[]),
            "TEST-1", work_item={"description": desc},
        )
        f_rewritten = audit_runner._compute_content_fingerprint(
            _FakeGitRepo(grep_files=["ProjectSettings/ProjectSettings.asset"],
                         blobs={"ProjectSettings/ProjectSettings.asset": "blob-1"},
                         status=[" M ProjectSettings/ProjectSettings.asset"]),
            "TEST-1", work_item={"description": desc},
        )
        assert f_clean != f_rewritten

    # ------------------------------------------------------------------
    # Fingerprint extraction / embedding
    # ------------------------------------------------------------------

    def test_extract_fingerprint_roundtrip(self):
        """The fingerprint line is extracted back from a stored report."""
        fp = "f" * 64
        report = self._report_with_fingerprint(fp)
        assert audit_runner._extract_content_fingerprint(report) == fp

    def test_extract_fingerprint_none_for_legacy_report(self):
        """Legacy reports without the line yield None (time floor applies)."""
        assert audit_runner._extract_content_fingerprint(
            "Ready to close: Yes\n\n## Summary\nok"
        ) is None

    def test_report_embeds_fingerprint_line(self):
        """The assembled report embeds the fingerprint metadata line."""
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            model="Local Proxy/plan", model_source="local",
            content_fingerprint="f" * 64,
        )
        assert f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{'f' * 64}" in report

    def test_report_without_fingerprint_has_no_line(self):
        """Backward compatibility: no fingerprint → no metadata line."""
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            model="Local Proxy/plan", model_source="local",
        )
        assert "Audit content fingerprint" not in report

    # ------------------------------------------------------------------
    # Freshness gate decisions (AC1, AC2, AC3, AC4)
    # ------------------------------------------------------------------

    def test_unchanged_fingerprint_skips(self):
        """AC1: unchanged fingerprint → existing report returned (skip)."""
        fp = self._fp(self._repo())
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        assert self._check(run_wl, self._repo()) == report

    def test_unrelated_commit_does_not_reauth(self):
        """AC1/AC7: an unrelated commit at a different HEAD reuses the audit."""
        fp = self._fp(self._repo(head_sha="a" * 40))
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        # Different HEAD + unrelated dirty file → still fresh.
        repo = self._repo(head_sha="b" * 40, status=[" M src/unrelated.py"])
        assert self._check(run_wl, repo) == report

    def test_touched_file_change_reauths(self):
        """AC2: an uncommitted change to a touched file → re-audit (None)."""
        fp = self._fp(self._repo(status=[]))
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        assert self._check(run_wl, self._repo(status=[" M src/a.py"])) is None

    def test_description_changed_reauths(self):
        """AC3: changed description → fingerprint mismatch → re-audit."""
        fp = self._fp(self._repo())
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- AC2: added later"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        assert self._check(run_wl, self._repo()) is None

    def test_key_files_changed_reauths(self):
        """AC3: changed Key Files → fingerprint mismatch → re-audit."""
        fp = self._fp(self._repo())
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- `src/other.py`"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        assert self._check(run_wl, self._repo()) is None

    def test_fingerprint_unavailable_reauths_fail_open(self):
        """AC4: a stored fingerprint that cannot be recomputed → re-audit."""
        fp = self._fp(self._repo())
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        assert self._check(run_wl, self._repo(grep_rc=128)) is None

    def test_ready_no_verdict_not_masked_by_skip(self):
        """AC7: a stored 'Ready to close: No' verdict is returned verbatim."""
        fp = self._fp(self._repo())
        report = self._report_with_fingerprint(fp, verdict="No")
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        result = self._check(run_wl, self._repo())
        assert result == report
        assert "Ready to close: No" in result

    def test_fingerprint_gate_not_blocked_by_recent_update(self):
        """The content gate skips even when updatedAt moved after the audit
        (e.g. a comment added) — the 60s floor only applies to legacy audits."""
        fp = self._fp(self._repo())
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(
            audit_raw=report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-05T00:00:00.000Z",
        )
        assert self._check(run_wl, self._repo()) == report

    def test_comment_recorded_commit_contributes_to_touched_set(self):
        """A commit hash recorded in the item's comments contributes its files.

        The gate must forward the caller-supplied comments to the resolver so
        the fingerprint covers commits whose message lacks the item id.
        """
        repo = _FakeGitRepo(
            grep_files=[],
            comment_commits={"c" * 40: ["src/from_comment.py"]},
            blobs={"src/from_comment.py": "blob-c"},
        )
        comments = [{"comment": f"SA-1: committed as {'c' * 40}"}]
        fp_with = self._fp(repo, desc="", comments=comments)
        fp_without = self._fp(repo, desc="", comments=None)
        assert fp_with is not None
        assert fp_without is None  # no grep files, no Key Files → fail open

    # ------------------------------------------------------------------
    # 60s time floor interaction (legacy audits)
    # ------------------------------------------------------------------

    def test_legacy_audit_without_fingerprint_uses_time_floor(self):
        """Audits without a fingerprint fall back to the 60s time gate —
        fresh when auditedAt > updatedAt + 60s, stale otherwise."""
        legacy_report = "Ready to close: Yes\n\n## Summary\nlegacy audit"
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-02T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:00.000Z",
        )
        assert self._check(run_wl, self._repo()) == legacy_report

        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-02T00:00:00.000Z",
        )
        assert self._check(run_wl, self._repo()) is None

    def test_legacy_audit_persistence_write_within_tolerance_is_fresh(self):
        """SA-0MTHC710X003ORZM: legacy audits whose updatedAt is the runner's
        own persistence write (≤ 30 s after auditedAt) are treated as fresh."""
        legacy_report = "Ready to close: Yes\n\n## Summary\nlegacy audit"
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:10.000Z",
        )
        assert self._check(run_wl, self._repo()) == legacy_report

        # Boundary: exactly 30 s is still fresh (inclusive).
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:30.000Z",
        )
        assert self._check(run_wl, self._repo()) == legacy_report

        # Just past tolerance (31 s) and inside the 60 s buffer → NOT fresh.
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-01T00:00:31.000Z",
        )
        assert self._check(run_wl, self._repo()) is None

    # ------------------------------------------------------------------
    # cmd_issue integration (AC7: --force bypass, skip notice, no model
    # calls on a fresh second audit).
    # ------------------------------------------------------------------

    def _patch_fingerprint(self, touched=None, states=None):
        """Patch the resolver + per-path states for deterministic cmd_issue
        integration tests (no real git queries)."""
        touched = touched if touched is not None else ["src/a.py"]
        states = states if states is not None else {
            "src/a.py": {"head": "blob-a", "worktree": ""},
        }
        return (
            mock.patch.object(
                audit_runner, "_resolve_touched_files", return_value=touched,
            ),
            mock.patch.object(
                audit_runner, "_compute_path_fingerprints", return_value=states,
            ),
        )

    def test_force_bypasses_content_gate(self):
        """AC4: --force bypasses the content gate (fresh audit still runs)."""
        updates = []

        def _make_runner():
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": self._report_with_fingerprint("f" * 64),
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    updates.append(list(cmd))
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                if "--children" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                            "children": [],
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        p_touched, p_states = self._patch_fingerprint()
        with (
            p_touched, p_states,
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={"extracted_text": "[]"},
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value="h" * 40,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=_make_runner(),
            )
        assert rc == 0

    def test_fresh_skip_notice_surfaces_verdict_and_timestamp(self):
        """The fast-path skip notice names the verdict and auditedAt of the
        fresh audit instead of a bare 'still fresh' line."""
        captured = []

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    report = self._report_with_fingerprint(fp, verdict="Yes")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        p_touched, p_states = self._patch_fingerprint()
        with p_touched, p_states:
            fp = audit_runner._compute_content_fingerprint(
                _make_runner("f" * 64), "TEST-1",
                work_item={"description": self._DESC},
            )
        assert fp is not None

        p_touched2, p_states2 = self._patch_fingerprint()
        with (
            p_touched2, p_states2,
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log",
                return_value={"extracted_text": "[]"},
            ),
            mock.patch(
                "builtins.print",
                side_effect=lambda *a, **k: captured.append(
                    " ".join(str(x) for x in a)
                ),
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=_make_runner(fp),
            )
        assert rc == 0
        joined = "\n".join(captured)
        assert "Skipping: audit still fresh" in joined
        assert "Ready to close: Yes" in joined
        assert "2026-08-01T00:00:00.000Z" in joined

    def test_second_audit_unchanged_state_short_circuits_without_model(self):
        """AC7: auditing twice at an unchanged touched-file state reuses the
        fresh audit — the second run exits 0 and never invokes the model.

        This reproduces the NV-0MSP7P2PN009FT9U scenario (stale whole-repo
        HEAD/work-tree gate caused a false re-audit whose persisted verdict
        flipped on model judgement variance)."""
        pi_calls = []

        def _pi(**kwargs):
            pi_calls.append(kwargs)
            raise AssertionError(
                "model must not be invoked when a fresh audit exists "
                f"(call #{len(pi_calls)}: {kwargs})"
            )

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    report = self._report_with_fingerprint(fp, verdict="Yes")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        p_touched, p_states = self._patch_fingerprint()
        with p_touched, p_states:
            fp = audit_runner._compute_content_fingerprint(
                _make_runner("f" * 64), "TEST-1",
                work_item={"description": self._DESC},
            )
        assert fp is not None

        p_touched2, p_states2 = self._patch_fingerprint()
        with (
            p_touched2, p_states2,
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_pi,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=False, runner=_make_runner(fp),
            )
        assert rc == 0
        assert pi_calls == [], "the fresh audit short-circuit must skip the model"

    def test_second_audit_force_still_invokes_model(self):
        """Guard: --force bypasses the short-circuit — the model IS invoked
        even with a matching fresh audit."""
        pi_calls = []

        def _make_runner(fp: str):
            mock_runner = mock.MagicMock()

            def _side_effect(cmd):
                cmd_str = " ".join(cmd)
                if "audit-show" in cmd_str:
                    report = self._report_with_fingerprint(fp, verdict="Yes")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "audit": {
                                "auditedAt": "2026-08-01T00:00:00.000Z",
                                "rawOutput": report,
                            },
                        }),
                        stderr="",
                    )
                if "update" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({"success": True}), stderr="",
                    )
                if "show" in cmd_str and "--children" not in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1",
                                "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                        }),
                        stderr="",
                    )
                if "--children" in cmd_str:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "success": True,
                            "workItem": {
                                "id": "TEST-1", "status": "open",
                                "stage": "plan_complete",
                                "description": self._DESC,
                            },
                            "children": [],
                        }),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"success": True}), stderr="",
                )
            mock_runner.side_effect = _side_effect
            return mock_runner

        def _pi(*args, **kwargs):
            pi_calls.append(kwargs.get("prompt", "")[:50])
            return {"extracted_text": "[]"}

        p_touched, p_states = self._patch_fingerprint()
        with (
            p_touched, p_states,
            mock.patch.object(
                audit_runner, "_call_pi_and_maybe_log", side_effect=_pi,
            ),
            mock.patch(
                "code_review.scripts.code_quality.run_code_quality",
                return_value={"success": True, "findings": [], "fixes_applied": 0},
            ),
            mock.patch.object(
                audit_runner, "_resolve_audited_head", return_value="h" * 40,
            ),
        ):
            rc = audit_runner.cmd_issue(
                "TEST-1", persist=False, force=True, runner=_make_runner("f" * 64),
            )
        assert rc == 0
        assert pi_calls, "--force must bypass the fresh-audit short-circuit"
