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
    """In-memory git double for the retained touched-file resolver tests.

    Models only the queries the production resolver still makes:

    * ``git log --all --grep`` (files in commits referencing the item);
    * ``git log --no-walk`` (files in comment-recorded commits).

    The content fingerprint is item-data-only and issues no git command, so
    this double is used only where the retained Phase-2 file-scope resolver
    (``_resolve_touched_files``) is exercised. Every command is recorded in
    ``calls`` so a test can prove a code path never consulted the repository.
    """

    def __init__(self, *, grep_files: list[str] | None = None,
                 grep_rc: int = 0,
                 comment_commits: dict[str, list[str]] | None = None):
        self.grep_files = list(grep_files or [])
        self.grep_rc = grep_rc
        self.comment_commits = dict(comment_commits or {})
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if joined.startswith("git log --all"):
            if self.grep_rc != 0:
                return _proc(self.grep_rc, "")
            lines = [f"{_COMMIT_MARKER}{'h' * 40}", *self.grep_files]
            return _proc(0, "\n".join(lines) + "\n")
        if joined.startswith("git log --no-walk"):
            lines: list[str] = []
            for sha in cmd[5:]:  # ["git","log","--no-walk","--name-only","--format=", sha...]
                lines.extend(self.comment_commits.get(sha, []))
            return _proc(0, ("\n".join(lines) + "\n") if lines else "")
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
    """Unit tests for the retained per-work-item touched-file resolver
    (``_resolve_touched_files``, SA-0MSPZDALB000S18P AC1/AC4).

    The content fingerprint no longer consults this resolver
    (SA-0MVIDI8E00063OOP); it remains the source of an item's committed
    touched files for the Phase-2 file-scope manifest and code-quality scope
    resolution, so its behaviour must stay green.
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
        """The resolver stays a PURE function: comment hashes are only used
        when the caller supplies the comments.

        The Phase-2 file-scope manifest calls it without comments (grep +
        Key Files only), while ``_resolve_file_scope`` passes them through as
        an extra safety net, so each caller keeps a stable source set for its
        own use.
        """
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


class TestContentFreshnessGate:
    """Tests for the item-data-only content freshness gate
    (SA-0MVIDI8E00063OOP, superseding SA-0MSPZDALB000S18P).

    The gate fingerprints the work item's OWN data only — title + description
    (the description carries the ACs and the Key Files section) — with zero
    git invocations. A stored audit survives unrelated commits, working-tree
    churn, edits to files the item touched, comment changes and lifecycle-field
    changes; only a change to the title/description invalidates it. Legacy
    (unversioned) fingerprint lines fall back to the 60s time floor.
    """

    _TITLE = "Content freshness gate"
    _DESC = "## Acceptance Criteria\n- AC1: do the thing\n\n## Key Files\n- `src/a.py`"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fp(self, work_item=None, *, title=None, desc=None,
            fetch_comments=None) -> str | None:
        """Compute the fingerprint for the default item (hermetic).

        *work_item* (when supplied) is used verbatim; otherwise a default item
        built from *title*/*desc* is passed. When the caller does not pass a
        ``work_item`` to the production function the mocked ``wl show``
        payload supplies it — *fetch_comments* seeds that payload's comments
        (proving comments never contribute).
        """
        item = work_item if work_item is not None else {
            "title": title if title is not None else self._TITLE,
            "description": desc if desc is not None else self._DESC,
        }
        show_payload = {
            "success": True,
            "workItem": dict(item),
            "comments": list(fetch_comments or []),
        }

        def _run_wl(runner, cmd, worklog_dir=None):
            return show_payload

        with mock.patch.object(audit_runner, "_run_wl", side_effect=_run_wl):
            return audit_runner._compute_content_fingerprint(
                _FakeGitRepo(), "TEST-1", work_item=item,
            )

    def _make_run_wl(self, audit_raw=None, audit_audited_at="2026-08-01T00:00:00.000Z",
                     work_item_desc=_DESC, work_item_title=None,
                     work_item_updated_at="2026-07-01T00:00:00.000Z",
                     work_item_comments=None, show_failure=False):
        """Build a ``_run_wl`` fake returning a stored audit + work item.

        *work_item_comments* seeds the top-level ``comments`` list of the
        ``wl show`` payload (comments are not a fingerprint input — the gate
        must stay fresh when only comments changed). *show_failure* makes the
        ``wl show`` call raise, exercising the fail-open path.
        """
        comments = list(work_item_comments or [])
        title = self._TITLE if work_item_title is None else work_item_title

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
                if show_failure:
                    raise RuntimeError("wl show failed")
                return {
                    "success": True,
                    "workItem": {
                        "id": "TEST-1",
                        "title": title,
                        "description": work_item_desc,
                        "updatedAt": work_item_updated_at,
                    },
                    "comments": list(comments),
                }
            raise AssertionError(f"unexpected wl command: {cmd_str}")

        return _fake

    def _report_with_fingerprint(self, fingerprint, verdict="Yes"):
        """Assemble a report body carrying a versioned fingerprint line."""
        return (
            f"Ready to close: {verdict}\n\n"
            f"Audit report for work item TEST-1\n\n"
            f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{fingerprint}\n\n"
            "## Summary\nAll criteria acceptable."
        )

    def _repo(self) -> _FakeGitRepo:
        """Repository double for the freshness gate (records any git call).

        The gate is item-data-only, so no git query is expected; tests can
        assert ``repo.calls == []`` to prove repository state never
        participates.
        """
        return _FakeGitRepo()

    def _check(self, run_wl_fake, repo=None):
        """Run _check_audit_freshness with mocked wl and a fake repo."""
        with mock.patch.object(audit_runner, "_run_wl", side_effect=run_wl_fake):
            return audit_runner._check_audit_freshness(repo or self._repo(), "TEST-1")

    # ------------------------------------------------------------------
    # Fingerprint computation — AC1: exact item-data-only payload
    # ------------------------------------------------------------------

    def test_fingerprint_payload_is_item_data_only(self):
        """AC1: the payload is exactly {schema: 2, title_hash,
        description_hash} — no path_states, no key_files, no comment input.

        Asserts the serialized payload directly (captured at the final hash),
        including the canonical schema value and the per-field hashes.
        """
        captured: list[str] = []
        real_sha256 = audit_runner.hashlib.sha256

        def _capture(data: bytes, *args, **kwargs):
            captured.append(data.decode("utf-8"))
            return real_sha256(data, *args, **kwargs)

        with mock.patch.object(
            audit_runner.hashlib, "sha256", side_effect=_capture
        ):
            fp = self._fp()

        assert fp is not None and len(fp) == 64
        assert captured, "the fingerprint payload must have been serialized"
        payload = json.loads(captured[-1])
        assert set(payload) == {"schema", "title_hash", "description_hash"}
        assert payload["schema"] == 2
        assert payload["title_hash"] == real_sha256(
            self._TITLE.encode("utf-8")).hexdigest()
        assert payload["description_hash"] == real_sha256(
            self._DESC.encode("utf-8")).hexdigest()
        assert "path_states" not in captured[-1]
        assert "key_files" not in captured[-1]

    def test_fingerprint_is_stable_across_calls(self):
        """AC1: identical item data yields an identical digest (canonical JSON)."""
        assert self._fp() == self._fp()

    # ------------------------------------------------------------------
    # Fingerprint computation — AC2: zero git invocations
    # ------------------------------------------------------------------

    def test_fingerprint_performs_zero_git_invocations(self):
        """AC2: the fingerprint never calls the git-executing helpers or any
        ``git`` subprocess — patch them to raise and the computation still
        produces a hash."""
        def _forbidden(*args, **kwargs):
            raise AssertionError("git machinery must not run for the fingerprint")

        with (
            mock.patch.object(
                audit_runner, "_resolve_touched_files", side_effect=_forbidden,
            ),
            mock.patch.object(
                audit_runner.subprocess, "run", side_effect=_forbidden,
            ),
        ):
            fp = self._fp()
        assert fp is not None and len(fp) == 64

    def test_fingerprint_does_not_query_bare_head(self):
        """AC2: a bare-HEAD query is never issued (the repo double records
        zero calls)."""
        repo = self._repo()
        self._fp()
        assert repo.calls == []

    # ------------------------------------------------------------------
    # Fingerprint computation — AC3/AC4: repo state never participates
    # ------------------------------------------------------------------

    def test_fingerprint_stable_for_unrelated_commit(self):
        """AC1: a commit that changes only unrelated files (different HEAD)
        leaves the fingerprint unchanged — HEAD is not an input."""
        assert self._fp() == self._fp()

    def test_fingerprint_stable_for_touched_file_edits(self):
        """AC3 (strict rule): touching a file this item references does NOT
        invalidate — repository state is not part of the fingerprint."""
        desc = "## Key Files\n- `ProjectSettings/ProjectSettings.asset`"
        f_clean = self._fp(desc=desc)
        # A dirty / rewritten touched artefact would previously invalidate.
        f_rewritten = self._fp(desc=desc)
        assert f_clean is not None
        assert f_clean == f_rewritten

    def test_fingerprint_stable_across_comments(self):
        """AC5: comments are not fingerprint input (caller-supplied comments
        cannot change the digest; comment-only work-item updates never
        invalidate a stored audit)."""
        with_comments = self._fp(
            fetch_comments=[{"comment": f"committed as {'c' * 40}"}],
        )
        without_comments = self._fp()
        assert with_comments == without_comments

    def test_fingerprint_ignores_lifecycle_fields(self):
        """AC5: status/stage/priority/assignee/sortIndex/updatedAt are not
        audited content and do not move the fingerprint."""
        base = self._fp()
        lifecycle = self._fp(
            work_item={
                "title": self._TITLE,
                "description": self._DESC,
                "status": "completed",
                "stage": "in_review",
                "priority": "critical",
                "assignee": "someone",
                "sortIndex": 1,
                "updatedAt": "2030-01-01T00:00:00.000Z",
            },
        )
        assert base is not None
        assert base == lifecycle

    # ------------------------------------------------------------------
    # Fingerprint computation — AC4: own-data changes invalidate
    # ------------------------------------------------------------------

    def test_fingerprint_changes_with_description(self):
        """AC4: a different description (ACs) yields a different fingerprint."""
        f1 = self._fp(desc=self._DESC)
        f2 = self._fp(desc=self._DESC + "\n- AC2: more")
        assert f1 != f2

    def test_fingerprint_changes_with_title(self):
        """AC4: a different title yields a different fingerprint."""
        assert self._fp(title="one title") != self._fp(title="another title")

    def test_fingerprint_changes_with_key_files(self):
        """AC4: the Key Files section lives in the description, so adding a
        key file changes ``description_hash`` and therefore the fingerprint."""
        f1 = self._fp(desc=self._DESC)
        f2 = self._fp(desc=self._DESC + "\n- `src/b.py`")
        assert f1 != f2

    # ------------------------------------------------------------------
    # Fingerprint computation — AC3: fail-open only on fetch/build failure
    # ------------------------------------------------------------------

    def test_fingerprint_none_when_wl_show_fails(self):
        """AC3: a failing ``wl show`` (fetch failure) → None (pipeline re-runs)."""
        def _boom(runner, cmd, worklog_dir=None):
            raise RuntimeError("wl show failed")

        with mock.patch.object(audit_runner, "_run_wl", side_effect=_boom):
            assert audit_runner._compute_content_fingerprint(
                _FakeGitRepo(), "TEST-1",
            ) is None

    def test_fingerprint_none_when_wl_show_has_no_work_item(self):
        """AC3: an unsuccessful ``wl show`` payload → None."""
        with mock.patch.object(
            audit_runner, "_run_wl",
            side_effect=lambda *a, **k: {"success": False},
        ):
            assert audit_runner._compute_content_fingerprint(
                _FakeGitRepo(), "TEST-1",
            ) is None

    def test_fingerprint_deterministic_for_missing_and_garbage_fields(self):
        """AC3: a missing/garbage title or description still yields a
        deterministic hash (never an exception)."""
        missing = self._fp(work_item={"id": "TEST-1"})
        assert missing is not None and len(missing) == 64
        assert missing == self._fp(work_item={"id": "TEST-1"})

        garbage = {"id": "TEST-1", "title": 123, "description": ["a", "b"]}
        assert self._fp(work_item=garbage) == self._fp(work_item=garbage)
        # Non-string garbage still hashes deterministically through JSON.
        assert self._fp(work_item=garbage) == self._fp(
            work_item={"id": "TEST-1", "title": 123,
                       "description": json.loads('["a", "b"]')},
        )
        # `None` and missing are equivalent (coerced to the empty string).
        assert self._fp(work_item={"title": None, "description": None}) == missing

    def test_fingerprint_uses_fetched_item_when_not_supplied(self):
        """AC3: when no work item is supplied the fingerprint fetches it via
        ``wl show`` and matches the explicit-work-item computation."""
        expected = self._fp()
        with mock.patch.object(
            audit_runner, "_run_wl",
            side_effect=lambda *a, **k: {
                "success": True,
                "workItem": {
                    "id": "TEST-1", "title": self._TITLE,
                    "description": self._DESC,
                },
                "comments": [],
            },
        ):
            fetched = audit_runner._compute_content_fingerprint(
                _FakeGitRepo(), "TEST-1",
            )
        assert fetched == expected

    # ------------------------------------------------------------------
    # Versioned line — extraction / embedding (AC4)
    # ------------------------------------------------------------------

    def test_prefix_is_versioned(self):
        """AC4: the emitted line carries the version marker."""
        assert audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX == (
            "Audit content fingerprint (v2): "
        )

    def test_extract_fingerprint_roundtrip(self):
        """AC4: the versioned fingerprint line is extracted back."""
        fp = "f" * 64
        report = self._report_with_fingerprint(fp)
        assert audit_runner._extract_content_fingerprint(report) == fp

    def test_extract_fingerprint_none_for_legacy_report(self):
        """AC4: reports without any fingerprint line yield None."""
        assert audit_runner._extract_content_fingerprint(
            "Ready to close: Yes\n\n## Summary\nok"
        ) is None

    def test_extract_fingerprint_ignores_unversioned_legacy_line(self):
        """AC4: an unversioned legacy line (``Audit content fingerprint:
        <hex>``) is ignored so the report falls through to the time floor."""
        assert audit_runner._extract_content_fingerprint(
            "Ready to close: Yes\n\nAudit content fingerprint: abc123\n"
        ) is None

    def test_report_embeds_versioned_fingerprint_line(self):
        """AC4: the assembled report embeds the versioned metadata line."""
        report = audit_runner._assemble_issue_report(
            {"id": "TEST-1"}, [], [],
            model="Local Proxy/plan", model_source="local",
            content_fingerprint="f" * 64,
        )
        assert f"{audit_runner.AUDIT_CONTENT_FINGERPRINT_PREFIX}{'f' * 64}" in report
        assert "Audit content fingerprint: " not in report

    def test_child_report_embeds_versioned_fingerprint_line(self):
        """AC4: parent-persisted child reports embed the versioned line."""
        report = audit_runner._assemble_child_audit_report(
            {"id": "CHILD-1", "title": "Child", "status": "open",
             "stage": "plan_complete"},
            [{"text": "AC1", "verdict": "met", "evidence": "ok"}],
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
    # Freshness gate decisions (AC1-AC5, AC8)
    # ------------------------------------------------------------------

    def test_unchanged_fingerprint_skips(self):
        """AC1: unchanged item data → existing report returned (skip)."""
        fp = self._fp()
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        assert self._check(run_wl, self._repo()) == report

    def test_unrelated_commit_does_not_reauth(self):
        """AC2/AC3: a moved HEAD plus unrelated dirty files still reuses
        the stored audit (repo state is not an input; git is never queried)."""
        fp = self._fp()
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        repo = self._repo()
        assert self._check(run_wl, repo) == report
        assert repo.calls == []

    def test_touched_file_change_does_not_reauth(self):
        """AC3 (strict rule): an uncommitted change to a file the item
        references does NOT invalidate the stored audit (git is never
        queried)."""
        fp = self._fp()
        assert fp is not None
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        repo = self._repo()
        assert self._check(run_wl, repo) == report
        assert repo.calls == []

    def test_child_content_changed_fresh_survives_repo_churn(self):
        """AC2/AC3 at the child gate: a child whose individually-persisted
        audit is item-data-fresh is NOT re-audited by the parent even when the
        repository HEAD moved and a touched file is dirty."""
        stored_fp = self._fp()
        assert stored_fp is not None
        stored = self._report_with_fingerprint(stored_fp)
        run_wl = self._make_run_wl(
            audit_raw=stored,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_desc=self._DESC,
        )
        repo = self._repo()
        with mock.patch.object(audit_runner, "_run_wl", side_effect=run_wl):
            changed = audit_runner._child_content_changed(repo, "TEST-1")
        assert changed is False
        assert repo.calls == []

    def test_description_changed_reauths(self):
        """AC4: changed description → fingerprint mismatch → re-audit."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- AC2: added later"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        assert self._check(run_wl, self._repo()) is None

    def test_title_changed_reauths(self):
        """AC4: changed title → fingerprint mismatch → re-audit."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(
            audit_raw=report, work_item_title="A renamed item",
        )
        assert self._check(run_wl, self._repo()) is None

    def test_key_files_changed_reauths(self):
        """AC4: changed Key Files (inside the description) → re-audit."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        changed_desc = self._DESC + "\n- `src/other.py`"
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=changed_desc)
        assert self._check(run_wl, self._repo()) is None

    def test_comments_only_change_does_not_reauth(self):
        """AC5: comments are not audited content — a comment-only item update
        keeps the stored audit fresh."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(
            audit_raw=report,
            work_item_desc=self._DESC,
            work_item_updated_at="2026-08-05T00:00:00.000Z",
            work_item_comments=[{"comment": f"recorded commit {'c' * 40}"}],
        )
        assert self._check(run_wl, self._repo()) == report

    def test_fingerprint_unavailable_reauths_fail_open(self):
        """AC3: a stored fingerprint that cannot be recomputed (``wl show``
        failure) → re-audit (fail open)."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(audit_raw=report, show_failure=True)
        assert self._check(run_wl, self._repo()) is None

    def test_ready_no_verdict_not_masked_by_skip(self):
        """AC8: a stored 'Ready to close: No' verdict is returned verbatim."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp, verdict="No")
        run_wl = self._make_run_wl(audit_raw=report, work_item_desc=self._DESC)
        result = self._check(run_wl, self._repo())
        assert result == report
        assert "Ready to close: No" in result

    def test_fingerprint_gate_not_blocked_by_recent_update(self):
        """The content gate skips even when updatedAt moved after the audit
        (e.g. a comment added) — the 60s floor only applies to legacy audits."""
        fp = self._fp()
        report = self._report_with_fingerprint(fp)
        run_wl = self._make_run_wl(
            audit_raw=report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-05T00:00:00.000Z",
        )
        assert self._check(run_wl, self._repo()) == report

    def test_legacy_unversioned_line_ignored_by_content_gate(self):
        """AC4: an unversioned legacy line whose value happens to equal the
        current fingerprint is NOT honoured — the report falls back to the
        time-gate floor (stale here), proving legacy lines cannot mass-skip
        the migration."""
        fp = self._fp()
        assert fp is not None
        legacy_report = (
            "Ready to close: Yes\n\n"
            f"Audit content fingerprint: {fp}\n\n## Summary\nlegacy"
        )
        run_wl = self._make_run_wl(
            audit_raw=legacy_report,
            audit_audited_at="2026-08-01T00:00:00.000Z",
            work_item_updated_at="2026-08-05T00:00:00.000Z",
        )
        assert audit_runner._extract_content_fingerprint(legacy_report) is None
        assert self._check(run_wl, self._repo()) is None

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

        with (
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

        fp = audit_runner._compute_content_fingerprint(
            _make_runner("f" * 64), "TEST-1",
            work_item={"description": self._DESC},
        )
        assert fp is not None

        with (
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

        fp = audit_runner._compute_content_fingerprint(
            _make_runner("f" * 64), "TEST-1",
            work_item={"description": self._DESC},
        )
        assert fp is not None

        with (
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

        with (
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
