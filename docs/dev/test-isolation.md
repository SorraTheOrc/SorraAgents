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

The framework's own test plumbing does not rely on that misresolution
hypothesis either: it now *sets* a disk-backed `TMPDIR` for every suite
subprocess and prunes stale `pytest-of-<user>/pytest-*` roots ahead of
full-scope runs. See
[`test-skill-reference.md` §3 “Disk-backed test temporary storage and the
inode-exhaustion signature”](test-skill-reference.md#3-disk-backed-test-temporary-storage-and-the-inode-exhaustion-signature-sa-0mw2qed9m005fwok)
for the precedence order, the prune rules and the operator remedy.

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
   enclosing repository;
5. never inherit an ambient `GIT_INDEX_FILE` — the finish gate exports its
   temporary would-be-commit index for the suite it runs, and a fixture that
   inherits it stages into the *gate's* index instead of its own (see §9.7).

The single way to satisfy (3) and (5) is to run fixture git commands through
`shared.git_sandbox.run_git(...)`, or to pass
`shared.git_sandbox.fixture_git_env()` as the subprocess `env`. A command that
deliberately targets a specific index passes that path explicitly
(`env={"GIT_INDEX_FILE": path}`); the explicit value wins.

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

## 8. Live-repo guard exclusion contract (SA-0MUINEW6X0034C65)

Both guards (`run_tests.py` F4 outer snapshot and the repo-root
`skill/shared/live_repo_guard.py` inner per-test plugin) share
`skill/shared/git_sandbox.py` for their snapshot, fingerprint and diff
surfaces, so a change to the exclusion contract cannot make the two consumers
drift.

### 8.1 Surfaces and exclusions

The mutation surface is **refs + local `.git/config` + working tree**. Three
namespaces are deliberately excluded because their movement cannot be
distinguished from suite-caused movement (working-tree churn is attributed
separately — see §8.5):

- `refs/worklog/*` — managed by the `wl` tool;
- concurrent-infrastructure churn — every branch ref checked out in a
  registered worktree **other than the guard's own root** (ownership-based
  exclusion, SA-0MV8311B3005DNG8), **plus the `branch.<name>.remote` /
  `branch.<name>.merge` config that `git worktree add --track -b` writes for
  that branch** (which survives `git worktree remove`, so a register→remove
  cycle wholly inside the guard window is also invisible), **plus the
  remote-tracking refs of an excluded branch** (a concurrent finish fetches
  before fast-forwarding, so `refs/remotes/origin/<parent>` moves alongside
  the local branch);
- concurrency-excluded ref namespaces (WL-0MV5MYBKJ0033P8U): the exact ref
  `refs/heads/dev` and the whole `refs/remotes/origin/*` namespace. On a
  multi-agent host these are moved by **concurrent collaborative activity** —
  a sibling agent's `implement.py finish` fast-forwards the shared main
  checkout's `dev`, and concurrent `fetch`/`pull`/`push` moves
  `refs/remotes/origin/*` (including `refs/remotes/origin/dev`). Such movement
  is **indistinguishable from suite-caused movement**, so it is excluded from
  the diff and fingerprint; a hermetic fixture can never move these refs
  (only `fetch`/`pull`/`push` can create/move remote-tracking refs, and a
  suite never fast-forwards the integration branch). The excluded set is
  expressed as ref-namespace constants in
  `skill/shared/git_sandbox.py` (`CONCURRENCY_EXCLUDED_REF_EXACT` /
  `CONCURRENCY_EXCLUDED_REF_NAMESPACES`), derived from git plumbing
  namespaces — never a `wl-*` name pattern (which would also have excluded
  the incident's own fixture branches) and never directory names.
- the `wl` data-branch namespace `refs/heads/worklog/*`
  (SA-0MVV2W9610002CLW): every `wl` write and every `wl sync` moves
  `refs/heads/worklog/data`, so a sibling agent session moves it mid-run.
  It lives under `refs/heads/`, so the `refs/worklog/*` parser filter never
  sees it; it is excluded as a namespace prefix in
  `CONCURRENCY_EXCLUDED_REF_NAMESPACES` (`WORKLOG_HEADS_REF_NAMESPACE`) —
  never a `wl-*` name heuristic. Unlike `refs/heads/dev` and
  `refs/remotes/origin/*`, its movement is **reported as an informational
  note** (`EXCLUDED_REF_CHURN_NOTE_PREFIX`, rendered by
  `excluded_churn_notes()` and surfaced as `live_repo_excluded`), so the
  exclusion is auditable rather than silent.

A legitimately-created fixture branch that happens to be backed by a
registered worktree other than the guard root is therefore excluded, as is
the shared integration branch and the remote-tracking namespace.

### 8.2 Ownership rule and guard-root anchoring

The exclusion is **ownership-based plus concurrency-based**: every ref checked
out in a registered worktree that is *not* the root the guard runs from
(`snapshot_repo_state` threads the guard's toplevel into the worktree parser)
is a concurrent agent's infrastructure churn, regardless of the worktree's
directory name (`.worklog/worktrees/`, `.worklog/tmp-worktree-*`, or anywhere
else). The **guard root's own branch stays monitored** except where a
concurrency namespace applies — `refs/heads/dev` is the shared integration
branch on a multi-agent host, so a sibling finish's fast-forward of it is
indistinguishable from suite-caused movement and is excluded even when the
guard runs at the main checkout (see §8.1); every other own-root branch (e.g.
``wl-guarded`` in a linked worktree) stays monitored. A concurrent sibling
agent's `implement.py finish` (which fast-forwards the main checkout's
`dev` and registers/removes its own `.worklog/` worktrees) therefore no
longer trips either guard.

### 8.3 No false positives: fingerprint is a trigger, the diff is the authority

The per-test guard uses a cheap `fingerprint()` (refs + config only, no status
scan) as a **trigger**. When it moves, the guard confirms the movement against
the authoritative `diff_snapshots()` before failing; an empty diff re-baselines
and continues. `fingerprint()` also excludes the owned refs/config entirely, so
registration/removal churn does not even move it in the common case.
Consequently excluded-set churn alone can never fail a test with the bogus
`(no differences)` signature.

### 8.4 No false negatives: genuine mutations are still reported

Exclusion is scoped to the ownership/worklog/concurrency namespaces only.
Genuine mutations still fail the guard and name the offending test:

- an added/deleted/moved ref outside the excluded set (e.g.
  `refs/heads/intruder`, `refs/heads/feature-x`, `refs/worklog/`-less fixture
  branches);
- a rewritten local-config key outside the excluded set (e.g. `user.name`,
  `core.bare`, `branch.dev.remote`);
- a deleted tracked file or an added untracked file **that was clean at
  snapshot start** (a path that was already dirty and is resolved later is
  attributed to concurrent churn — see §8.5);
- a move of a monitored own-root branch (own-root branches other than the
  concurrency-excluded `refs/heads/dev` stay monitored).

**Residual risks.** Two blind spots remain and are accepted:

1. **Unregistered sibling feature-branch movement.** A sibling's feature
   branch that is NOT checked out in a registered worktree at either snapshot
   time (e.g. a scratch branch created and deleted outside the guard window,
   or a branch never attached to a worktree) is not ownership-excluded, so its
   movement can still trip the guard. There is no safe plumbing-derived
   criterion to distinguish it from a suite-caused mutation while keeping the
   incident class detectable (a name pattern is forbidden).
2. **`refs/heads/dev`-only movement is no longer a standalone incident
   signal.** Because the integration branch is concurrency-excluded, a
   mutation that moves *only* `dev` is no longer detected via the guard's
   ref surface. The SA-0MUIA3OE40001QJX leak class is still caught through
   the other surfaces — a `GIT_DIR`-leak incident also rewrites local config
   and/or the working tree (see §9), and those surfaces always fire — but a
   **clean** own-checkout fast-forward (the 2026-11-03 mixed-tree incident
   class: `git status` clean before and after, HEAD and tracked files
   rewritten by a concurrent `implement.py finish`) is invisible to every
   one of the guard's surfaces. That class is now detected on the new
   **own-checkout state surface** — `snapshot_own_checkout` /
   `compare_own_checkout_states` in `skill/shared/git_sandbox.py`, consumed
   by the test cache's `state_changed_during_run` flag (SA-0MW3FF18I003KGZE,
   §8.6) — which compares the guarded root's own HEAD and tracked-tree
   content around the run instead of the ref surface. Detection of a
   hypothetical *pure* ref-only `dev` rewrite is traded away for multi-agent
   false-positive immunity (WL-0MV5MYBKJ0033P8U); the own-checkout surface
   is the replacement signal for the incident class that ref-only detection
   used to cover.

Regression coverage:

- helper-level `excluded_refs` ownership (`TestOwnershipBasedExclusion`,
  `TestOwnershipExclusionBoundaries`), linked-worktree anchoring
  (`TestMainCheckoutAnchoring`), concurrency namespaces
  (`TestConcurrencyExcludedRefs`) and wl data-branch churn
  (`TestWorklogHeadsRefChurn`) in
  `skill/shared/tests/test_git_sandbox.py`;
- guard-level registration, `--track` registration, removal, concurrent-`dev`
  fast-forward, linked-worktree sibling churn, the config-rewrite and
  own-root-branch fires proofs, plus the concurrency-namespace clean-run and
  adversary proofs, in
  `tests/test_live_repo_mutation_guard.py`;
- outer-guard detection inherits the helper (no drift) in
  `skill/test/tests/test_guarded_run_all.py`.

### 8.5 Working-tree attribution (SA-0MUIJAHXS003M7BG)

The ref/config exclusions above cannot help with **working-tree** churn: on a
multi-agent host a guarded run shares its checkout with sibling sessions, and a
sibling that commits work it had *already dirtied* makes those paths leave the
status surface mid-run. Before this rule the guard reported exactly that as
``deleted path   [ M]`` and failed a run that mutated nothing (2026-10-19: 4427
passed, only two pre-dirty docs paths reported after a concurrent commit
``7de44be9`` landed inside the run window).

**The rule (option (a), pre-existing-dirty attribution).** Start-of-run
provenance, taken from the two ``git status --porcelain=v1 -uall`` snapshots:

| Snapshot difference | Verdict |
|---|---|
| path **present** at snapshot start, **absent** at session end | **excluded** — resolved during the run (a sibling commit/checkout of work it had itself dirtied); reported as an informational note |
| path **absent** at snapshot start, **present** at session end (modified, deleted or newly created) | **mutation** — clean→dirty damage is suite-caused |
| path present at both, with a **different status code** | **mutation** — no resolution is observable, so nothing is attributable |

The derivation is git plumbing only (status codes plus the start snapshot's own
``status`` map) — never a ``wl-*`` or path-name pattern (AC4). The excluded
paths are emitted as notes (``EXCLUDED_CHURN_NOTE_PREFIX`` in
``skill/shared/git_sandbox.py``), never silently dropped:

```text
note: excluded: concurrently resolved working-tree change in /home/…/repo: docs/dev/test-isolation.md [ M]
```

surfaced as ``live_repo_excluded`` (a list of notes) in the ``run_tests.py``
result JSON, as ``note:`` lines in its text output, printed by the inner plugin
at session end, and logged by ``implement.py finish``'s test gate.

**Blind spots (accepted, pinned).**

1. **Masked revert/delete of another session's *uncommitted* work.** A suite
   that reverts a sibling's pre-existing edit, or deletes its pre-existing
   untracked file, produces the same "resolved" signal as the sibling's own
   commit, so it is attributed away. Mitigations: the note names every
   excluded path, and the finish main-checkout guard (WL-0MVPADZ1Z000FOSG)
   remains a second net.
2. **Retained clean→dirty false positive.** The exclusion deliberately does
   not touch the incident direction: a path that becomes dirty from a clean
   start still fails the run, even if a sibling (not the suite) created it.
3. **Invisible content-only change to an already-dirty path.** A status-code
   surface never compared content, before or after this rule: an already-dirty
   path edited again keeps its code and yields no difference.

Regression pins: ``TestWorkingTreeAttribution``,
``TestWorkingTreeAttributionIncidents`` and
``TestWorkingTreeAttributionBoundaries`` in
``skill/shared/tests/test_git_sandbox.py`` (helper rule, the benign concurrent
commit from a second actor, the delete/modify/create incident cases and the
pinned boundaries), ``TestWorkingTreeAttributionNested`` in
``tests/test_live_repo_mutation_guard.py`` (inner-plugin parity) and the
``live_repo_excluded`` tests in
``skill/test/tests/test_guarded_run_all.py`` (outer-guard parity, AC3 note).

### 8.6 Own-checkout state surface (`state_changed_during_run`)

The guard's surfaces are **refs + config + working tree**, and §8.4 records
what they cannot see: a *clean* own-checkout fast-forward of the
concurrency-excluded `dev` rewrites the checkout's HEAD and tracked files
while `git status --porcelain=v1 -uall` stays clean before and after. That
incident class is now detected on a **separate, deliberately narrow surface**
— the guarded root's own state (SA-0MW3FF18I003KGZE):

- `snapshot_own_checkout(root)` captures the checkout's own HEAD commit plus
  a content fingerprint of the working-tree deltas the tree resolves to.
  It never reads the ref surface at all, so `refs/heads/dev`,
  `refs/remotes/origin/*` and `refs/heads/worklog/*` movement stays
  invisible *by construction* — a sibling fast-forward only matters when it
  rewrites this checkout's own HEAD/tree, which the fingerprint captures.
- `compare_own_checkout_states(before, after)` reports `moved` (with both
  commits) unless the difference is fully explained by the §8.5
  start-of-run working-tree attribution (a sibling committing work it had
  itself dirtied mid-run is churn, never a move — reported in
  `attributed`). An unfingerprintable checkout is fail-closed and never
  reported as movement.

The test cache brackets every executed run with this snapshot pair
(`skill/test_cache.py`); on `moved` it quarantines the verdict — no cache
entry under either state, a distinct exit status 75
(`STATE_CHANGED_EXIT_CODE`), the `state_changed_during_run` notice naming
checkout + before/after commits, and no parsed failure records — and the
finish gate/audit classify it as infrastructure (see
docs/dev/test-skill-reference.md, “Notice codes”). A stable run is unchanged.

Regression pins: `TestOwnCheckoutState` in
`skill/shared/tests/test_git_sandbox.py` (helper), the harness in
`tests/test_test_cache.py` (`TestStateChangedDuringRun`, end-to-end through
`run_cached`), and the runner/gate/audit surface tests in
`skill/test/tests/test_run_tests_failure_notices.py`,
`skill/implement/tests/test_finish_gate_state_changed.py` and
`skill/audit/tests/test_audit_runner_green_run.py`.


## 9. Production test-execution scrub (2026-09-26 recurrence, SA-0MUIA3OE40001QJX)

### 9.1 What happened

The 2026-09-24 fix (§5) neutralised repository-override variables for **test
fixtures** (`skill/shared/git_sandbox.py`), but the **production
test-execution paths** still inherited the full process environment. On
2026-09-26 an orphaned audit fan-out (30+ concurrent `audit_runner.py issue`
processes) exported `GIT_DIR=<live worktree git dir>` into the suite
subprocesses, so real-git fixtures operated on the live checkout: 24 junk
commits were created on `dev` and pushed to `origin/dev`. The remote was
force-restored, but the production path remained unguarded.

Root cause: `test_cache._default_runner` copied `os.environ` wholesale, and
`run_tests._run_cmd` inherited the parent env (no `env=`). Both are the choke
points every cached/audited suite run passes through.

### 9.2 Prevention layers (defence in depth)

| Layer | Entry point | Behaviour |
|-------|-------------|-----------|
| Shared scrub | `shared.git_sandbox.scrub_repository_overrides` (re-exported by `test_runner`) | Returns a copy of an env mapping with every `REPOSITORY_OVERRIDE_ENV_VARS` name removed; never mutates its input. |
| Cache runner scrub | `test_cache._default_runner` | Builds the subprocess env via the shared scrub + `~/.local/bin` PATH injection. |
| Test-skill runner scrub | `run_tests._run_cmd` | Passes an explicit scrubbed `env=` (previously inherited the parent env). |
| Audit pi-launch scrub | `audit_runner._call_pi` | Passes a scrubbed `env=` to the `pi` subprocess so a later test run cannot inherit a leaked override. |
| Startup diagnostic | `test_runner.log_repository_override_scrub` | Emits **once** per process, naming (never the values of) the stripped variables; silent when none are present. |
| Release-gate fail-fast | `run_tests.py --strict-git-env` (invoked by `.githooks/pre-push` for `dev`/`main` and documented in [`skill/ship/SKILL.md`](../../skill/ship/SKILL.md)) | Refuses to start (exit 2) when an override var is present, before any suite command runs. Operator opt-out: `RUN_TESTS_ALLOW_REPO_OVERRIDES=1` (loud warning; scrub still applied). A **worktree-managed** `GIT_DIR` (`<main>/.git/worktrees/<name>`, which git exports to hooks run from a linked worktree) is exempt via `shared.git_sandbox.is_worktree_git_dir` — it is git's own hook environment, not a leak, and refusing it blocked every implement-workflow worktree push (SA-0MUMR3QPM002VK7M). Genuine leaks (any other `GIT_DIR`, and every other override var) are still refused. |
| Release-gate subject binding | `.githooks/pre-push` → `run_tests.py --gate-commit <local_sha>` (SA-0MVTRBT50005DUEH) | The dev/main full-suite verdict is bound to the commit being pushed: the runner resolves a checkout whose `HEAD` is that commit with no dirty paths (the push cwd when it qualifies, else a clean `git worktree list` checkout) and runs/caches there, or refuses the push (exit 2) naming the commit and `TEST_SCOPE_SKIP=1`. A green `scope: full` entry computed from any other state — including the main checkout's own `HEAD` plus dirty `.worklog/` churn, which is how a red commit reached `dev` on another tree's verdict (MS-0MVM7NJF70037YU4) — is a miss, so the suite executes. The hook mirrors the resolved checkout's cache into the main checkout's `.worklog/cache` so post-push verification and audit F3 can see the entry. |
| Host-wide audit cap | `audit_runner.main` `issue` ingress | Bounds independently launched `audit_runner.py issue` processes via the shared `audit-host` flock semaphore (`AUDIT_MAX_HOST_AUDITS`, default 6 — right-sized via F7 to the 3-slot proxy pool, SA-0MUSZJRB9006ZCHE; `AUDIT_HOST_LOCK_TIMEOUT`, default 90s bounded wait). The N+1th process waits for a slot and exits cleanly with a clear message if none frees in time, instead of adding another concurrent suite runner. |
| Orphan detection | `audit_runner._find_orphaned_audit_processes` / `_warn_on_orphaned_audits` | Scans `/proc` for `audit_runner.py issue` processes with `PPID 1` and warns, so an orphaned fan-out is visible. |
| Detect-only live-repo guard | `run_tests.py` `_detect_live_repo_mutation` (+ the repo-root `conftest.py` plugin) | Unchanged inner net: fails a run that mutated its own checkout, but only *after* the mutation. |

Regression coverage: `tests/test_repository_override_isolation.py` (victim-repo
proof for every production path, plus an assertion that `refs/heads/dev` and
`refs/remotes/origin/dev` cannot be moved by a fixture),
`skill/shared/tests/test_git_sandbox.py` (scrub helper contract),
`tests/test_git_index_file_env.py` (**pass-through** contract: `GIT_INDEX_FILE`
is deliberately *not* an override and must survive the scrub so the finish
gate's temporary would-be-commit index reaches child `git ls-files` commands;
the file also fails if the name is ever added to
`REPOSITORY_OVERRIDE_ENV_VARS`, SA-0MVQHV9Y3001RNNW),
`skill/audit/tests/test_audit_runner_concurrency.py` (host cap + orphan
detection), `tests/test_run_tests_cache.py` (strict release gate,
including the worktree-hook exemption and the non-worktree refusal,
SA-0MUMR3QPM002VK7M), `tests/test_pre_push_gate_subject.py` (gate subject
binding: another tree's green entry cannot satisfy the gate, an entry at the
pushed clean state is served, a dirty push cwd still gates the clean worktree,
an unmaterialisable commit refuses and runs nothing, and the default
no-`--gate-commit` path is unchanged — SA-0MVTRBT50005DUEH), and
`tests/test_git_identity_guard.py::TestPrePushWiring::test_ambient_hook_bypass_var_is_scrubbed`
(ambient hook-bypass variables scrubbed from the hook subprocess env; see
§9.6, SA-0MUN83EXN004JBMW).

### 9.3 Residual risk — the external launcher (resolved at source)

The 2026-09-26 leak was **architectural**: an override variable was forwarded
by a launcher **outside** the `skill/` tree. The investigation
(**SA-0MUIZSXEY008NGR8**, evidence child **SA-0MV0G9BF7008S01K**) confirmed the
propagation boundary in the herdr launcher (ContextHub):

- `buildDowntimeSpawnOptions` in
  `packages/herdr/src/downtime-worker.ts` built the spawned pane environment as
  `{ ...process.env, HERDR_RESOLVED_CWD, AUDIT_PHASE2_PARALLELISM }`. Any
  `GIT_DIR` / `GIT_WORK_TREE` / `GIT_CONFIG*` / `GIT_OBJECT_DIRECTORY` /
  `GIT_ALTERNATE_OBJECT_DIRECTORIES` already present in the herdr process was
  forwarded to every pane and from there to `send-to-pi.sh` →
  `run-pi-agent.sh` → `pi` → test subprocesses. Git honours `GIT_DIR` over
  `cwd`, so a leaked value could redirect a `git` command into the live
  checkout — the in-repo scrub was the only thing standing between the export
  and the tests.
- Git exports `GIT_EXEC_PATH` and an empty `GIT_PREFIX` to `post-checkout`
  hook subprocesses (the incident's signature); a hook that forwards its
  environment carries that context further down the process tree.

A reproducible, non-destructive tracer,
`docs/dev/trace_git_env_propagation.py`, demonstrates the forwarding
(`spawn_forwards_overrides: true`), captures the hook signature, and dumps a
controlled fresh session. Evidence child **SA-0MV0G9BVW008M8AT** shows the
end-to-end state was clean at investigation time (live `/proc/<pid>/environ`
and `--session-env` both report no override), i.e. the forwarding was a latent
risk rather than an active leak — but it was still the mechanism by which any
future upstream export would have reached a session.

**Fix at the source** (cross-repo, per the incident's Q1 = A decision):
ContextHub **`WL-0MV0TZEWZ003ZXEB`** (commit `daa7d2a9`) —

- `buildDowntimeSpawnOptions` now forwards
  `{ ...scrubRepositoryOverrides(process.env), ... }`, dropping every
  repository-override variable before the spawn; and
- `send-to-pi.sh` / `run-pi-agent.sh` `unset` the same variables as a
  shell-side defence layer (so even a herdr path that bypasses the TS helper
  cannot forward them).

The §9.2 layers remain as defence-in-depth; with the export removed at the
launcher, the in-repo scrub is no longer the only protection. The SorraAgents
in-repo boundary is regression-tested by
`tests/test_repository_override_isolation.py` and
`skill/shared/tests/test_git_sandbox.py` (child **SA-0MV0G9CD1007A0IC**); the
ContextHub boundary by `packages/herdr/src/downtime-worker.test.ts`.

### 9.4 Recovery playbook for a recurrence

1. **Stop the bleeding.** Kill the offending run(s); do not let the process
   finish and push.
2. **Identify the victim refs.** Snapshot the checkout read-only (never
   `gc`/`fetch` on the live repo):
   ```bash
   git for-each-ref --format='%(refname) %(objectname)'
   git status --porcelain=v1 -uall
   git config --local --list
   ```
   The incident signature is `refs/heads/dev` moved onto `Test <test@test.com>`
   / `T <t@t>` / `Cache Test` / `RT Test` commits, fixture branches
   (`feature-x`, `wl-*-test*`), and a rewritten `.git/config`.
3. **Restore local `dev`** only after confirming the intended tip (the last
   legitimate push). Use `--force-with-lease`, never `--force`:
   ```bash
   git update-ref refs/heads/dev <legitimate-sha>
   git push --force-with-lease origin dev:refs/heads/dev
   ```
4. **Verify `core.bare` and identity.** A leaked `git init --bare` flips
   `core.bare`; repair the shared config:
   ```bash
   git config --local --get core.bare      # expect: false
   git config --local --get user.name
   ```
   The pre-push hook runs `scripts/check_git_identity.py` before `wl sync`
   (SA-0MUJ2VMF7000UHCX) and reports an actionable remedy when the effective
   `user.email` is empty, is a known incident sentinel (e.g. `t@t.com`), or a
   local override differs from the global identity — instead of letting the
   author-identity gate silently refuse the merge. Run it directly with
   `python3 scripts/check_git_identity.py --repo-root .`.
5. **Reap orphaned audits.** `ps -eo pid,ppid,cmd | awk '$2==1'` and look for
   `audit_runner.py issue`; the F5 cap prevents a new fan-out from forming.
6. **Record the incident** on the tracking work item and re-run the suite via
   `/skill:test` before any further push.

### 9.5 Verification evidence

- Full suite (pytest + node) green on the implementation worktree — see the
  work-item comment on SA-0MUIA3OE40001QJX for the exact command and result.
- `origin/dev`/`refs/heads/dev` fixture-immutability asserted by
  `tests/test_repository_override_isolation.py::test_poisoned_env_cannot_move_dev_or_origin_dev`.

### 9.6 Ambient hook-bypass variables (SA-0MUN83EXN004JBMW)

The §9.2 layers scrub **repository-override** variables, not the
``.githooks/pre-push`` **bypass switches** (`WORKLOG_SKIP_PRE_PUSH`,
`BRANCH_POLICY_SKIP`, `CONTEXT_BUDGET_SKIP`, `TEST_SCOPE_SKIP`). An operator
pushing from a worktree exports `WORKLOG_SKIP_PRE_PUSH=1` (the documented
tracked-hook bypass). When the suite itself is launched under that
environment, the variable is inherited by the pytest process and then by any
hook subprocess a test spawns, so the hook takes its early bypass exit
**before** the git-identity guard and `wl sync`.

This was a test-isolation defect, not a hook defect:
`tests/test_git_identity_guard.py::TestPrePushWiring` built its hook
subprocess env with `_env()`, which scrubbed only the repository-override
variables. Under `WORKLOG_SKIP_PRE_PUSH=1` both wiring cases failed, and
`run_tests.py` recorded those failures in the cache keyed by git state — a
gate poisoning its own push to `dev` (observed blocking SA-0MULPQ3BK001A1M5).

The fix keeps `_env()` an explicit, **allow-listed** scrub (never a blanket
`os.environ` clear, so `PATH`/`HOME` handling is untouched): it now also drops
`_HOOK_BYPASS_VARS` — `WORKLOG_SKIP_PRE_PUSH` plus the sibling switches the
hook reads — so ambient values never reach the hook subprocess. Each wiring
test re-sets the switches it needs explicitly, so the test controls them
rather than inheriting them. The production hook's documented
`WORKLOG_SKIP_PRE_PUSH=1` bypass is unchanged. Regression coverage:
`tests/test_git_identity_guard.py::TestPrePushWiring::test_ambient_hook_bypass_var_is_scrubbed`
(pollutes the parent environment, asserts `_env()` omits every bypass switch,
and asserts the mismatched-identity case still reports `git identity mismatch`
and skips `wl sync`).

### 9.7 Inherited `GIT_INDEX_FILE`: fixture rule and per-test guard (SA-0MVWQ4FZ4003S0GE)

`implement.py finish` stages the *would-be commit* into a temporary index
inside the repository's git directory and exports `GIT_INDEX_FILE` for the
suite it runs (SA-0MVQHVGFR006W8LG). That variable **deliberately survives**
the repository-override scrub (§9.2) so child `git ls-files` guards see the
staged tree (SA-0MVQHV9Y3001RNNW). The consequence for fixtures is direct:
every `git` subprocess a test spawns inherits the gate's index, so a helper
that runs raw `git init` / `add` / `commit` in its own throwaway repository
stages into the **gate's** index. The gate then refuses its own verdict —

```
gate evidence invalid: the temporary index changed while the suite ran …
Fixture git commands must use a hermetic environment
```

— and the failure is unattributable: the verdict belongs to the whole suite,
not to the test that wrote the index.

#### The rule

Fixture git commands use the hermetic helpers:

```python
from shared.git_sandbox import commit_all, fixture_git_env, init_repo, run_git

repo = init_repo(tmp_path / "repo", sandbox_root=tmp_path)   # own index, pinned identity
(repo / "f.txt").write_text("x\n", encoding="utf-8")
run_git(repo, ["add", "-A"], check=True)                     # never the inherited index
commit_all(repo, "init")
```

- `run_git` / `fixture_git_env` scrub the repository overrides **and** drop an
  inherited `GIT_INDEX_FILE` (`fixture_git_env`); `git_env_for_repo` — the
  *inspection* helper used by production paths — keeps the ambient index only
  when it belongs to the repository being inspected.
- A test that intentionally drives a specific index passes it explicitly
  (`run_git(repo, [...], env={"GIT_INDEX_FILE": str(index)})`); the explicit
  value wins over the scrub.
- The variables that must stay inherited (`GIT_INDEX_FILE`, and by design every
  other non-override variable) are pinned by
  `tests/test_git_index_file_env.py`.
- **Production paths that inspect a named repository scrub it themselves.**
  `git ls-files` applies an inherited index to whatever repository `cwd`
  names, so an inspection helper must run through
  `git_env_for_repo(<repo>)` (which keeps `GIT_INDEX_FILE` only when it lies
  inside that repository's git dir). `audit_runner._project_top_levels` now
  does: without it, every repository under the gate reported the *gate's* tree
  as its own, so the root-file-marker scope tests failed only inside
  `implement.py finish` (SA-0MVWJ6U3J003TLRZ). Regression coverage:
  `skill/audit/tests/test_audit_runner_launch_context.py::TestRootFileOnlyRepoManifest::test_foreign_gate_index_does_not_replace_the_repo_index`.

#### The guard

The repo-root `conftest.py` arms `skill/shared/inherited_index_guard.py`
(`IndexWatch`) at session start:

- it records the **inherited** index path, its staged tree (`git write-tree`)
  and a content digest — only when `GIT_INDEX_FILE` names an existing file, so
  an ordinary `pytest` run is unaffected;
- an autouse fixture polls it after **every** test and fails the offending test
  **by name**, with the index path, the tested tree vs the index tree now, and
  the `run_git` / `fixture_git_env` remediation;
- the stat signature is only a *trigger*: a rewrite that stages the same
  entries (a git stat-cache refresh) is not a violation, so there are no false
  positives;
- after reporting, the watch re-baselines on the current state, so only the
  first offending test is named instead of every later test in the session;
- it is bound to the index inherited at **session start**, so a test that
  exports its own `GIT_INDEX_FILE` on purpose (the finish-gate tests) is not
  affected;
- it stands down under `--collect-only` and under xdist workers, where a write
  cannot be attributed to the test in this process.

#### Relationship to the package-level env isolation

`tests/conftest.py` (session- and function-scoped), `skill/implement/tests/`
and `skill/test/tests/` keep autouse fixtures that *delete* the inherited
`GIT_INDEX_FILE` for their tests. Those remain deliberately, as a complementary
layer, not a substitute for the rule above:

- they cover tests that drive **production** git-inspecting code against a
  fixture repository (e.g. `refactor.session_boundary.get_all_source_files`,
  `generate_standup.commit_report_file`, `implement.phase_finish`). Production
  code must honour an index that belongs to the repository it inspects, so its
  environment cannot be scrubbed; the isolation has to happen at the test
  boundary instead;
- the guard therefore only ever fires where the inherited index is visible
  (`skill/*/tests/` packages without such a fixture, or a fixture that
  explicitly re-inherits it), and it names the test when it does;
- where the isolation applies, the guard is inert by construction, which is why
  the fixture-level rule (hermetic helpers) is the primary control and the
  sweep above matters even for packages that pop the variable.

#### Regression coverage

- `skill/shared/tests/test_inherited_index_guard.py` — `IndexWatch` unit
  behaviour (no inherited index → inert; hermetic fixture stays silent; raw
  fixture staging into the inherited index is reported once and re-baselined;
  a same-tree rewrite is not a violation) plus an end-to-end **simulated gate
  run**: the real `conftest.py` and guard modules are copied, unchanged, into a
  throwaway project whose suite contains one hermetic and one poisoning test.
  The hermetic run stays green and leaves the index byte-identical; the
  poisoning run fails and the output names the offending test and the
  remediation.
- `tests/test_finish_gate_temp_index.py` — the gate's own post-suite integrity
  check (the last line of defence when a nested suite has no conftest).
- The fixture sweep itself — `tests/` and `skill/*/tests/` helpers now route
  their git commands through the shared hermetic helpers (the sentinel case
  being `tests/test_test_cache.py`, whose raw `git add -A` produced the
  measured gate index corruption on 2026-10-31). Production paths that inspect
  a *named* repository keep an ambient index only when it belongs to that
  repository (`git_env_for_repo`), which is why
  `generate_standup.commit_report_file`'s default runner no longer inherits a
  foreign index.
- `skill/implement/tests/test_repo_root_resolution.py` clears
  `IMPLEMENT_DRIVE_ACTIVE` for the removal assertion: a driven child session
  defers worktree removal by design, so without it the test fails inside any
  driven session's own `finish` gate run — an environment-dependent failure of
  the same family as the ones this item fixes.

### 9.8 Per-work-item lock store isolation (SA-0MVWJ6U3J003TLRZ)

`implement.py phase_start` takes a **real** per-work-item `flock` at
`<worklog>/locks/child-<id>.lock` **before** any of the steps a test mocks
(claim, dirty-tree gate, worktree creation) — see [the implement
reference](implement-skill-reference.md#per-child-implementation-lock-sa-0mvq7vmrl00498yb).
Synthetic ids used by the implement tests (`SA-TEST`, `SA-TEST123`, …) share
the repository's own `SA` prefix, so `resolve_worklog_dir()` returned the
**live** store and every run wrote a real lock file into the developer's
`.worklog/locks`. Two concurrent pytest processes then raced the same id:
the loser's `phase_start` refused with
`Work item SA-TEST is held by implement@pid <n> (session <id>)` and the
choosing tests failed for reasons unrelated to the code under test (a
shared-state failure family, alongside the slot-contention reporting from
SA-0MVW1MJL6003767Q).

#### The rule

`phase_start` resolves its lock store through `_child_lock_dir()`, which
honours the **`IMPLEMENT_CHILD_LOCK_DIR`** override (unset in production, so
the worklog store resolved from the item id is authoritative). The value is
read per call, not at import time, so the override also isolates tests that
load `implement.py` **by file path** instead of importing it — the reason a
module-level `patch.object` on `_child_lock_dir` is not sufficient.

Every test boundary that drives `phase_start` sets it to its own throwaway
directory:

```python
@pytest.fixture(autouse=True)
def isolated_implement_child_lock_store(tmp_path, monkeypatch):
    store = tmp_path / ".worklog"
    (store / "locks").mkdir(parents=True)
    monkeypatch.setenv("IMPLEMENT_CHILD_LOCK_DIR", str(store))
    return store
```

- `tests/conftest.py` and `skill/implement/tests/conftest.py` each define this
autouse fixture, so every test in either package — including the root-level
`test_implement_*.py` modules that load `implement.py` by path — gets a
per-test store of its own.
- A test that asserts the lock **path** (e.g.
`skill/implement/tests/test_phase_start_child_lock.py`) patches
`_child_lock_dir` directly; its throwaway store still wins, because the inner
patch replaces the env-derived default for that test.
- Names are unique per test process (`tmp_path`), so two suites running
concurrently never contend for the same synthetic id anymore.

#### Regression coverage

- `skill/implement/tests/test_phase_start_claim.py::test_child_lock_lives_in_the_isolated_store_not_the_real_one`
— drives the real `phase_start` and asserts the lock file was created in the
isolated store and **not** in the live worklog store.
- `tests/test_implement_child_lock_isolation.py` — asserts the store resolves
to the isolated directory and that a real acquisition writes only there.

## 10. Tests never emit real release notifications (WL-0MUYA98Z8003HU8X / SA-0MU3VIW8C0035KKL)

A test run must be side-effect free with respect to external services. The
release integration harnesses run the **real** `run-release.js`, which reaches
Step 8.5 (`sendReleaseNotification`). A harness's temp project root has no
`.worklog/` config, so the **real** `discord-notify.js` would fall back to the
operator's global `~/.pi/agent/config.yaml` and POST a real Discord release
notification (e.g. `Release v9.9.9`) on every run.

The rule:

- Every test harness that executes `run-release.js` must **substitute a
  recording stub for `discord-notify.js`** instead of copying the real module;
  install it with `writeStubDiscordNotify()` from the shared helper
  `tests/helpers/release-notify-stub.mjs`;
- `tests/unit/test-run-release-merge-guard.mjs` writes a recording stub and
  pins this: Step 8.5 must be served by the stub (`stubbed: true`) and no
  notification may be emitted before the merge is verified;
- `tests/unit/test-run-release-fail-fast.mjs` covers the genuine-release path
  (`gitDescribe: 'v0.2.0'`) that previously POSTed a real `Release v0.2.0`; it
  now uses the shared stub and asserts Step 8.5 was stubbed, while its fail-fast
  no-POST tests deliberately keep the **real** module and point it at an
  in-process loopback webhook;
- `tests/unit/test-run-release.mjs`, `tests/unit/test-run-release-arg-filtering.mjs`,
  `tests/unit/test-code-freeze-marker.mjs` and `tests/unit/test-run-release-cleanup.mjs`
  use the shared stub for the `discord-notify.js` dependency;
- `tests/unit/test-run-release-discord-notify.mjs` is the reference pattern.

Integration tests that only assert **argument forwarding** (e.g. the
`--skip-checks` forwarding suite in `test-run-release-arg-filtering.mjs`) must
invoke `run-release.js` with `--skip-checks` so the wrapper's full gating-check
suite (git/`wl`/audit subprocesses) does not run inside the test's short spawn
window. Without it the gates contend with the rest of a parallel full-suite run
and can exceed the spawnSync timeout, making the merge script never run and the
assertion fail — flaky standalone-green, parallel-red (SA-0MVE01JCJ00243W1).
The gating behaviour itself is covered by dedicated gate tests; forwarding tests
stay fast and deterministic.

Production behaviour is unchanged — real releases still post the Discord
notification. The ship-skill detail lives in
[`skill/ship/docs/dev/ship-skill-reference.md`](../../skill/ship/docs/dev/ship-skill-reference.md#test-isolation).
