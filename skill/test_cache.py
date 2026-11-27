"""Per-repo cache for full test-suite runs.

The test skill, ship release gates, and implement loops repeatedly execute the
same full suite at the same git state within minutes (observed: ``npm test``
ran twice back-to-back, 405s then 101s, purely to grep summary lines). This
module adds a caching layer so repeated verification is cheap:

- **Per-repo, gitignored storage**: ``<repo>/.worklog/cache/`` when a
  ``.worklog`` directory exists, otherwise ``<repo>/.git/test-cache/``
  (resolved worktree-aware via ``git rev-parse --git-dir``). Non-git repos
  fall back to ``<repo>/.test-cache/``.
- **Keyed by normalized command + git-state fingerprint**: the fingerprint is
  HEAD sha plus a content hash of every path ``git status --porcelain``
  reports as dirty (tracked modifications, staged changes, deletions and
  untracked files), resolved against the repo containing the working
  directory (worktree-aware). Hashing file **contents** — not just the
  status text, which lists paths only — means editing a file that is
  *already* dirty yields a new fingerprint too (AH-0MVFGIV6700329SZ). The
  fingerprint always describes the repository containing the working
  directory: repository-override variables (``GIT_DIR``, ``GIT_WORK_TREE``,
  ``GIT_CONFIG*``, …) are scrubbed before every git call, so a leaked value
  cannot make the cache describe a different repository
  (SA-0MUIA3OE40001QJX).
- **Fail closed on an indeterminate state**: when the state cannot be
  determined — a failing/locked/corrupt index, a git timeout under load, an
  unreadable dirty path — the run is *not* looked up and *not* stored. A
  degraded, state-independent fallback fingerprint would make every dirty
  tree share one key and serve stale green results for the whole TTL
  (WL-0MVPO9P610055UQ3). Correctness first: an unfingerprintable run simply
  executes fresh.
- **Keyed on the post-run state**: the entry records the state the run
  **left behind**, because that is the state every later consumer observes
  (the next run's pre-check, ``query_cached()``, ``--summary``). Keying on
  the pre-run state made a completed run unqueryable whenever transient
  churn (plugin/dist/scratch files) disappeared while it ran
  (WL-0MVPO9P610055UQ3). When the tree moved during the run, the pre-run
  state is still recorded as ``git_state_at_start`` metadata for diagnosis;
  it never keys the entry.
- **2-hour TTL** (configurable): expired entries are re-run and replaced.
  Failed (non-zero-exit) runs use a short 5-minute TTL instead
  (SA-0MSJELL44009XYIL) so transient infra failures are not re-served as
  current results.
- **Correctness first**: a hit requires the git state to match AND the run to
  be within the TTL. Corrupt/unreadable entries degrade to a fresh run and
  never raise into the caller. ``force`` / ``no_cache`` bypass the cache.
- **Mid-run checkout movement is quarantined** (SA-0MW3FF18I003KGZE): the
  guarded checkout's own HEAD + tracked-tree content is snapshotted around
  every executed run; when it moved mid-run (a concurrent ``finish``
  fast-forward rewriting test files under a lazy-importing suite), the mixed
  verdict is discarded — no entry is stored under either state, the run is
  reported non-green with the distinct :data:`STATE_CHANGED_EXIT_CODE` (75,
  ``EX_TEMPFAIL``) and a ``state_changed_during_run`` notice naming both
  commits. Differences fully explained by the start-of-run working-tree
  attribution (another session committing its own start-dirty WIP) never
  flag a run; an unfingerprintable checkout is fail-closed and never
  reported as movement.
- **Read-only query mode**: ``query_cached()`` + ``summary_lines()`` return
  summary lines from a cached run without executing anything (used by the
  ``run_tests.py --summary`` flag and available to read-only consumers such as
  the audit skill — operator-attested path SA-0MSGLAVCZ002LVZ4 and the
  automatic full-suite verification path SA-0MSIU5HFI0024D7W).

Entry layout (stable, documented for read-only consumers)::

    <cache_dir>/<key>/
        metadata.json   {"version":1, "command":..., "git_state":...,
                         "git_state_at_start":...,
                         "scope": "full"|"changed",
                         "test_type": "full"|"unit"|...,
                         "fingerprint_kind": "worktree"|"tree",
                         "interpreter": "<pytest interpreter/exe>",
                         "exit_code":..., "completed_at": <epoch float>}
        stdout.txt      full stdout of the run
        stderr.txt      full stderr of the run

``git_state`` is the key the entry is stored under — the state the run left
behind (see above). ``git_state_at_start`` records the state the run started
in and is informational only (it differs when the tree moved during the run,
which is exactly the transient-churn case that used to make entries
unqueryable); entries predating the field omit it.

``interpreter`` records the concrete interpreter/executable that actually ran
pytest (empty for non-pytest commands and for older entries predating the
field). The cache key stays git-state + command + type — the interpreter is
informational metadata, so an entry produced under a different interpreter is
still visible and auditable rather than silently invalidated
(SA-0MVK154IE007OS3D AC2).

``scope`` records whether the run was a full-suite run (``"full"`` — the
historical default, so older entries without the field read as full) or a
changed-file partial run (``"changed"`` — distinct cache keys from the full
suite anyway, but the field lets read-only consumers such as the audit skill
reject partial runs as full-suite evidence even if command sets ever
converge).

``test_type`` records the typed profile a run belongs to (``"full"`` — the
historical default, so older entries without the field read as full). Only
``test_type == "full"`` populates the audit-accepted full-suite entry; other
types use independent cache keys (the type is part of the key) so a partial
typed run can never satisfy a "full test suite passes" acceptance criterion.

``fingerprint_kind`` records *which kind* of fingerprint keys the entry —
``"worktree"`` (the default: :func:`compute_git_state`, HEAD plus the loose
working-tree contents) or ``"tree"`` (a would-be-commit tree SHA computed by
:func:`compute_tree_state`). It is the entry's provenance: a consumer that asks
for a tree fingerprint is never served an entry recorded for a loose state, and
an entry that predates the field reads as ``"worktree"`` (historical
default), so a tree request misses it and fails closed rather than accepting a
verdict recorded for a differently-identified tree (SA-0MVL2NH1G0094FA2 AC4).

``"change_set"`` is the third kind (SA-0MWFJS9OL001ZTTC): the SHA-256 of the
**complete patch** a change set contributes (:func:`compute_change_set_state`).
A pure rebase rewrites the commit and its tree but not that patch, so a
verdict measured before a lost push race stays valid evidence for the
rebased commit — the patch covers every changed path plus the pre-image and
post-image object ids, so a content change (ours or upstream's, in a file the
patch touches) always produces a different key and misses. Entries of this
kind record ``change_set_base_tree``/``change_set_target_tree`` so a consumer
can state which tree the reused verdict was actually measured on.

Writes are atomic (temp file + ``os.replace``); no locking is used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared.git_sandbox import (
    compare_own_checkout_states,
    git_env_for_repo,
    snapshot_own_checkout,
)
from test_runner import (
    executable_test_command,
    is_pytest_command,
    log_repository_override_scrub,
    normalize_test_command,
    resolve_pytest_executable,
    subprocess_env_with_scrubbed_overrides,
)

LOG = logging.getLogger("skill.test_cache")


class GitStateUnavailable(RuntimeError):
    """The working tree's git state could not be fully determined.

    A cache verdict is only meaningful for a state built from every input
    (HEAD plus the content of every dirty path). Raised by
    :func:`compute_git_state` in strict mode when git cannot report that
    input — a failing/locked/corrupt index, a git timeout under load, a
    repository whose work tree cannot be resolved — or when a dirty path's
    content cannot be read. Callers that can serve a *stale* result
    (:func:`run_cached`, :func:`query_cached`) turn this into "no cache":
    the run executes and stores nothing, because a degraded fingerprint makes
    every dirty tree share one key (WL-0MVPO9P610055UQ3).
    """


#: Fingerprint provenance kinds. ``worktree`` is the historical default (the
#: loose :func:`compute_git_state` fingerprint); ``tree`` is an explicitly
#: supplied would-be-commit tree SHA (:func:`compute_tree_state`);
#: ``change_set`` is the content hash of the patch a change set contributes
#: (:func:`compute_change_set_state`), which survives a pure rebase
#: (SA-0MWFJS9OL001ZTTC).
FINGERPRINT_KIND_WORKTREE = "worktree"
FINGERPRINT_KIND_TREE = "tree"
FINGERPRINT_KIND_CHANGE_SET = "change_set"

#: Domain separator mixed into the change-set hash so a patch hash can never
#: be confused with any other SHA-256 this module computes. Bump the version
#: when the patch-generation flags change: every previously stored
#: change-set entry then misses instead of being matched against a
#: differently-derived key.
CHANGE_SET_FINGERPRINT_VERSION = b"change-set-v1\x00"

#: Full hexadecimal tree/commit object names (40 for SHA-1, 64 for SHA-256).
_OBJECT_NAME_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

#: The distinct non-zero exit status a run whose guarded checkout moved
#: mid-run is reported with (``EX_TEMPFAIL`` — temporary failure, retryable)
#: (SA-0MW3FF18I003KGZE AC1). Exported so the runner and the finish gate share
#: one constant and cannot drift.
STATE_CHANGED_EXIT_CODE = 75

#: Stable machine-readable notice code for a mid-run checkout move. Emitted as
#: ``notice_code`` inside ``state_changed_during_run`` and by the runner's
#: aggregate so gate classification and triage share one vocabulary.
STATE_CHANGED_NOTICE_CODE = "state_changed_during_run"

#: Exit status a bounded suite runner uses to report a wall-clock **hang**
#: (``timeout(1)`` convention; TCE's ``vitest-run-with-retry.ts`` reports it).
#: A hang is not a red suite: it produced no parsable verdict, so it must
#: never destroy a green entry for the same tree (SA-0MWEURY420040OMD).
ABORT_EXIT_CODE = 124

#: Side-car file written next to a preserved green entry to record an abort
#: observation. It lives inside the entry directory so it is mirrored by
#: :func:`mirror_cache_entries` and removed with the entry's TTL expiry.
ABORT_SIDECAR_FILENAME = "abort.json"

#: Bound for the stdout/stderr tail carried on an abort side-car (keeps the
#: side-car small; the full preserved output lives in the green entry).
ABORT_OUTPUT_TAIL_CHARS = 2000


def _no_abort_observation() -> dict[str, Any]:
    """Return the stable "no abort observed" result fields.

    An executed run (or an entry with no abort side-car) reports the same four
    fields as a preserved-green hit so consumers never branch on their absence.
    """
    return {
        "abort_observed": False,
        "abort_exit_code": None,
        "abort_completed_at": None,
        "abort_notice": "",
    }


#: Result-summary markers that prove a non-zero run still produced a parsable
#: verdict (so it is a genuine red run, never an abort). Patterns are anchored
#: to line starts so incidental prose ("0 passed" in a log line) cannot be
#: mistaken for a summary. Covers pytest (``N passed``), Vitest
#: (``Test Files``/``Tests``) and TAP (``# pass``/``not ok``).
_RESULT_SUMMARY_RE = re.compile(
    r"^(?:"
    r"(?:FAILED|ERROR)\s+"
    r"|\d+ (?:passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)"
    r"|Test Files\s+\d"
    r"|Tests\s+\d"
    r"|# (?:pass|fail|tests)\s+\d+"
    r"|not ok\s+\d+"
    r")",
    re.MULTILINE,
)

#: The bounded retry runner's final verdict line (``vitest-run-with-retry``):
#: ``[vitest-runner] attempts=N status=S outcome=O …``. Its presence is a
#: parsable verdict (e.g. ``coverage-failed`` exits non-zero with no failing
#: test), so it makes a non-zero run RED rather than an abort — unless the
#: outcome is ``hang`` or the status is :data:`ABORT_EXIT_CODE`.
_VITEST_RUNNER_VERDICT_RE = re.compile(
    r"^\[vitest-runner\] attempts=\d+ status=(?P<status>\S+) "
    r"outcome=(?P<outcome>[a-z-]+)",
    re.MULTILINE,
)


def _bounded_output_tail(text: str, limit: int = ABORT_OUTPUT_TAIL_CHARS) -> str:
    """Return the last *limit* characters of *text* (empty for falsy input)."""
    if not text:
        return ""
    text = text.rstrip()
    return text if len(text) <= limit else text[-limit:]


def is_abort_result(exit_code: int, stdout: str = "", stderr: str = "") -> bool:
    """Whether a run result is an **abort** rather than a red verdict.

    A hang/abort produced no usable verdict for the tree, so it must not be
    allowed to destroy a green entry for an unchanged tree
    (SA-0MWEURY420040OMD). An abort is:

    * exit :data:`ABORT_EXIT_CODE` (``124``) — the bounded-runner hang;
    * a negative exit code — killed by a signal (OOM kill, worker crash), so
      the verdict is incomplete;
    * any other non-zero exit whose combined output carries **no** parsable
      result marker (pytest summary, Vitest ``Test Files``/``Tests``, TAP
      ``# pass``/``not ok``, or a ``[vitest-runner]`` verdict line).

    A genuine red run always exits non-zero *and* prints a parsable verdict
    (a failing test, or the retry runner's ``coverage-failed`` verdict), so
    it is never classified as an abort. A green run (exit 0) never is either.

    Args:
        exit_code: The command's exit status.
        stdout: The command's captured stdout.
        stderr: The command's captured stderr.

    Returns:
        True when the result is an abort with no usable verdict.
    """
    if exit_code == 0:
        return False
    if exit_code == ABORT_EXIT_CODE or exit_code < 0:
        return True
    combined = f"{stdout}\n{stderr}"
    verdict = _VITEST_RUNNER_VERDICT_RE.search(combined)
    if verdict is not None:
        # A recorded verdict line is a parsable outcome: either an explicit
        # hang (abort) or a genuine red run (e.g. ``coverage-failed``, which
        # exits non-zero with no failing test).
        return verdict.group("outcome") == "hang" or verdict.group(
            "status"
        ) == str(ABORT_EXIT_CODE)
    return _RESULT_SUMMARY_RE.search(combined) is None


def _abort_sidecar_path(entry_dir: Path) -> Path:
    """Return the abort side-car path inside *entry_dir*."""
    return entry_dir / ABORT_SIDECAR_FILENAME


def _read_abort_sidecar(entry_dir: Path) -> dict[str, Any] | None:
    """Read an abort side-car, or None when absent/corrupt/foreign."""
    try:
        data = json.loads(_abort_sidecar_path(entry_dir).read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return None
    return data


def _abort_preservable_green(
    entry_dir: Path,
    *,
    git_state: str,
    scope: str,
    test_type: str,
    fingerprint_kind: str,
    ttl: float,
) -> dict[str, Any] | None:
    """Return the existing entry's metadata when an abort must preserve it.

    A green entry is preserved only when it is for the **same key** (the
    entry directory already encodes command + state + type), records a green
    verdict, matches the caller's identity fields and is within *ttl*. Every
    other case — missing/corrupt/red/expired entry, or a mismatched identity —
    returns None so the abort is stored normally and nothing is ever masked.
    """
    try:
        meta = json.loads((entry_dir / "metadata.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or meta.get("version") != _CACHE_VERSION:
        return None
    if meta.get("git_state") != git_state:
        return None
    if meta.get("scope", "full") != scope:
        return None
    if meta.get("test_type", "full") != test_type:
        return None
    if meta.get("fingerprint_kind", FINGERPRINT_KIND_WORKTREE) != fingerprint_kind:
        return None
    try:
        exit_code = int(meta.get("exit_code", 1))
        completed_at = float(meta.get("completed_at", 0))
    except (TypeError, ValueError):
        return None
    if exit_code != 0:
        return None
    if time.time() - completed_at > ttl:
        return None
    return meta


def _abort_preserved_after_store(
    entry_dir: Path, abort: bool, completed_at: float
) -> bool:
    """Whether *store* preserved a green entry instead of recording *abort*.

    True only when *abort* was stored, the entry still holds a green verdict
    and the abort side-car records this very run (same ``completed_at``). That
    is exactly the case where the caller must report the green as preserved
    rather than freshly written.
    """
    if not abort:
        return False
    try:
        meta = json.loads((entry_dir / "metadata.json").read_text())
        sidecar = json.loads(_abort_sidecar_path(entry_dir).read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict) or not isinstance(sidecar, dict):
        return False
    try:
        green = int(meta.get("exit_code", 1)) == 0
        same_run = float(sidecar.get("completed_at", -1)) == float(completed_at)
    except (TypeError, ValueError):
        return False
    return green and same_run


DEFAULT_TTL_SECONDS = 2 * 60 * 60  # 2 hours (operator decision)
# Non-zero-exit runs get a much shorter TTL (SA-0MSJELL44009XYIL): a failed
# run may reflect a transient infra failure (e.g. /tmp disk quota) and must
# not be re-served as a current result for the full 2h TTL — it expires
# quickly so a later query/run re-executes fresh. Green runs keep the full
# TTL.
FAILED_RUN_TTL_SECONDS = 5 * 60  # 5 minutes
_CACHE_VERSION = 1
_KEY_LENGTH = 32
# Token separated from the command when hashing so that command+state pairs
# cannot collide (e.g. command "a b" + state "c" vs command "a" + state "b c").
_KEY_SEPARATOR = "\x00"

# A runner executes the (normalized) command and returns an object with
# ``stdout``, ``stderr`` and ``returncode`` attributes.
Runner = Callable[[str, str, int], Any]


# ---------------------------------------------------------------------------
# Git-state fingerprint (worktree-aware)
# ---------------------------------------------------------------------------


def _run_git(cwd: str, *args: str) -> str | None:
    """Run a git command in *cwd*, returning stdout or None on failure.

    The environment is :func:`shared.git_sandbox.git_env_for_repo` for *cwd*.
    Repository overrides are scrubbed (SA-0MUIA3OE40001QJX): git honours
    ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_CONFIG*`` over ``cwd``, so a leaked
    value made the fingerprint describe a *different* repository — one
    constant key for every state of the tree being tested, i.e. stale results
    for the whole TTL (WL-0MVPO9P610055UQ3). An ambient ``GIT_INDEX_FILE`` is
    honoured only when it belongs to *cwd*'s own repository: the finish gate
    exports a temporary would-be-commit index for the suite it runs
    (SA-0MVQHVGFR006W8LG), so the repository under test must be fingerprinted
    through it, while a fixture repository that inherited it would see a
    foreign index with no dirty paths and fingerprint every edit identically
    (SA-0MVTET8KQ0031R6Z). The fingerprint always describes the repository
    containing ``cwd``.
    """
    log_repository_override_scrub()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=git_env_for_repo(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _has_git_directory(cwd: str) -> bool:
    """Whether *cwd* or any ancestor carries a ``.git`` directory or file.

    Distinguishes a genuinely non-git directory (documented TTL-only
    fallback) from a git checkout whose state git could not report (which
    must fail closed instead of falling back to a state-independent key).
    """
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate / ".git"):
            return True
    return False


def compute_git_state(cwd: str | Path | None = None, *, strict: bool = False) -> str:
    """Return a fingerprint of the git state at *cwd*.

    The fingerprint combines the HEAD commit sha with the **content** of every
    path the working tree reports as dirty — tracked modifications, staged
    changes, deletions and untracked files alike. ``git status --porcelain``
    alone is not enough: it records paths and status codes only, so a second
    edit to a file that is already dirty leaves the status text unchanged and
    would otherwise serve a stale cached result (AH-0MVFGIV6700329SZ).

    Git commands run with ``cwd`` as the working directory and a scrubbed
    environment, so the fingerprint reflects the *worktree containing cwd*
    (implement loops run suites inside git worktrees) and never a repository
    named by a leaked ``GIT_DIR``. Porcelain paths are repository-root
    relative, so content is resolved against ``git rev-parse --show-toplevel``.

    For non-git directories a stable per-directory fingerprint is returned so
    the cache still works (TTL-only invalidation), documented fallback.

    *strict* raises :class:`GitStateUnavailable` instead of degrading when the
    state cannot be determined: a git checkout whose work tree, HEAD or dirty
    paths cannot be read, or a dirty path whose content cannot be read. A
    caller that may serve a stale result must pass ``strict=True`` — a
    degraded fingerprint (HEAD only, or a per-directory constant) cannot
    distinguish two dirty trees and would serve one tree's verdict for the
    other (WL-0MVPO9P610055UQ3).
    """
    cwd = str(Path(cwd or os.getcwd()).resolve())
    in_git_checkout = _has_git_directory(cwd)
    toplevel = _run_git(cwd, "rev-parse", "--show-toplevel")
    if toplevel is None:
        if strict and in_git_checkout:
            raise GitStateUnavailable(
                f"git cannot report the work tree containing {cwd} "
                "(corrupt/locked index, timeout, or a broken repository)"
            )
        # Not a git repository: stable per-directory key.
        digest = hashlib.sha256(f"no-git:{cwd}".encode()).hexdigest()
        return digest[:16]

    head = _run_git(cwd, "rev-parse", "HEAD")
    if head is None:
        # An unborn branch (a repository with no commits yet) has no HEAD but
        # is otherwise fingerprinted normally; any other HEAD failure is an
        # indeterminate state.
        unborn = _run_git(cwd, "symbolic-ref", "-q", "HEAD") is not None
        if not unborn and strict:
            raise GitStateUnavailable(
                f"git cannot resolve HEAD in {cwd} (corrupt repository or git timeout)"
            )
        head_token = b"unborn" if unborn else b""
    else:
        head_token = head.strip().encode()

    hasher = hashlib.sha256()
    hasher.update(head_token)
    base = Path(toplevel.strip())
    for status, path in sorted(_status_entries(cwd, strict=strict)):
        hasher.update(b"\0status\0" + status.encode("utf-8", "surrogateescape"))
        _update_with_path_content(hasher, base, path, strict=strict)
    return hasher.hexdigest()


def compute_tree_state(
    index_file: str | Path, *, cwd: str | Path | None = None
) -> str:
    """Return the tree SHA (``git write-tree``) of the index at *index_file*.

    This is the fingerprint of a *would-be commit tree*: the gate stages the
    pending change set into an index (a temporary copy when it must not touch
    the real one) and the commit that follows produces exactly this tree.
    Unlike :func:`compute_git_state` it is not blind to generated, gitignored
    metadata — the tree records what the commit would contain, so a suite that
    reads on-disk generated files is bound to the tree it actually validated
    (SA-0MVL2NH1G0094FA2 AC4).

    Args:
        index_file: Path to the index to read — the repository's real
            ``.git/index`` or a temporary copy (never left modified here).
        cwd: Repository/worktree to run ``git`` in. Defaults to the current
            working directory; the finish gate passes its worktree explicitly.

    Returns:
        The 40-character tree SHA ``git write-tree`` reports for *index_file*.

    Raises:
        RuntimeError: when the index cannot be read or ``git write-tree``
            fails (missing repository, unreadable index). Failing loudly is
            deliberate: a silent fallback to the loose working-tree
            fingerprint would reintroduce the unattributable verdict this
            fingerprint exists to prevent.
    """
    resolved_index = str(Path(index_file).resolve())
    resolved_cwd = str(Path(cwd or os.getcwd()).resolve())
    # GIT_INDEX_FILE names the index explicitly and is *not* one of the
    # repository-override variables the shared scrub removes (F3,
    # SA-0MVQHV9Y3001RNNW), so it survives here.
    env = subprocess_env_with_scrubbed_overrides()
    env["GIT_INDEX_FILE"] = resolved_index
    try:
        proc = subprocess.run(
            ["git", "write-tree"],
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Cannot compute the tree state of index {resolved_index}: {exc}"
        ) from exc
    tree = proc.stdout.strip()
    if proc.returncode != 0 or not tree:
        raise RuntimeError(
            f"Cannot compute the tree state of index {resolved_index}: "
            f"{proc.stderr.strip() or 'git write-tree failed'}"
        )
    return tree


def compute_change_set_state(
    cwd: str | Path | None = None,
    *,
    target_tree: str | None = None,
    base_ref: str = "origin/dev",
) -> dict[str, Any] | None:
    """Return the fingerprint of the **patch** a change set contributes.

    A change-set fingerprint identifies the content of a change independently
    of the base it is applied to (SA-0MWFJS9OL001ZTTC). The dev-push gate is
    tree-keyed, so a lost push race — ``dev`` advanced concurrently, the
    feature branch is rebased, the commit and its tree are rewritten — misses
    the tree-keyed entry and re-executes the whole suite even though the
    change itself is byte-identical. This function produces the key that
    survives that rebase.

    The patch is computed **tree-to-tree** with ``--full-index``, so it covers
    every changed path, its pre-image and post-image object ids and every
    hunk. That is what makes reuse provable rather than assumed: a difference
    in our own changes *or* in an upstream change to a file the patch touches
    changes one of those object ids and therefore the fingerprint. Two
    identical fingerprints describe the same patch applied to the same
    pre-image content; only *other* files may differ between the two trees,
    and those are covered by the base tree's own (gate-green) verdict.

    The base tree is derived from *cwd*'s ``HEAD``:

    * when ``HEAD``'s tree **is** *target_tree* (the checked-out pushed
      commit — the dev-push gate's case), the base is ``merge-base(HEAD,
      base_ref)``, falling back to ``HEAD^`` when *base_ref* is unavailable
      (a repo with no remote tracking ref, or a root commit);
    * otherwise *target_tree* is a would-be commit tree (the finish gate's
      pre-commit case) and the base is ``merge-base(HEAD, base_ref)``,
      falling back to ``HEAD`` itself.

    Tree-to-tree diffs never consult the index, so an ambient temporary
    ``GIT_INDEX_FILE`` (the finish gate's would-be-commit index) cannot skew
    the result.

    Args:
        cwd: Repository/worktree the git commands run in.
        target_tree: The tree the change set produces — the would-be-commit
            tree SHA or the pushed commit's tree SHA. An unusable value (or
            one this repository does not hold) disables tracking.
        base_ref: Parent ref the change set targets (default ``origin/dev``).

    Returns:
        ``{"fingerprint", "base_commit", "base_tree", "target_tree",
        "paths"}`` or ``None`` when there is nothing to key on: an unusable
        or missing tree, git cannot answer, or the change set is empty (base
        tree == target tree) — an empty patch must never become a cache key,
        because every empty change set would then share one verdict.
    """
    cwd_str = str(Path(cwd or os.getcwd()).resolve())
    tree = (target_tree or "").strip().lower()
    if not _OBJECT_NAME_RE.fullmatch(tree):
        return None
    try:
        head = (
            _run_git(cwd_str, "rev-parse", "--verify", "HEAD^{commit}") or ""
        ).strip()
        if not head:
            return None
        head_tree = (
            _run_git(cwd_str, "rev-parse", f"{head}^{{tree}}") or ""
        ).strip()
        base_commit = ""
        if base_ref:
            base_commit = (
                _run_git(cwd_str, "merge-base", head, base_ref) or ""
            ).strip()
        if not base_commit:
            # No usable parent ref: for a checked-out pushed commit the base
            # is its first parent, otherwise HEAD itself is the base the
            # would-be tree is built on.
            fallback = "HEAD^" if head_tree == tree else "HEAD"
            base_commit = (
                _run_git(
                    cwd_str, "rev-parse", "--verify", f"{fallback}^{{commit}}"
                )
                or ""
            ).strip()
        if not base_commit:
            return None
        base_tree = (
            _run_git(cwd_str, "rev-parse", f"{base_commit}^{{tree}}") or ""
        ).strip()
        if base_tree and base_tree == tree and head_tree == tree:
            # The target is already on the base ref — the commit has landed
            # (post-push verification runs *after* the ref update) or it is the
            # ref tip. The change set is then the commit's own patch, measured
            # against its first parent: exactly the base the gate used before
            # the push moved the ref. Without this the key would be empty
            # post-push and the gate's own evidence invisible.
            base_commit = (
                _run_git(cwd_str, "rev-parse", "--verify", "HEAD^") or ""
            ).strip()
            base_tree = (
                _run_git(cwd_str, "rev-parse", f"{base_commit}^{{tree}}")
                if base_commit
                else None
            )
            base_tree = (base_tree or "").strip()
        if not base_tree or base_tree == tree:
            # Nothing changed between base and target: no change set to key
            # on (an empty patch would collide with every other empty patch).
            return None
        # ``--full-index`` records pre- and post-image object ids in full, so
        # upstream churn in a touched file cannot hide behind an abbreviated
        # index line. ``--no-renames`` and the pinned prefixes make the patch
        # text independent of user/repo diff config.
        patch = _run_git(
            cwd_str,
            "diff",
            "--no-renames",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base_tree,
            tree,
        )
        if patch is None or not patch.strip():
            return None
        paths = _run_git(
            cwd_str, "diff", "--no-renames", "--name-only", base_tree, tree
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        LOG.warning("test cache: change-set fingerprint unavailable (%s)", exc)
        return None
    fingerprint = hashlib.sha256(
        CHANGE_SET_FINGERPRINT_VERSION + patch.encode("utf-8", "surrogateescape")
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "target_tree": tree,
        "paths": [line for line in (paths or "").splitlines() if line],
    }


def _status_entries(cwd: str, *, strict: bool = False) -> list[tuple[str, str]]:
    """Return ``(status, path)`` for every dirty path in *cwd*.

    ``--untracked-files=all`` expands untracked directories into individual
    files so each file's content is fingerprinted. ``-z`` keeps paths
    NUL-terminated (no quoting) and places a rename/copy's original path in
    the field following the entry.

    With *strict* a failing ``git status`` raises :class:`GitStateUnavailable`
    rather than reporting an empty (and therefore state-independent) dirty
    set.
    """
    output = _run_git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if output is None:
        if strict:
            raise GitStateUnavailable(
                f"git cannot report the working tree state in {cwd} "
                "(corrupt/locked index, timeout, or a broken repository)"
            )
        return []
    fields = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the rename/copy source path follows the entry
        entries.append((status, path))
    return entries


def _update_with_path_content(
    hasher: Any, base: Path, relpath: str, *, strict: bool = False
) -> None:
    """Feed the content at ``base/relpath`` into *hasher*.

    A missing path contributes a stable marker instead of raising (a deleted
    dirty file is a legitimate state). Symlinks hash their target. A directory
    (a submodule gitlink) hashes its own HEAD and status rather than walking
    into another repository; a plain directory falls back to a sorted
    recursive walk.

    With *strict* an unreadable path raises :class:`GitStateUnavailable`
    instead of contributing an "unreadable" marker: the marker is
    content-blind, so two different edits of an unreadable file would share
    one fingerprint and be served each other's cached verdict
    (WL-0MVPO9P610055UQ3).
    """
    path = base / relpath
    hasher.update(b"\0path\0" + relpath.encode("utf-8", "surrogateescape"))
    try:
        if path.is_symlink():
            target = os.readlink(path)
            hasher.update(b"\0link\0" + target.encode("utf-8", "surrogateescape"))
            return
        if path.is_dir():
            sub_head = _run_git(str(path), "rev-parse", "HEAD")
            if sub_head is not None:
                hasher.update(b"\0gitlink\0" + sub_head.strip().encode())
                sub_status = _run_git(str(path), "status", "--porcelain=v1") or ""
                hasher.update(
                    b"\0gitlink-status\0"
                    + sub_status.encode("utf-8", "surrogateescape")
                )
                return
            for child in sorted(path.rglob("*")):
                if child.is_dir() and not child.is_symlink():
                    continue
                _update_with_path_content(
                    hasher, base, child.relative_to(base).as_posix(), strict=strict
                )
            return
        if path.is_file():
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return
        hasher.update(b"\0missing")
    except OSError as exc:
        if strict:
            raise GitStateUnavailable(
                f"cannot read the dirty path {path} to fingerprint it ({exc})"
            ) from exc
        hasher.update(b"\0unreadable")


def is_worktree_clean(cwd: str | Path) -> bool | None:
    """Whether the checkout at *cwd* reports no dirty paths.

    Cleanliness is evaluated with the same ``git status`` call
    (:func:`_status_entries`, ``--untracked-files=all``) that
    :func:`compute_git_state` fingerprints, so "clean" here means exactly "the
    fingerprint is the commit's own state, with no dirty content folded in".
    That is the property the pre-push gate needs to bind a verdict to a pushed
    commit: a dirty checkout's fingerprint belongs to the dirty state, never to
    the commit it happens to have checked out (SA-0MVTRBT50005DUEH).

    Returns ``True`` when no path is dirty, ``False`` when at least one is, and
    ``None`` when the state cannot be reported at all (corrupt/locked index, a
    git timeout, a directory that is not a checkout). A caller that must not
    accept a stale verdict treats ``None`` as "not clean" — fail closed.
    """
    try:
        entries = _status_entries(str(Path(cwd).resolve()), strict=True)
    except GitStateUnavailable:
        return None
    return not entries


def _fingerprint_for_cache(cwd: str) -> str | None:
    """Return the fresh fingerprint for *cwd*, or None when indeterminate.

    The cache never serves or stores a verdict without a fresh, complete
    fingerprint: a degraded one cannot distinguish two dirty trees, so using
    it would return one tree's result for another (WL-0MVPO9P610055UQ3).
    """
    try:
        return compute_git_state(cwd, strict=True)
    except GitStateUnavailable as exc:
        LOG.warning(
            "test cache bypassed: the git state cannot be fingerprinted (%s)",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------


def cache_dir(repo_root: str | Path) -> Path:
    """Resolve the per-repo cache directory for *repo_root*.

    Resolution order:
      1. ``<repo>/.worklog/cache/`` when ``<repo>/.worklog`` exists
      2. ``<git-dir>/test-cache/`` — git dir resolved worktree-aware via
         ``git rev-parse --git-dir`` (``<repo>/.git`` in the main checkout;
         inside the shared git dir for worktrees). Anything under the git dir
         is untracked by definition.
      3. ``<repo>/.test-cache/`` — documented fallback for non-git repos.
    """
    repo_root = Path(repo_root).resolve()
    worklog = repo_root / ".worklog"
    if worklog.is_dir():
        return worklog / "cache"

    git_dir = _run_git(str(repo_root), "rev-parse", "--git-dir")
    if git_dir:
        git_dir_path = Path(git_dir.strip())
        if not git_dir_path.is_absolute():
            git_dir_path = repo_root / git_dir_path
        return git_dir_path / "test-cache"

    return repo_root / ".test-cache"


def cache_key(normalized_command: str, git_state: str, test_type: str = "full") -> str:
    """Return the deterministic cache key for a normalized command + state.

    *test_type* namespaces non-``full`` typed profiles so they never share a
    key with (and can never be mistaken for) a full-suite run. The ``full``
    type keeps the historical key (command + git state only), preserving all
    existing cached entries and audit queries.
    """
    raw = f"{normalized_command}{_KEY_SEPARATOR}{git_state}"
    if test_type != "full":
        raw += f"{_KEY_SEPARATOR}type:{test_type}"
    return hashlib.sha256(raw.encode()).hexdigest()[: _KEY_LENGTH]


def _entry_dir(
    command: str,
    git_state: str,
    cwd: str | Path,
    test_type: str = "full",
) -> Path:
    """Resolve the entry directory for a command at *cwd*."""
    repo_root = Path(cwd or os.getcwd()).resolve()
    return cache_dir(repo_root) / cache_key(
        normalize_test_command(command), git_state, test_type
    )


# ---------------------------------------------------------------------------
# Lookup / store
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file + os.replace)."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def lookup(
    command: str,
    git_state: str,
    *,
    cwd: str | Path | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    test_type: str = "full",
    fingerprint_kind: str = FINGERPRINT_KIND_WORKTREE,
) -> dict[str, Any] | None:
    """Return a cached run entry or None on miss/corruption/expiry.

    A hit requires the stored git state to match *git_state*, the stored
    ``test_type`` to match *test_type*, the stored ``fingerprint_kind`` to
    match *fingerprint_kind*, AND the run to be within *ttl* seconds old.
    Corrupt or unreadable entries are treated as a miss (fresh run) and never
    raise into the caller.

    *fingerprint_kind* names the fingerprint namespace *git_state* belongs to:
    ``"worktree"`` (default — the loose :func:`compute_git_state` fingerprint;
    entries that predate the provenance field read as worktree, so existing
    consumers keep their historical hits) or ``"tree"`` (an explicit
    would-be-commit tree SHA from :func:`compute_tree_state`; an entry without
    provenance metadata, or recorded for a loose state, is a miss so a verdict
    is never accepted for a differently-identified tree).

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``cache_age_seconds`` (how long ago the entry was recorded —
    the age of the reused verdict, for provenance reporting), ``command``
    (normalized), ``git_state``, ``fingerprint_kind``,
    ``scope`` (``"full"`` for historical entries lacking the field — see module
    docs), ``test_type`` (``"full"`` for historical entries lacking the field)
    and ``cached: True``. ``interpreter`` names the concrete
    interpreter/executable recorded for a pytest run (empty for non-pytest
    commands and entries that predate the field).

    A green entry preserved across an aborted run (SA-0MWEURY420040OMD)
    carries its abort observation: ``abort_observed`` (bool),
    ``abort_exit_code`` / ``abort_completed_at`` (the aborted run's values,
    ``None`` when nothing was observed) and ``abort_notice`` (a human-readable
    sentence). The green verdict is still served — the abort is reported
    alongside it, never silently accepted as green.
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd(), test_type)
    meta_path = entry_dir / "metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
        stdout = (entry_dir / "stdout.txt").read_text()
        stderr = (entry_dir / "stderr.txt").read_text()
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if meta.get("version") != _CACHE_VERSION:
        return None
    if meta.get("git_state") != git_state:
        return None  # stale state: never return stale data silently
    if meta.get("test_type", "full") != test_type:
        return None  # type mismatch: never serve one profile as another
    # Provenance: an entry with no recorded kind is a loose-worktree entry
    # (the historical default). A tree request therefore always misses it,
    # and a worktree request never accepts a tree-bound verdict.
    stored_kind = meta.get("fingerprint_kind", FINGERPRINT_KIND_WORKTREE)
    if stored_kind != fingerprint_kind:
        return None
    completed_at = float(meta.get("completed_at", 0))
    exit_code = int(meta.get("exit_code", 0))
    # Failed runs expire after the short failed-run TTL; green runs keep the
    # caller's TTL (SA-0MSJELL44009XYIL).
    effective_ttl = ttl if exit_code == 0 else min(ttl, FAILED_RUN_TTL_SECONDS)
    if time.time() - completed_at > effective_ttl:
        return None  # expired TTL

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": int(meta.get("exit_code", 0)),
        "completed_at": completed_at,
        "cache_age_seconds": max(0.0, time.time() - completed_at),
        "command": meta.get("command", normalize_test_command(command)),
        "git_state": git_state,
        "fingerprint_kind": fingerprint_kind,
        "scope": meta.get("scope", "full"),
        "test_type": meta.get("test_type", "full"),
        "interpreter": meta.get("interpreter", ""),
        # Change-set provenance (SA-0MWFJS9OL001ZTTC): the trees the reused
        # verdict was measured between, so a consumer can state which tree a
        # change-set-keyed verdict actually belongs to. ``verdict_origin``
        # marks a tree-kind entry inherited from a change-set hit
        # (``"change_set_reuse"``, with ``measured_tree`` naming the tree the
        # suite really ran against) so it is never read as directly executed
        # tree evidence.
        "change_set_base_tree": meta.get("change_set_base_tree", ""),
        "change_set_target_tree": meta.get("change_set_target_tree", ""),
        "change_set_paths": list(meta.get("change_set_paths") or []),
        "verdict_origin": meta.get("verdict_origin", ""),
        "measured_tree": meta.get("measured_tree", ""),
        # Abort observation (SA-0MWEURY420040OMD): a green entry preserved
        # across a hang/abort carries the aborted run's exit code and time, so
        # the served green verdict is never silent about the abort.
        **_abort_observation(entry_dir),
        "cached": True,
    }


def _abort_observation(entry_dir: Path) -> dict[str, Any]:
    """Return the abort-observation fields for a served entry.

    Reads the entry's abort side-car (written when an abort would otherwise
    have overwritten a green entry, SA-0MWEURY420040OMD). Always returns the
    four fields — ``abort_observed`` is False when no side-car exists — so a
    consumer sees a stable shape and never has to infer absence.
    """
    sidecar = _read_abort_sidecar(entry_dir)
    if sidecar is None:
        return _no_abort_observation()
    exit_code = sidecar.get("exit_code")
    completed_at = sidecar.get("completed_at")
    try:
        exit_code = int(exit_code)
    except (TypeError, ValueError):
        exit_code = None
    try:
        completed_at = float(completed_at)
    except (TypeError, ValueError):
        completed_at = None
    return {
        "abort_observed": True,
        "abort_exit_code": exit_code,
        "abort_completed_at": completed_at,
        "abort_notice": (
            f"a later run aborted (exit {exit_code if exit_code is not None else '?'})"
            " on the same git state; the green verdict for the unchanged tree"
            " was preserved — re-run on a quiet host to refresh it"
        ),
    }


def store(
    command: str,
    git_state: str,
    *,
    cwd: str | Path | None = None,
    stdout: str,
    stderr: str,
    exit_code: int,
    completed_at: float | None = None,
    scope: str = "full",
    test_type: str = "full",
    interpreter: str = "",
    git_state_at_start: str = "",
    fingerprint_kind: str = FINGERPRINT_KIND_WORKTREE,
    extra_metadata: dict[str, Any] | None = None,
    abort: bool = False,
    ttl: float = DEFAULT_TTL_SECONDS,
) -> Path:
    """Persist a run result, replacing any existing entry for the same key.

    Writes stdout/stderr first, then metadata last (atomic each); a partial
    write is therefore treated as corrupt by :func:`lookup` and degrades to a
    fresh run.

    *abort* (default ``False``) marks the result as a hang/abort rather than a
    verdict (see :func:`is_abort_result`). When an abort would otherwise
    overwrite a **green entry for the same key that is still within** *ttl*,
    the green entry is left untouched and the abort is recorded in a side-car
    (:data:`ABORT_SIDECAR_FILENAME`) instead — so an unchanged tree's green
    verdict survives a contended-host hang and a re-run resumes from it
    (SA-0MWEURY420040OMD). Every other case — red verdict, missing/expired
    green, changed state — writes normally, so a real failure is never masked.
    A normal (non-abort) store removes any stale side-car, because the fresh
    verdict supersedes the earlier abort observation.

    *git_state* is the fingerprint **the entry is keyed on**. Callers that
    can serve the entry later (:func:`run_cached`) pass the *post-run* state,
    because that is the state the next consumer observes; keying on the
    pre-run state leaves a completed run unqueryable whenever transient churn
    disappears while it runs (WL-0MVPO9P610055UQ3).

    *git_state_at_start* optionally records the state the run started in. It
    is informational metadata only — it never keys the entry — and differs
    from *git_state* when the tree moved during the run.

    *scope* records whether the run covered the full suite (``"full"``,
    default — the only scope before scope-aware selection existed) or a
    changed-file subset (``"changed"``). Read-only consumers can therefore
    distinguish partial runs from full-suite evidence.

    *test_type* records the typed profile (``"full"``, default). Non-``full``
    types are keyed independently (see :func:`cache_key`) so they can never be
    mistaken for full-suite evidence.

    *interpreter* records the concrete interpreter/executable that ran pytest
    (empty for non-pytest commands). It is metadata only — it does not change
    the cache key — so the choice of interpreter is auditable per entry
    (SA-0MVK154IE007OS3D AC2).

    *fingerprint_kind* records the provenance of *git_state*: ``"worktree"``
    (default — the loose :func:`compute_git_state` fingerprint), ``"tree"``
    (an explicit would-be-commit tree SHA) or ``"change_set"`` (the patch
    content hash from :func:`compute_change_set_state`). Read-only consumers
    and later lookups use it to refuse verdicts recorded for a different kind
    of tree.

    *extra_metadata* adds informational keys to the entry's ``metadata.json``
    (e.g. the trees a change-set-keyed verdict was measured between).
    Reserved keys (the ones this function writes) always win, so a caller
    cannot rewrite the entry's identity.

    Returns the entry directory path.
    """
    entry_dir = _entry_dir(command, git_state, cwd or os.getcwd(), test_type)
    normalized = normalize_test_command(command)
    completed = completed_at if completed_at is not None else time.time()

    if abort:
        preserved = _abort_preservable_green(
            entry_dir,
            git_state=git_state,
            scope=scope,
            test_type=test_type,
            fingerprint_kind=fingerprint_kind,
            ttl=ttl,
        )
        if preserved is not None:
            # Preserve the green evidence; record the abort alongside it. The
            # green's metadata/stdout/stderr are not touched, so a re-run
            # within the TTL still serves the earlier green verdict.
            _atomic_write(
                _abort_sidecar_path(entry_dir),
                json.dumps(
                    {
                        "version": _CACHE_VERSION,
                        "command": normalized,
                        "git_state": git_state,
                        "scope": scope,
                        "test_type": test_type,
                        "fingerprint_kind": fingerprint_kind,
                        "exit_code": int(exit_code),
                        "completed_at": completed,
                        "stdout_tail": _bounded_output_tail(stdout),
                        "stderr_tail": _bounded_output_tail(stderr),
                    },
                    indent=2,
                ),
            )
            return entry_dir

    entry_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "version": _CACHE_VERSION,
        "command": normalized,
        "git_state": git_state,
        "git_state_at_start": git_state_at_start,
        "scope": scope,
        "test_type": test_type,
        "fingerprint_kind": fingerprint_kind,
        "interpreter": interpreter,
        "exit_code": int(exit_code),
        "completed_at": completed,
    }
    if extra_metadata:
        for key, value in extra_metadata.items():
            meta.setdefault(key, value)
    _atomic_write(entry_dir / "stdout.txt", stdout)
    _atomic_write(entry_dir / "stderr.txt", stderr)
    _atomic_write(entry_dir / "metadata.json", json.dumps(meta, indent=2))
    # A fresh verdict supersedes any earlier abort observation.
    try:
        _abort_sidecar_path(entry_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - defensive
        LOG.warning("test cache: cannot remove the abort side-car (%s)", exc)
    return entry_dir


# ---------------------------------------------------------------------------
# Execution through the cache
# ---------------------------------------------------------------------------


def _default_runner(command: str, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Execute a normalized command string, capturing stdout/stderr.

    The pytest executable prefix is resolved through
    :func:`executable_test_command` so a bare canonical ``pytest`` command runs
    under the interpreter selected by ``resolve_pytest_command()`` — the
    runner's own interpreter when it can run pytest — instead of a PATH
    ``pytest`` that may be bound to a foreign interpreter
    (SA-0MVK154IE007OS3D). This is the path used by consumers that call
    :func:`run_cached` directly (the audit F3 auto-run and the implement
    finish gate), which previously spawned the bare command.

    The environment is the shared repository-override scrub (F2,
    SA-0MUIA3OE40001QJX): a leaked ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_CONFIG*``
    would otherwise override ``cwd`` in every ``git`` subprocess a test spawns
    and redirect fixture git operations at the live checkout. ``~/.local/bin``
    is prepended to PATH if not already present so that user-installed
    executables (e.g. pytest at ``~/.local/bin/pytest``) are found when the
    audit runner spawns suite commands in a restricted environment
    (SA-0MSUZAJPC003BS66).
    """
    log_repository_override_scrub()
    env = subprocess_env_with_scrubbed_overrides()
    return subprocess.run(
        shlex.split(executable_test_command(command)),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def run_cached(
    command: str,
    *,
    cwd: str | Path | None = None,
    force: bool = False,
    no_cache: bool = False,
    ttl: float = DEFAULT_TTL_SECONDS,
    timeout: int = 600,
    runner: Runner = _default_runner,
    scope: str = "full",
    test_type: str = "full",
    fingerprint: str | None = None,
    track_change_set: bool = True,
    change_set_base_ref: str = "origin/dev",
    abort_classifier: Callable[[int, str, str], bool] | None = None,
) -> dict[str, Any]:
    """Run *command* through the cache.

    - A valid cached result (matching git state and type, within TTL) is
      returned without executing — verified by tests asserting the underlying
      command is not spawned.
    - A miss, changed git-state, or expired TTL runs the normalized command,
      stores the result, and returns it.
    - ``force=True`` bypasses lookup but still stores (refresh).
    - ``no_cache=True`` bypasses lookup AND storage (pure bypass).
    - The stored entry is keyed on the **post-run** state — the state the
      checkout is in once the run finishes, which is what the next consumer
      (the next run's pre-check, ``query_cached()``, ``--summary``) observes.
      Keying on the pre-run state made a completed run unqueryable whenever
      transient churn disappeared while it ran: the ``missing`` summary
      reported immediately after a green full-suite run
      (WL-0MVPO9P610055UQ3). The pre-run state is recorded as
      ``git_state_at_start`` metadata for diagnosis.
    - When the git state cannot be fingerprinted (corrupt/locked index, git
      timeout, unreadable dirty path) the cache is bypassed entirely: no
      lookup, no store, a fresh execution. A degraded fingerprint would make
      every dirty tree share one key and serve stale verdicts
      (WL-0MVPO9P610055UQ3).
    - *scope* (default ``"full"``) is recorded in the stored metadata so
      read-only consumers can tell partial runs from full-suite evidence.
    - *test_type* (default ``"full"``) selects the typed-profile cache
      namespace; non-``full`` types use independent keys and only ``full``
      populates the audit-accepted full-suite entry.
    - *fingerprint* (default ``None``) is an **explicit expected fingerprint**
      — normally a would-be-commit tree SHA from :func:`compute_tree_state`.
      When supplied it keys the entry and the lookup instead of the loose
      :func:`compute_git_state` value (which is then not computed at all), and
      the entry is recorded with ``fingerprint_kind="tree"``. A stored entry
      whose fingerprint differs, or that predates the provenance metadata, is
      a miss: a cached verdict is accepted only for a matching tree. Callers
      that pass nothing keep the historical loose-worktree keys byte for byte
      (test skill CLI, audit, pre-push).

    *track_change_set* (default ``True``, only meaningful with *fingerprint*)
    additionally keys the run on the **patch content** the change set
    contributes (:func:`compute_change_set_state`, ``fingerprint_kind=
    "change_set"``) and, on a tree-keyed miss, serves a verdict stored for
    that same patch (SA-0MWFJS9OL001ZTTC). A pure rebase onto a concurrently
    advanced parent rewrites the commit and its tree but not the patch, so
    the verification a lost push race used to pay for again is served instead
    of re-executed. The reuse is provable rather than heuristic: the patch
    records every changed path plus its pre- and post-image object ids, so a
    change in this work *or* an upstream change to a file the patch touches
    yields a different key and misses. A change set identical to its base has
    an empty patch and is never keyed (an empty key would be shared by every
    empty change set). *change_set_base_ref* names the parent ref the patch is
    computed against (default ``origin/dev``), falling back to ``HEAD`` /
    ``HEAD^`` when it does not resolve.

    On a change-set hit the result carries ``reused_via_change_set: True`` and
    ``change_set_target_tree`` — the tree the reused verdict was actually
    measured on — while ``fingerprint_kind`` reports ``"change_set"``.

    *abort_classifier* (default ``None``) classifies an executed result as a
    **hang/abort** rather than a verdict: it is called once, only when the
    command exits non-zero, with ``(exit_code, stdout, stderr)``. When it
    returns True, the entry is stored as an abort
    (``store(..., abort=True)``): if a green entry for the same key is still
    within *ttl* it is preserved and the abort recorded alongside it, so a
    contended-host hang does not force a full re-run of an unchanged tree
    (SA-0MWEURY420040OMD). The run result reports ``abort_result`` (the
    classification), ``abort_recorded`` (an abort verdict was recorded —
    never under ``no_cache``) and ``green_preserved`` (a green entry was
    preserved instead of overwritten). Without a classifier (the default, and
    every pre-existing caller) behaviour is byte-for-byte unchanged: run
    results are always stored as the current verdict.

    A change-set *hit* also mirrors the served verdict under the pushed tree's
    key, stamped ``verdict_origin="change_set_reuse"`` with the tree the suite
    actually measured (``measured_tree``) and the original ``completed_at``:
    the pushed tree is never silently presented as directly executed evidence
    (``lookup`` returns both fields), and every read-only consumer (post-push
    verification, gate-evidence classification, audit) finds the verdict under
    the key it already queries.

    Returns a dict with ``stdout``, ``stderr``, ``exit_code``, ``completed_at``
    (epoch float), ``command`` (normalized), ``git_state`` (the state the
    entry is keyed on — empty when the cache was bypassed for an
    indeterminate state), ``fingerprint_kind`` (``"worktree"`` or ``"tree"``),
    ``scope``, ``test_type``, ``interpreter`` (the concrete
    interpreter/executable that ran pytest, empty for non-pytest commands) and
    ``cached`` (True when served from cache).
    """
    cwd_path = Path(cwd or os.getcwd()).resolve()
    cwd_str = str(cwd_path)
    normalized = normalize_test_command(command)

    # ``no_cache`` never reads or writes the cache, so it needs no fingerprint
    # (and must not fail when the tree cannot be fingerprinted).
    if fingerprint is not None:
        # Explicit expected fingerprint (a would-be-commit tree SHA): bind the
        # verdict to the tree the caller named and never recompute — or let it
        # be replaced by — the loose worktree state.
        git_state_before = None if no_cache else str(fingerprint)
        fingerprint_kind = FINGERPRINT_KIND_TREE
    else:
        git_state_before = None if no_cache else _fingerprint_for_cache(cwd_str)
        fingerprint_kind = FINGERPRINT_KIND_WORKTREE

    # Change-set binding (SA-0MWFJS9OL001ZTTC): the same change, measured on a
    # different (re-based) parent. An unavailable state (non-git cwd, unusable
    # tree, empty change set) only disables the extra key — never the run.
    change_set: dict[str, Any] | None = None
    if fingerprint is not None and track_change_set and not no_cache:
        change_set = compute_change_set_state(
            cwd_str,
            target_tree=str(fingerprint),
            base_ref=change_set_base_ref,
        )

    if not force and git_state_before is not None:
        hit = lookup(
            command,
            git_state_before,
            cwd=cwd_str,
            ttl=ttl,
            test_type=test_type,
            fingerprint_kind=fingerprint_kind,
        )
        # A cached entry recorded under a different scope is not a valid hit
        # for this request (scope is stored in metadata; the cache key is the
        # command+state, so this only matters if command sets ever converge —
        # never serve a partial run as a full-suite result and vice versa).
        if hit is not None and hit.get("scope", "full") == scope:
            return {**hit, "state_changed_during_run": None}
        # Tree-keyed miss: the exact tree has no verdict, but a verdict exists
        # for the identical *change set* (a rebase rewrote the tree, not the
        # patch). Serving it is what makes a lost push race cheap.
        if change_set is not None:
            change_hit = lookup(
                command,
                change_set["fingerprint"],
                cwd=cwd_str,
                ttl=ttl,
                test_type=test_type,
                fingerprint_kind=FINGERPRINT_KIND_CHANGE_SET,
            )
            if change_hit is not None and change_hit.get("scope", "full") == scope:
                # Mirror the inherited verdict under the pushed tree's key so
                # consumers that look for tree-kind evidence (post-push
                # verification refuses the terminal transition on ``missing``)
                # find it. The entry keeps the original ``completed_at`` (no
                # extended lifetime) and states its origin, so it is never
                # mistaken for a directly executed tree verdict.
                if fingerprint is not None and not no_cache:
                    store(
                        normalized,
                        str(fingerprint),
                        cwd=cwd_str,
                        stdout=change_hit.get("stdout", ""),
                        stderr=change_hit.get("stderr", ""),
                        exit_code=change_hit.get("exit_code", 0),
                        completed_at=change_hit.get("completed_at"),
                        scope=change_hit.get("scope", scope),
                        test_type=test_type,
                        interpreter=change_hit.get("interpreter", ""),
                        git_state_at_start=str(fingerprint),
                        fingerprint_kind=FINGERPRINT_KIND_TREE,
                        extra_metadata={
                            "verdict_origin": "change_set_reuse",
                            "measured_tree": change_hit.get(
                                "change_set_target_tree"
                            )
                            or "",
                            "change_set_fingerprint": change_set["fingerprint"],
                        },
                    )
                return {
                    **change_hit,
                    "state_changed_during_run": None,
                    "reused_via_change_set": True,
                    "primary_fingerprint_missed": str(fingerprint),
                }

    normalized = normalize_test_command(command)
    # Detect a mid-run checkout move by bracketing the runner with the guarded
    # checkout's own state (HEAD + tracked-tree content, SA-0MW3FF18I003KGZE
    # Q2 = b): a full-suite run started in a checkout that a concurrent
    # ``implement.py finish`` fast-forwards executes the *new* test files
    # against the *old* sources and reports failures that exist in neither
    # tree. On movement the mixed verdict is quarantined — no cache entry under
    # either state — and reported non-green with the distinct
    # :data:`STATE_CHANGED_EXIT_CODE`. Differences fully explained by the
    # start-of-run working-tree attribution (a sibling session committing work
    # it had itself dirtied) are never flagged (AC3). An unfingerprintable
    # checkout (``ok=False``) is never reported as movement (fail closed).
    own_before = snapshot_own_checkout(cwd_str)
    proc = runner(normalized, cwd_str, timeout)
    completed_at = time.time()
    own_after = snapshot_own_checkout(cwd_str)
    movement = compare_own_checkout_states(own_before, own_after)

    # Record which interpreter actually ran pytest (metadata only; the cache
    # key is unchanged) so the choice is auditable after the fact
    # (SA-0MVK154IE007OS3D AC2). Non-pytest commands record an empty string.
    interpreter = resolve_pytest_executable() if is_pytest_command(normalized) else ""

    # A hang/abort produced no usable verdict (SA-0MWEURY420040OMD): classify
    # it so store() can preserve a green entry instead of overwriting it. The
    # classifier only runs on a non-zero exit and is fail-safe — an exception
    # leaves the result treated as a normal verdict, never as an abort that
    # could mask a real failure.
    abort_result = False
    if abort_classifier is not None and proc.returncode != 0:
        try:
            abort_result = bool(
                abort_classifier(
                    proc.returncode, proc.stdout or "", proc.stderr or ""
                )
            )
        except Exception as exc:  # noqa: BLE001 - classification is advisory
            LOG.warning(
                "test cache: abort classifier failed (%s); treating the run "
                "as a normal verdict",
                exc,
            )
            abort_result = False

    if movement["moved"]:
        checkpoint = movement["checkout"]
        before_commit = movement["before_commit"]
        after_commit = movement["after_commit"]
        notice = {
            "notice_code": STATE_CHANGED_NOTICE_CODE,
            "checkout": checkpoint,
            "before_commit": before_commit,
            "after_commit": after_commit,
            "message": (
                f"checkout {checkpoint} changed while the test suite executed "
                f"(HEAD {before_commit or '<none>'} -> "
                f"{after_commit or '<none>'}); the mixed tree does not exist "
                f"at any single commit, so the verdict is discarded — re-run "
                f"the suite from a stable checkout."
            ),
        }
        return {
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "exit_code": STATE_CHANGED_EXIT_CODE,
            "completed_at": completed_at,
            "command": normalized,
            "git_state": "",
            "fingerprint_kind": fingerprint_kind,
            "scope": scope,
            "test_type": test_type,
            "interpreter": interpreter,
            "cached": False,
            "abort_result": False,
            "abort_recorded": False,
            "green_preserved": False,
            **_no_abort_observation(),
            "state_changed_during_run": notice,
        }

    git_state = git_state_before or ""
    green_preserved = False
    if not no_cache:
        if fingerprint is not None:
            # An explicit fingerprint describes the tree the caller named; it
            # cannot be recomputed from the (possibly churned) worktree after
            # the run, so it keys the entry as-is.
            tree_entry = store(
                normalized,
                git_state,
                cwd=cwd_str,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                exit_code=proc.returncode,
                completed_at=completed_at,
                scope=scope,
                test_type=test_type,
                interpreter=interpreter,
                git_state_at_start=git_state_before or "",
                fingerprint_kind=FINGERPRINT_KIND_TREE,
                abort=abort_result,
                ttl=ttl,
            )
            green_preserved = _abort_preserved_after_store(
                tree_entry, abort_result, completed_at
            )
            # Mirror the same verdict under the change-set key so a later
            # rebase of the identical patch is served instead of re-executed
            # (SA-0MWFJS9OL001ZTTC). The entry records both trees, so a
            # consumer can always state which tree this verdict measured.
            if change_set is not None:
                store(
                    normalized,
                    change_set["fingerprint"],
                    cwd=cwd_str,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    exit_code=proc.returncode,
                    completed_at=completed_at,
                    scope=scope,
                    test_type=test_type,
                    interpreter=interpreter,
                    git_state_at_start=change_set["fingerprint"],
                    fingerprint_kind=FINGERPRINT_KIND_CHANGE_SET,
                    extra_metadata={
                        "change_set_base_commit": change_set["base_commit"],
                        "change_set_base_tree": change_set["base_tree"],
                        "change_set_target_tree": change_set["target_tree"],
                        "change_set_paths": change_set["paths"],
                    },
                    abort=abort_result,
                    ttl=ttl,
                )
        else:
            # Key the entry on the state the run left behind: that is the
            # state every later consumer computes, so a completed run is
            # always queryable. The pre-run state is kept as metadata only —
            # it never keys the entry, because transient churn present when a
            # run starts can be gone by the time it finishes
            # (WL-0MVPO9P610055UQ3).
            git_state_after = _fingerprint_for_cache(cwd_str)
            if git_state_after is not None:
                git_state = git_state_after
                worktree_entry = store(
                    normalized,
                    git_state_after,
                    cwd=cwd_str,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    exit_code=proc.returncode,
                    completed_at=completed_at,
                    scope=scope,
                    test_type=test_type,
                    interpreter=interpreter,
                    git_state_at_start=git_state_before or "",
                    abort=abort_result,
                    ttl=ttl,
                )
                green_preserved = _abort_preserved_after_store(
                    worktree_entry, abort_result, completed_at
                )

    return {
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "completed_at": completed_at,
        "command": normalized,
        "git_state": git_state,
        "fingerprint_kind": fingerprint_kind,
        "scope": scope,
        "test_type": test_type,
        "interpreter": interpreter,
        "cached": False,
        "abort_result": abort_result,
        "abort_recorded": bool(abort_result and not no_cache),
        "green_preserved": green_preserved,
        **_no_abort_observation(),
        "state_changed_during_run": None,
    }


def query_cached(
    command: str,
    *,
    cwd: str | Path | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    test_type: str = "full",
    fingerprint: str | None = None,
    fingerprint_kind: str = FINGERPRINT_KIND_WORKTREE,
) -> dict[str, Any] | None:
    """Read-only cache query: never executes anything.

    Returns the cached entry for *command* at the current git state and
    *test_type*, or None on miss. Defaults to the ``full`` profile so the
    audit skill's full-suite verification is unchanged; read-only consumers
    must pass a non-``full`` *test_type* to inspect a typed profile.

    An indeterminate git state is a miss, never a hit: the cache cannot prove
    the entry was recorded for this tree, and a degraded fingerprint would
    match every dirty tree (WL-0MVPO9P610055UQ3).

    *fingerprint* (default ``None``) queries an explicit fingerprint instead
    of computing the loose worktree state — normally a would-be-commit (or
    pushed-commit) tree SHA from :func:`compute_tree_state`. It is paired with
    *fingerprint_kind* (default ``"worktree"`` for backwards compatibility;
    pass ``"tree"`` for a tree key) so a read-only consumer can look up the
    verdict the tree-keyed gate recorded (SA-0MWAXC5TD00461FN). Passing an
    explicit fingerprint never recomputes the loose state: the entry is looked
    up under exactly the named key and a mismatched kind is a miss.
    """
    cwd_str = str(Path(cwd or os.getcwd()).resolve())
    if fingerprint is not None:
        git_state = str(fingerprint)
    else:
        git_state = _fingerprint_for_cache(cwd_str)
        if git_state is None:
            return None
    return lookup(
        command,
        git_state,
        cwd=cwd_str,
        ttl=ttl,
        test_type=test_type,
        fingerprint_kind=fingerprint_kind,
    )


# ---------------------------------------------------------------------------
# Cache grafting (isolated-worktree run -> main checkout)
# ---------------------------------------------------------------------------


def mirror_cache_entries(
    source: str | Path, destination: str | Path
) -> list[str]:
    """Graft *source*'s cache entries into *destination*'s cache.

    An isolated full-suite run (the audit skill's F3 execution worktree) is a
    linked worktree with no ``.worklog`` of its own, so :func:`cache_dir`
    stores its entries under ``<git-dir>/test-cache`` — and ``git worktree
    remove`` deletes that directory with the worktree. Read-only consumers
    (subsequent audits and post-push verification) query the **main
    checkout's** ``.worklog/cache``, so without a graft every audit re-executes
    a suite whose verdict it already holds (WL-0MVSPZ1FH000RWET).

    Mirrors the dev-push hook's ``mirror_suite_cache`` semantics: cache keys
    are content-addressed (command + git state), so copying an entry the
    destination lacks is always safe, and an entry that already exists is
    **never overwritten** (a fresher/identical entry must not be replaced by
    a copy). Fail-soft by design — cache plumbing must never fail the run:
    every per-entry I/O error is logged and skipped, and the caller's verdict
    is unaffected.

    Args:
        source: A checkout whose cache directory should be copied **from**
            (normally the isolated execution worktree).
        destination: A checkout whose cache directory should be copied
            **into** (normally the main checkout).

    Returns:
        The entry directory names that were copied (empty when the source has
        no cache, the two roots resolve to the same cache, or every entry was
        already present).
    """
    try:
        source_dir = cache_dir(source)
        destination_dir = cache_dir(destination)
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        LOG.warning("test cache: cannot resolve cache directories (%s)", exc)
        return []
    if source_dir.resolve() == destination_dir.resolve():
        return []
    if not source_dir.is_dir():
        return []
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOG.warning(
            "test cache: cannot create the destination cache %s (%s)",
            destination_dir,
            exc,
        )
        return []
    import shutil

    mirrored: list[str] = []
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        target = destination_dir / entry.name
        if target.exists():
            # Never overwrite an existing entry: the key is content-addressed,
            # so the destination's copy describes the same state.
            continue
        try:
            shutil.copytree(entry, target)
            mirrored.append(entry.name)
        except OSError as exc:
            LOG.warning("test cache: could not mirror %s (%s)", entry, exc)
            shutil.rmtree(target, ignore_errors=True)
    if mirrored:
        LOG.info(
            "test cache: grafted %d entr%s from %s into %s",
            len(mirrored),
            "y" if len(mirrored) == 1 else "ies",
            source_dir,
            destination_dir,
        )
    return mirrored


# ---------------------------------------------------------------------------
# Summary extraction
# ---------------------------------------------------------------------------

# Summary lines that agents typically grep for after a run: vitest output
# ("Test Files ...", "Tests ..."), pytest ("N passed", "N failed"), node TAP
# ("# tests", "# pass", "# fail").
_DEFAULT_SUMMARY_PATTERN = r"Test Files|Tests\s+\d+|passed|failed|skipped|xfailed|# (pass|fail|tests)"


def summary_lines(
    stdout: str, stderr: str = "", pattern: str | None = None
) -> list[str]:
    """Return output lines matching *pattern* (default summary pattern).

    Used by ``run_tests.py --summary`` to extract summary lines from a cached
    run without executing it.
    """
    import re

    rx = re.compile(pattern or _DEFAULT_SUMMARY_PATTERN)
    combined = f"{stdout}\n{stderr}"
    return [line for line in combined.splitlines() if rx.search(line)]


__all__ = [
    "ABORT_EXIT_CODE",
    "ABORT_OUTPUT_TAIL_CHARS",
    "ABORT_SIDECAR_FILENAME",
    "DEFAULT_TTL_SECONDS",
    "FAILED_RUN_TTL_SECONDS",
    "FINGERPRINT_KIND_CHANGE_SET",
    "FINGERPRINT_KIND_TREE",
    "FINGERPRINT_KIND_WORKTREE",
    "STATE_CHANGED_EXIT_CODE",
    "STATE_CHANGED_NOTICE_CODE",
    "GitStateUnavailable",
    "cache_dir",
    "cache_key",
    "compute_change_set_state",
    "compute_git_state",
    "compute_tree_state",
    "is_abort_result",
    "lookup",
    "mirror_cache_entries",
    "normalize_test_command",
    "query_cached",
    "run_cached",
    "store",
    "summary_lines",
]
