# Test Isolation — Live-Repository Mutation Evidence and Hermetic-Git Requirement

> **Work item:** SA-0MUG0WFP8008WN63 (parent), evidence slice
> SA-0MUG30H6V00036HS (this document).
>
> **Status of the fix:** this document records the *evidence* and the
> *convention*. The enforcement layers (hermetic sandbox helper + the two
> regression guards) are delivered by the sibling children F2–F5; the
> authoritative test-authoring guidance is updated by F6 in
> [`skill/shared/test-writing-guidelines.md`](../../skill/shared/test-writing-guidelines.md)
> and [`skill/test/SKILL.md`](../../skill/test/SKILL.md).

## 1. What happened

On 2026-09-24 a full-suite run (`run_tests.py --scope full`) invoked from the
live checkout mutated the live repository:

- local `dev` HEAD was moved onto fixture commits (~59 fixture commits);
- fixture branches were created in the live repo
  (`feature-x`, `wl-OSL-1-test`, `wl-SA-001-test-feature`, local `origin/dev`);
- `.git/config` was rewritten (identity, `core.bare`, `core.hooksPath`);
- tracked `.githooks/*` files were deleted from the working tree;
- fixture commits were pushed to the real `origin/dev`.

The remote and local `dev` were force-restored, but the underlying
test-isolation defect remained. Prior work (SA-0MU8EKJYY007PT42,
SA-0MU89WA740004GOJ) guessed the cause and guarded a single helper
(`_init_repo`); the other real-git helpers were left unguarded.

## 2. Root-cause hypotheses

Two hypotheses were investigated, time-boxed to one working session.

### H1 — Environment leak (`GIT_DIR` and friends) — **CONFIRMED**

If a repository-overriding git variable (`GIT_DIR`, `GIT_WORK_TREE`,
`GIT_CONFIG`, `GIT_CONFIG_GLOBAL`) is present in the process environment, it is
inherited by every `git` subprocess and **overrides the `cwd=` argument**. A
test helper that runs `git init` / `git branch -M dev` / `git config` /
`git add` / `git commit` / `git worktree add` in an otherwise-isolated
`tmp_path` repo therefore operates on the leaked target repository instead.

This single mechanism explains *every* observed symptom: `git init --bare`
flipping `core.bare`, identity rewrites, `branch -M dev` moving `dev`, fixture
branches (`wl-OSL-1-test`, `feature-x`, `wl-SA-001-test-feature`) and the local
`origin/dev` branch appearing in the live repo, and `git push origin dev`
publishing fixture commits to the real remote.

### H2 — `TMPDIR` / pytest `--basetemp` misresolution — **NOT the mechanism**

`pytest`'s `tmp_path` is rooted under the system temp dir
(`tempfile.gettempdir()`, i.e. `TMPDIR` or `/tmp`) — **never** the invoking
`cwd`. Even when `TMPDIR` points *inside* the live checkout, the fixtures
create their repos in a **subdirectory** of `tmp_path`, so `git init` creates a
*nested* repository and the parent checkout's refs are untouched. The probe in
§4 demonstrates this. `TMPDIR` misresolution can leave stray directories
(e.g. `root-file-repo/`) inside the checkout, but it cannot move the checkout's
refs or rewrite its config. The previous guard's docstring attributed the
incident to this hypothesis; that attribution is insufficient.

## 3. Deterministic reproduction (H1)

The reproduction is **hermetic**: it creates a throwaway *victim* repository in
the system temp dir with its own local bare `origin`, leaks `GIT_DIR` pointing
at the victim, and runs the **real, unguarded** fixture helpers with an
isolated `tmp_path` `cwd`. The live checkout is never the target.

### Environment

- Any Linux/macOS shell with `git` and `python3` (stdlib only).
- Run from the repository root; `TMPDIR` must be a normal temp directory
  (the default). The script refuses to run if its temp root sits inside a git
  repository.
- No `GIT_*` variables set in the outer environment.

### Ordered command sequence

```bash
# 1. Verify the live checkout fingerprint BEFORE (must equal the AFTER value).
git for-each-ref --format='%(refname) %(objectname)' | sha256sum
git status --porcelain=v1 -uall | sha256sum

# 2. Run the evidence reproduction (3 consecutive runs).
python3 docs/dev/repro_live_repo_leak.py --runs 3

# 3. Verify the live checkout fingerprint AFTER — it must be byte-identical
#    to step 1.
git for-each-ref --format='%(refname) %(objectname)' | sha256sum
git status --porcelain=v1 -uall | sha256sum
```

### Expected signature

Every scenario reports `[LEAK]`, each run creates the incident's signature in
the **victim** repo, and the script exits `0` with
`RESULT: leak mechanism REPRODUCED in all scenarios and runs.`

Captured output of three consecutive runs (abridged to the changed refs/config
per scenario; commit SHAs vary per run by design):

```text
=== run 1/3 ===
[LEAK] launch-context _make_real_git_project_with_worktree (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/wl-OSL-1-test <sha>
    + config  user.email=test@test.com
    + config  user.name=Test
[LEAK] merge-gate _make_real_repo (raised CalledProcessError)
    + config  user.email=t@t.com
    + config  user.name=T
[LEAK] run-tests-scope _make_repo (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/origin/dev <sha>
    + config  user.email=test@test.com
    + config  user.name=Test
=== run 2/3 ===
[LEAK] launch-context _make_real_git_project_with_worktree (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/wl-OSL-1-test <sha>
    + config  user.email=test@test.com
    + config  user.name=Test
[LEAK] merge-gate _make_real_repo (raised CalledProcessError)
    + config  user.email=t@t.com
    + config  user.name=T
[LEAK] run-tests-scope _make_repo (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/origin/dev <sha>
    + config  user.email=test@test.com
    + config  user.name=Test
=== run 3/3 ===
[LEAK] launch-context _make_real_git_project_with_worktree (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/wl-OSL-1-test <sha>
    + config  user.email=test@test.com
    + config  user.name=Test
[LEAK] merge-gate _make_real_repo (raised CalledProcessError)
    + config  user.email=t@t.com
    + config  user.name=T
[LEAK] run-tests-scope _make_repo (completed)
    + ref     refs/heads/dev <sha>
    + ref     refs/heads/origin/dev <sha>
    + config  user.email=test@test.com
    + config  user.name=Test

RESULT: leak mechanism REPRODUCED in all scenarios and runs.
```

Scenario → real helper mapping:

| Scenario | Helper | Incident signature |
|----------|--------|--------------------|
| `launch-context _make_real_git_project_with_worktree` | `skill/audit/tests/test_audit_runner_launch_context.py` | `wl-OSL-1-test` branch; `dev` moved |
| `merge-gate _make_real_repo` | `skill/audit/tests/test_audit_runner_merge_gate.py` | identity + `remote.origin` rewritten; `git init --bare` can flip `core.bare`; push attempted |
| `run-tests-scope _make_repo` | `skill/test/tests/test_run_tests_scope.py` | `dev` moved; local `origin/dev` created |

`_make_real_repo` raises `CalledProcessError` once the leaked target becomes
inconsistent (e.g. `git push` after the remote was rewritten) — the mutation
has already happened by then, which is precisely why a **detect-only guard
that fails the run** is required.

### Exact originating variable

The specific variable that leaked during the original incident cannot be
recovered from the repository (the evidence was overwritten and restored).
H1 is demonstrated as a *sufficient* mechanism, not as a claim about which
shell/process exported it. The regression guards (F4/F5) detect the *effect*
regardless of the leak's source, so the exact provenance is not required for
the fix. The guard-fires proof (F2) is the accepted fallback proof per parent
AC2 for the unrecoverable provenance.

## 4. H2 probe (misresolution alone does not mutate the parent)

```bash
# Create an outer repo, point TMPDIR inside it, then run a fixture-style
# `git init` in a tmp_path SUBDIRECTORY.
W=$(mktemp -d); mkdir -p "$W/outer"; cd "$W/outer"
git init -q .; git config user.email r@r; git config user.name R
echo x > a; git add -A; git commit -qm real; git branch -M dev
BEFORE=$(git for-each-ref --format='%(refname) %(objectname)')

TMPDIR="$W/outer" python3 - <<'PY'
import tempfile, subprocess
from pathlib import Path
base = Path(tempfile.mkdtemp(prefix="pytest-of-x"))
repo = base / "test_repo"; repo.mkdir()
subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
print("fixture repo toplevel =",
      subprocess.run(["git", "rev-parse", "--show-toplevel"],
                     cwd=repo, capture_output=True, text=True).stdout.strip())
PY

AFTER=$(git for-each-ref --format='%(refname) %(objectname)')
[ "$BEFORE" = "$AFTER" ] && echo "OUTER refs UNCHANGED"
```

Observed:

```text
tempdir = <W>/outer
fixture repo toplevel = <W>/outer/pytest-of-x.../test_repo   (nested)
OUTER refs UNCHANGED
```

The fixture repo is nested under `TMPDIR`; the enclosing checkout's refs do
not change. `TMPDIR` misresolution is therefore a *contributing* condition for
stray artefacts, not the mechanism that mutates refs/config.

## 5. Hermetic-git requirement (convention)

Every test helper that shells out to **real `git`** must:

1. operate inside a `tmp_path`-rooted sandbox, never the live checkout;
2. assert it is **not** targeting a repository outside its sandbox before the
   first mutating command;
3. neutralise repository-overriding environment variables
   (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_CONFIG`, `GIT_CONFIG_GLOBAL`) and set
   fixture-local identity (`user.name`/`user.email`) explicitly;
4. avoid `git init`-on-an-existing-repo patterns that silently re-target an
   enclosing repository.

The shared sandbox helper and the two regression guards (a `run_tests.py`
pre/post snapshot and a repo-root autouse `conftest` fixture) are delivered by
F2–F5. The authoritative authoring guidance is
[`skill/shared/test-writing-guidelines.md`](../../skill/shared/test-writing-guidelines.md)
and [`skill/test/SKILL.md`](../../skill/test/SKILL.md) (updated by F6).

## 6. Live-checkout cleanliness

The reproduction leaves the live checkout byte-identical (verified in AC5 of
this slice): `git for-each-ref` and `git status --porcelain=v1 -uall` hashes
were identical before and after the three evidence runs (251 refs, clean
working tree).

### 6.1 Cleanup of pre-existing fixture artefacts

The **pre-existing** artefacts left in the live checkout by the incident were
removed by the companion item **SA-0MUG216UP008821M** (blocked by the
prevention layer, F3) once the dirty-tree safety gate cleared:

| Artefact | Disposition |
|----------|-------------|
| Fixture branch `feature-x` (`4afe475f`) | Deleted (`git branch -D`) |
| Fixture branch `wl-OSL-1-test` (`ab56ec6c`) | Deleted (`git branch -D`) |
| Fixture branch `wl-SA-001-test-feature` (`fe099f2c`) | Deleted (`git branch -D`) |
| Local branch `origin/dev` (`ea135719`) | Deleted (`git branch -D`); tooling had temporarily renamed it `_stale_origin_dev_backup_SA_test`, so the earlier name-based check missed it |
| Untracked `root-file-repo/` | Already absent at cleanup time |
| 13 stale `.worklog/tmp-worktree-*` directories | Removed; `git worktree prune` run |
| Tags `backup-corrupt-dev-1790283975`, `backup-real-dev-6d7b4b4f` | **Retained** for rollback |

Verified after cleanup:

```bash
git branch --list 'origin/dev' 'feature-x' 'wl-OSL-1-test' 'wl-SA-001-test-feature'  # empty
git rev-parse HEAD                # 0fb18eeb11201e776d297a5f89aacb08569c2aae
git rev-parse origin/dev          # 0fb18eeb11201e776d297a5f89aacb08569c2aae  (remote-tracking)
git status --short                # empty
git tag --list 'backup-*'         # backup-corrupt-dev-1790283975, backup-real-dev-6d7b4b4f
```

**Ignore-rule review (`root-file-repo/`):** deliberately **not** added to
`.gitignore`. The live-repo mutation guard detects stray artefacts via
`git status --porcelain=v1 --untracked-files=all`; ignoring `root-file-repo/`
would hide a recurrence from the very guard meant to catch it. The existing
`.worklog/tmp-worktree-*` ignore entry is appropriate because those directories
are legitimate transient worktree placeholders, not incident artefacts.

**Cleanup safety:** the removal touched only the named fixture branches and the
stale detached worktree directories; it did not disturb other agents' worktrees
or unrelated branches, and the `backup-*` tags were retained.

## 7. Verification (F7, SA-0MUG30KZX000TW0S)

Verification of the parent's AC4/AC6 after the F5 guard fix
(SA-0MUH5KFDV002ORSS) and the companion cleanup (SA-0MUG216UP008821M, commit
`e36dd153`) had both landed on `dev`.

### 7.1 AC1 — full suite green

```bash
# from the F7 implement worktree (dev @ e36dd153)
python3 skill/test/scripts/run_tests.py --scope full --json --no-cache
```

Result: `success: true`, `failures: []`, `cached: false`, `returncode: 0`
(930 s, pytest + node suites). No test was skipped, xfailed, or retried to
reach green.

### 7.2 AC2 — prevention proof (guarded-worktree stand-in)

The plan's fresh-clone stand-in (D8) is **unsound** and was replaced:

- a clone is not owned by the SorraAgents prefix-based project resolution, so
  the audit launch-context tests resolve `TEST`-prefixed items to unrelated
  leftovers and the clone lacks the untracked `.worklog` runtime state — a clone
  run produced 56 spurious failures;
- an implement worktree is a **real** checkout that shares the live checkout's
  `.git` (same refs, same local config). A ref/config mutation therefore *would*
  be visible in the live checkout, making the worktree a **stronger** stand-in
  than a clone, not a weaker one.

The controlled run (§7.1) snapshotted the **live checkout** with read-only
plumbing before and after; nothing changed:

```
refs   sha256  f6f5fb6b...f937a5   (identical before/after; 249 refs)
config sha256  aefac331...d637cc   (identical)
status sha256  e3b0c442...b855   (identical; empty — clean tree)
```

A separate full `git for-each-ref` diff before/after the run is empty. The
`run_tests.py` F4 guard also reported no mutation (`success: true`).

> **Producer decision (assumption recorded):** AC2's literal "fresh clone" is
> replaced by the guarded implement-worktree run, per the OPEN QUESTION raised
> in this item's comment (2026-09-25T13:18Z). Rationale above. Residual risk:
> pointer to the decision remains for the producer's review at audit time.

### 7.3 AC3 — supervised live-checkout run: not performed

No full-suite run was executed *in* the live checkout. The live checkout was
only ever snapshotted read-only. Residual risk: the exact live environment was
not exercised directly; mitigated by the two independent guarded worktree runs
(F5 commit `1dbfafb9`; F7 controlled run above) and the F4/F5 guards.

### 7.4 AC4 — serialisation with the companion cleanup

Ordering was serialised: the companion cleanup `SA-0MUG216UP008821M` completed
and was pushed (`e36dd153`) **before** this verification run, so no concurrent
ref deletion could be mistaken for a mutation. The initial pre-cleanup run that
saw a ref-count change (250 → 249) was a one-time concurrent event; two
controlled re-runs (cached and `--no-cache`) showed zero ref change.

### 7.5 AC5 — post-cleanup checkout

```
git branch --list 'origin/dev' 'feature-x' 'wl-OSL-1-test' 'wl-SA-001-test-feature'   # empty
git for-each-ref | grep -E 'feature-x|wl-OSL-1-test|wl-SA-001-test-feature|heads/origin/dev'  # none
ls -d root-file-repo   # absent
git tag --list 'backup-*'   # backup-corrupt-dev-1790283975, backup-real-dev-6d7b4b4f (retained)
```

### 7.6 AC6 — parent AC coverage

| Parent AC | Child verification | Status |
|-----------|--------------------|--------|
| AC1 hermetic git usage | F2 `tests/test_live_repo_mutation_guard.py`; F3 `skill/shared/tests/test_git_sandbox.py` | met |
| AC2 reproduction / guard proof | F1 `docs/dev/repro_live_repo_leak.py` (3/3 runs); F2 guard-fires proof | met |
| AC3 regression guard | F2 guard tests; F4 `run_tests.py` snapshot; F5 root conftest guard (+ SA-0MUH5KFDV002ORSS ordering fix) | met |
| AC4 live checkout clean | companion SA-0MUG216UP008821M (`e36dd153`); §7.5 above | met |
| AC5 documentation | F6 `skill/shared/test-writing-guidelines.md` + `skill/test/SKILL.md` | met |
| AC6 full suite passes | §7.1 (`success: true`, 0 failures) | met |

