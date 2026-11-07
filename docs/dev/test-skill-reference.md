# Test skill — implementation reference

Deep implementation-reference detail relocated from `skill/test/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command documented
here is still valid.

## Test execution policy and conformance gate (SA-0MTYIMWQP0020ZUC)

`AGENTS_GLOBAL.md` carries an explicit **Test execution policy** (`## Test
execution policy`): agents must **never** invoke test commands directly — no
ad-hoc `pytest`, `npm test`, `vitest`, `node --test`, or equivalent — and
**every agent-initiated test run goes through the test skill** (`/skill:test`,
`run_tests.py`; full and changed scope, unit and smoke types), with no
exceptions. Automated gates that already route through this skill's runner
(the pre-push hook's `--scope full` runs, `implement.py finish` via
`guarded_run_all`, the audit skill's F3 auto-execution) are pipeline
infrastructure, not agent-invoked ad-hoc runs, and are out of scope.

The rule exists because only the test-skill runner populates the per-repo test
cache (§0 Cached execution below; git state + 2h TTL) that the audit skill
reads read-only via `query_cached()` to auto-verify execution-dependent ACs.
An ad-hoc run at the same git state is invisible to the cache and degrades
audit verification to F3 auto-execution or an operator `--green-run HEAD`
attestation.

**Conformance gate:** the policy is enforced machine-readably by
`tests/test_test_skill_directory.py` (AC3 of SA-0MTYIMWQP0020ZUC). It scans
every agent-facing doc (`AGENTS_GLOBAL.md`, `skill/*/SKILL.md`, `command/*.md`)
for command-shaped direct test-command patterns and fails when a *directive*
is introduced that is not an explicitly commented whitelist entry. The nested
exceptions are the legitimate infrastructure references: this skill's own
runner docs, git hooks/CI (`pre-push hook`, `--scope full`), the audit F2
suite-command resolution prose (`skill/audit/SKILL.md`, SA-0MSTMYE79006NA61),
and the single known resolve-pr-comments snippet tracked by SA-0MSN6FBFF009VCOB.
`<skill> ## Tests` sections are reworded to route through the test skill
(AC2), never deleted.

Note: doc-conformance tests scan **git-tracked** skill dirs only
(`tests/skill_inventory.py`), so an untracked `skill/<name>/` directory left by
concurrent WIP is excluded (SA-0MUU206QT001M66F) — its docs are not part of
the policy surface until committed.

### 0. Cached execution (default)

`run_tests.py` caches each suite run per-repo by default (see
[`test_cache.py`](../../skill/test_cache.py)): re-running the same command at the
same git state within the **2-hour TTL** is served from cache **without
re-executing the suite**. This prevents agents from burning 1.5–7 minutes
re-running an identical suite just to extract summary lines.

**Project-root resolution (SA-0MSNQV9J20010LE7):** the CLI targets the
*invoking* project, not the framework's install location. `main()` resolves
the project root at CLI time via `git rev-parse --show-toplevel` from the
current working directory (mirroring the audit skill's
`TARGET_PROJECT_ROOT`), falling back to the framework `REPO_ROOT` when cwd
is not inside a git repo. `--project-root <path>` overrides detection
explicitly. This means a run from e.g. the llm repo writes/reads
`<llm>/.worklog/cache/` with the fingerprint at llm's HEAD — and the audit
skill's read-only `query_cached(..., cwd=TARGET_PROJECT_ROOT)` continues to
hit the same entries because both resolve the same project root.

- **Storage**: `<repo>/.worklog/cache/` (fallback `<repo>/.git/test-cache/`,
  resolved worktree-aware). Gitignored; never committed.
- **Invalidation**: git-state fingerprint (HEAD sha + the **content** of
  every dirty tracked/untracked path, not just the `git status --porcelain`
  path list) + 2-hour TTL. Any content change — including a second edit to a
  file that is already dirty — an expired TTL, or a corrupt entry triggers a
  fresh run that replaces the stale entry (AH-0MVFGIV6700329SZ).
  `--force` bypasses lookup (still stores); `--no-cache` bypasses lookup and
  storage entirely.
- **Failed-run TTL (SA-0MSJELL44009XYIL)**: runs with a non-zero exit get a
  short 5-minute TTL instead of the full 2 hours, so a transient infra
  failure (e.g. `/tmp` disk quota) is never re-served as a current result for
  the full TTL — it expires quickly and the next query/run re-executes fresh.
  Green runs keep the full 2-hour TTL.
- **Pipeline normalization**: output-filtering pipelines (e.g.
  `npm test 2>&1 | grep -E "Test Files|failed"`, `| tail -30`, `| head`,
  `| tee`) normalize to the underlying run and share one cache entry.
- **Visibility**: non-JSON output marks cache hits with `[cached]` and names
  the pytest interpreter used (`[pytest interpreter: <path>]`). The concrete
  interpreter/executable is also stored in `metadata.json` (`interpreter`)
  — see *pytest interpreter resolution* below (SA-0MVK154IE007OS3D).
- **Verdict provenance in results (SA-0MVQHVJEX0071R7G)**: a run result
  states whether it was served from the cache and, when it was, which entry
  was reused. `run_suite()` returns `cached` (True only when every command of
  the suite was served) plus `cache_entries` — one mapping per cache-served
  command carrying the entry's `command`, `completed_at` (epoch float),
  `scope` and `fingerprint_kind` (`worktree` or `tree`). `run_all()` /
  `guarded_run_all()` aggregate the same two keys (`cached` is True only when
  every selected suite was cached). Consumers such as the implement finish
  gate therefore record *what* a green verdict is based on rather than only
  that something passed.
- **PATH augmentation for user-installed executables (SA-0MSUZAJPC003BS66)**: the
  default runner (`_default_runner` in `test_cache.py`) prepends
  `~/.local/bin` to the subprocess `PATH` when it is not already present, so
  user-installed executables (e.g. `pytest` installed via `pip --user`) are
  found when a suite command is spawned in a restricted environment (e.g. an
  audit runner whose PATH lacks the user-local bin directory). The path stays
  clean for callers that already have the directory configured (no duplicate
  entry).

#### Cache fingerprint contract (SA-0MVQHVHXI007PQAB, SA-0MVQHVKUH009VQMD)

The cache key is `command + fingerprint (+ test_type)`, and the fingerprint's
*provenance* is part of the entry, not just its value. Two kinds exist, and a
consumer can never be served one kind's verdict for a request expecting the
other.

**Worktree fingerprint (the default).** `compute_git_state(cwd, *, strict=False)`
combines the HEAD SHA with the **content** of every path `git status` reports
as dirty (tracked, staged, deleted, untracked), so a second edit to an
already-dirty file is a different state (AH-0MVFGIV6700329SZ). It reflects the
worktree containing `cwd`; an ambient `GIT_INDEX_FILE` is honoured only when
it belongs to `cwd`'s own repository (`git_env_for_repo`), never one inherited
from a foreign fixture repository (SA-0MVTET8KQ0031R6Z). With `strict=True`,
an unreadable work tree, HEAD, index or dirty path raises
`GitStateUnavailable` instead of degrading — a degraded fingerprint would make
every dirty tree share one key (WL-0MVPO9P610055UQ3).

**Tree fingerprint (would-be-commit).** `compute_tree_state(index_file, *,
cwd=None)` returns the SHA `git write-tree` reports for **`index_file`**,
run in `cwd` with the shared scrubbed environment plus an explicit
`GIT_INDEX_FILE`. That SHA is exactly the tree a commit built from that index
will contain: unlike the worktree fingerprint it sees generated, gitignored
metadata that is present on disk but untracked, so it binds a verdict to the
file set the suite actually validated
(SA-0MVL2NH1G0094FA2 AC4). It is **fail-closed**: a missing repository or an
unreadable index raises `RuntimeError` rather than falling back to the loose
worktree fingerprint (that fallback would reintroduce the unattributable
verdict it exists to prevent). The implement finish gate calls it through its
temporary index and passes the SHA in as the verdict's fingerprint.

**Provenance kinds.** `FINGERPRINT_KIND_WORKTREE` (`"worktree"`) and
`FINGERPRINT_KIND_TREE` (`"tree"`) are recorded as `fingerprint_kind` in each
entry's `metadata.json`. Entries predating the field read as `"worktree"` —
the historical default — so existing consumers keep their historical hits.

**Explicit-fingerprint override.** `run_cached(..., fingerprint=<sha>)` binds
a run to a caller-supplied fingerprint (normally a would-be-commit tree SHA):

- the value keys **both** the lookup and the stored entry, and the entry is
  recorded with `fingerprint_kind="tree"`;
- the loose `compute_git_state` value is **not computed at all**, so the run's
  cost and its key cannot drift with unrelated working-tree churn;
- the supplied value is stored **as-is** after the run — it describes the tree
  the caller named and cannot be recomputed from a possibly-churned worktree;
- callers that pass nothing keep the historical worktree keys byte for byte
  (test-skill CLI, audit). The pre-push gate is the one worktree-kind caller
  that *is* bound to a subject: it passes `--gate-commit <sha>` (see
  *0.4 Pre-push gate subject binding* below) and its key is the pushed
  commit's own clean worktree state.

`lookup(..., fingerprint_kind=...)` and `store(..., fingerprint_kind=...)`
expose the same distinction to callers that manage execution themselves.
`run_suite()` / `run_all()` / `guarded_run_all()` thread an optional
`fingerprint` through to the cache, so the finish gate reaches it through the
same canonical runner as every other consumer.

**Legacy and foreign entries miss (fail closed).** A cache hit requires the
command, `test_type`, `fingerprint_kind` and fingerprint to match, plus the
TTL (and, for exactness, the recorded `scope`). Therefore:

- an entry with **no** `fingerprint_kind` (a legacy entry) is read as
  `"worktree"`, so a **tree request always misses it** and the suite runs;
- an entry recorded for a **different kind** or a **different tree** is a
  miss;
- a corrupt or unreadable entry is a miss and never raises into the caller;
- `query_cached()` remains a read-only **worktree** query: the finish gate's
tree-bound entry is intentionally not visible to it (the audit's full-suite
evidence is recorded at the pushed commit's loose state, not at a
pre-commit staged tree).

### 0.4. Pre-push gate subject binding (SA-0MVTRBT50005DUEH)

SA-0MVTRBT50005DUEH. The dev-push pre-push gate is evidence about the commit
it pushes. `git` hands the hook `<local_ref> <local_sha> <remote_ref>
<remote_sha>` on stdin; `.githooks/pre-push` forwards `<local_sha>` as
`run_tests.py --scope full --strict-git-env --gate-commit <sha>`, and the
runner resolves **which checkout to measure** before anything else:

1. the invocation's project root, when its `HEAD` is the pushed commit and it
   has **no dirty paths** (`is_worktree_clean()` — the same
   `git status --porcelain --untracked-files=all` call the fingerprint uses);
2. else a checkout listed by `git worktree list --porcelain` whose `HEAD` is
   the pushed commit and which is clean;
3. else the push is **refused** (exit code 2) with the commit, every
   candidate tried and why it was rejected, and `TEST_SCOPE_SKIP=1`.

The resolved checkout replaces the project root for the whole run, so command
resolution, the per-command timeout, the suite execution and the **cache key**
all describe the pushed commit. A green entry recorded for any other state —
including the main checkout's own `HEAD` plus its dirty `.worklog/` churn —
is therefore a miss and the suite executes. No temporary worktree is
materialised (a fresh checkout has no installed dependencies, so a suite
failure there would not be about the pushed tree); pushing from a clean
worktree of the commit, or from a clean checkout at it, is the supported
remedy.

The gate reports the state it validated on **stderr** (stable prefixes; the
hook echoes them to the pusher):

```text
run_tests: gate-subject pushed=<sha> checkout=<in-place|worktree> fingerprint_kind=worktree
run_tests: gate-root <absolute path of the resolved checkout>
run_tests: gate-verdict verdict=<cached|executed> sha=<sha> git_state=<state> fingerprint_kind=worktree entries=<n> cached=<n>
run_tests: gate-refused <actionable message including TEST_SCOPE_SKIP=1>
```

The `gate-root` line is a contract with `.githooks/pre-push`, which mirrors
that checkout's cache entries into the main checkout's `.worklog/cache` —
keys are content-addressed, so copying an entry the main cache lacks is
always safe. Without `--gate-commit` the runner behaves exactly as before
(no subject resolution, no refusal, no `gate-*` lines), so every other
caller and every hook that does not forward the sha is unchanged.

**Propagation is per project.** `core.hooksPath=.githooks` makes the hook a
per-project file, so only a hook that forwards the sha is protected; the
runner-side flag reaches every project through the global skill install but
stays inert until its hook passes it. The verified inventory on this machine
(2026-11-03): SorraAgents forwards `<local_sha>` (this change); llm-manager
ships an independent v4 hook whose pushed-commit guard refuses any push whose
cwd `HEAD` is not the pushed commit (strict in-place — a deliberate divergence
that already binds the verdict to the commit, without the clean-worktree
fallback); AI_Hell, ContextHub, dev-scripts and Tableau-Card-Engine ship the
template v2 hook, which has **no** dev/main suite gate at all, so there is no
verdict to bind until they adopt the v3-style gate. See
`SA-0MVTRBT50005DUEH` for the recorded list.

Consequence for the finish gate: no green verdict can be served for a tree
whose suite never ran. A tree-keyed green exists only where the suite executed
against that exact tree, and a served green is reported as cached together
with the reused entry's metadata (see the
[implement finish-gate contract](implement-skill-reference.md#finish-gate-testing-the-tree-the-commit-produces-sa-0mvl2nh1g0094fa2)).

**`GIT_INDEX_FILE` pass-through (SA-0MVQHV9Y3001RNNW).** The finish gate's
tree binding depends on the suite inheriting its temporary index, so
`GIT_INDEX_FILE` must survive the runner's repository-override scrub —
`scrub_repository_overrides()` and `subprocess_env_with_scrubbed_overrides()`
preserve a set `GIT_INDEX_FILE` (name and value) while still stripping genuine
overrides such as `GIT_DIR`. It is deliberately **not** in
`REPOSITORY_OVERRIDE_ENV_VARS` (`skill/shared/git_sandbox.py`);
`tests/test_git_index_file_env.py` pins both halves (a scrubbed child
`git ls-files` sees the temporary index's file set, and a guard test fails if
`GIT_INDEX_FILE` is ever added to the scrub list). Fixture git commands must
use a hermetic environment (`shared.git_sandbox.fixture_git_env()`) so they do
not stage into an inherited index — see
[test-isolation.md](test-isolation.md#92-prevention-layers-defence-in-depth).

Query a cached run without executing anything:

```bash
# Print summary lines (Test Files / Tests / failed / passed) from the cache
python3 ./scripts/run_tests.py --summary --suite all

# Custom grep against cached output (read-only; never executes the suite)
python3 ./scripts/run_tests.py --summary --summary-grep "Test Files|failed"

# Force a fresh full run, refreshing the cache
python3 ./scripts/run_tests.py --force
```

Agents that only need summary information (e.g. release verification,
read-only audits) should use `--summary` instead of re-running the suite.

The **audit skill** consumes the cache directly (read-only, via
`query_cached()`): when the full suite was run green at the audited git state
within the TTL, the audit runner automatically verifies execution-dependent
acceptance criteria (e.g. "full project test suite passes") without any
operator attestation or re-execution (SA-0MSIU5HFI0024D7W). Suite commands
only cover node suite dirs that actually exist in the target repo
(`tests/node`, `tests/cli`, `tests/unit` — missing dirs are skipped,
SA-0MSJELL44009XYIL), so a repo whose layout diverges from that set can still
auto-verify; when it cannot, the runner prints a clear diagnostic naming the
missing/failed command and the remedy (`/skill:test` or `--green-run HEAD`).
Since F3 (SA-0MSTN5KRF0097TVP) a cache miss triggers auto-execution via this
skill's machinery instead of blocking; since F4 (SA-0MSTN8CWM003AAU9) the
audit never hard-blocks on execution-impossible repos.

### 0.5. Concurrency bounding (SA-0MTG5U75A001F1RG)

Concurrent test-suite executions are bounded host-wide via the shared flock
semaphore in `skill/shared/process_semaphore.py`.

**Separate namespace:** test runs use the **"test"** semaphore name, which is
completely independent of the **"audit"** semaphore. Holding the audit slot
never blocks a test run and vice-versa. Each workload has its own ceiling.

**Configuration via environment variables:**

| Variable | Default | Purpose |
|---|---|---|
| `TEST_MAX_CONCURRENCY` | `6` | Maximum concurrent **full-suite** executions (calibrated — see *Concurrency lanes*) |
| `TEST_MAX_CONCURRENCY_FAST` | `4` | Maximum concurrent **partial** runs (`--scope changed`, non-`full` types) |
| `TEST_LOCK_TIMEOUT` | `600` | Max seconds to wait for a free slot in either lane |
| `TEST_LOCK_JITTER` | `0.1` | Per-waiter stagger fraction of the bounded wait (`0` = off) — see *Staggered-wait contract* below |
| `VITEST_MAX_WORKERS` | `4` | Maximum workers in a single vitest suite (per-suite fan-out bound) — resolved value is **exported** to every suite command; see *Per-suite vitest worker bound* below |

The numeric env vars are validated at runtime: invalid values produce a stderr
warning and fall back to the documented default (never breaking the test run).
Values below 1 are clamped to 1.

### Concurrency lanes (SA-0MVWZQ269009SJXV)

Executions are bounded in two **independent** semaphore namespaces so a short
partial run can never queue behind a long full suite:

| Lane | Selection | Namespace | Ceiling |
|---|---|---|---|
| `full` | `--scope full` **and** `--type full` | `test` | `TEST_MAX_CONCURRENCY` (default 6) |
| `fast` | `--scope changed`, **or** any `--type` other than `full` (`unit`, `smoke`, `group:<name>`), or a per-test rerun | `test-fast` | `TEST_MAX_CONCURRENCY_FAST` (default 4) |

- The lane is derived from the **resolved** scope: a `--scope changed` run that
  falls back to the full suite reports — and contends in — the `full` lane.
- Cache hits never acquire a slot in either lane
  (SA-0MTG5U75A001F1RG AC4).
- `TEST_LOCK_TIMEOUT` bounds both lanes; the slot-busy notice names the lane
  and the ceiling env var that produced it.
- Every suite result carries `slot_lane` and `slot_wait_seconds` (queue time),
  kept separate from `elapsed_seconds` (execution time,
  SA-0MVU8XRBQ000DG9D); the aggregate adds `slot_lanes` and the summed
  `slot_wait_seconds`. Text output prints a `queued N s for a <lane>-lane test
  slot` line whenever the wait reaches 1 s.

**Why the ceilings are 6 and 4 (not 2).** The previous default of 2 was an
uncalibrated first cut. The 2026-10-31 production measurement
([test-ceiling-validation.json](test-ceiling-validation.json)) found 41 full
suites at 382–465 s (median 441 s) regardless of whether one or two ran
concurrently, each suite using < 1 core, the host 92 % CPU idle while this pool
sat at 100 % capacity, and 7.5–18.2 min slot waits for partial runs whose pytest
body took 0.65–2.88 s. Queue depth, not compute, was the binding constraint.

**Per-suite vitest worker bound (SA-0MVE98T1M006A17T):** the host-wide
`TEST_MAX_CONCURRENCY` ceiling bounds how many *suites* run at once, but a
single vitest suite normally sizes its tinypool to the CPU count (15+ forks
were observed from one suite, SA-0MSAEKOQE009TEB4). Every suite command the
runner executes therefore inherits `VITEST_MAX_WORKERS` in its **child
environment** — vitest's config resolution applies it as
`resolved.maxWorkers`, and `npm test` propagates it to the vitest process. The
default (`4`) matches the measured per-suite cap already applied to the
consumer repos (ContextHub / Tableau-Card-Engine, SA-0MSAK2ZH6009Z3TW).

- **Precedence:** an ambient `VITEST_MAX_WORKERS` (operator- or consumer-set)
  wins; the default applies only when it is unset. Values below 1 clamp to 1
  (serial); a non-numeric value warns and falls back to the default.
- **Env-only:** command strings are never rewritten, so the per-repo test
  cache key is unchanged and read-only consumers (the audit skill) still reuse
  entries. `NODE_OPTIONS` and every other consumer-set variable are preserved.
- **Escape hatch:** set `VITEST_MAX_WORKERS=1` for a suite that must run
  serially, or raise it for a repo whose tests are I/O-bound (env-var mechanism
  only; no config file).

**Bounded wait semantics:** unlike the audit runner's fail-fast slot wait, a
test run under saturation WAITS (bounded) for a free slot. If no slot frees
within `TEST_LOCK_TIMEOUT`, the run reports a clear failure notice (e.g.
`"test concurrency slot busy: no free slot within 600s"`) instead of failing
immediately or crashing. This means multiple agents running tests concurrently
will serialize gracefully — each waits its turn and succeeds once a slot
becomes free.

**Failure-notice contract (WL-0MVGUT787000LNDG, SA-0MVU8XRBQ000DG9D):** a
suite that fails WITHOUT structured test failures reports a stable
machine-readable `notice_code` alongside the human-readable `notice` —
`slot_busy` (`NOTICE_SLOT_BUSY`), `command_not_found`
(`NOTICE_COMMAND_NOT_FOUND`), `suite_timeout` (`NOTICE_SUITE_TIMEOUT`),
`killed` (`NOTICE_KILLED`) or `unparsed_failure`
(`NOTICE_UNPARSED_FAILURE`). `run_suite` also carries bounded
`stdout`/`stderr` tails (`_output_tail`, 4000 chars) and `run_all`
aggregates them into top-level `notice_codes`/`stdout`/`stderr`. Consumers
(e.g. `implement.py`'s finish gate) classify failures on the codes — never
by string-matching the notice text — so rewording a notice can never
silently reclassify a genuine test failure as infrastructure contention.
The slot-busy notice text itself is anchored to the exported
`SLOT_BUSY_NOTICE_PREFIX` constant.

**The pre-push gate names its reason (SA-0MVSJX5HS002DJHI).**
`.githooks/pre-push` no longer discards the runner's `--json` aggregate: on
failure it prints a stable `gate_reason=<code>` line plus a distinct human
diagnostic — `slot_contention`, `timeout`, `killed`, `unparsed`,
`test_failure`, `infrastructure` or `unknown`. Classification is delegated to
the shared `skill/test/scripts/gate_reason.py` classifier (the same one
`implement.py`'s changed-scope gate uses), which maps the published
`notice_codes` above onto those reasons — the codes are consumed, never
re-defined. The classifier itself is resolved through a tiered search order
(SA-0MVXEQE01008JW5R AC4): repo-local first (the runner's own directory,
`skill/test/scripts/`), then the installed global skill
(`$HOME/.pi/agent/skills/test/scripts/` — the same fallback the release gate
uses for `run_tests.py`), so a consumer repo without its own copy still
classifies from the installed skill. When no tier holds an importable copy
the failure is conservatively reported as `gate_reason=infrastructure` — never
`unknown`, never fail-open. The hook never fails open: an unparsable result is
reported as `gate_reason=unknown` and the push still exits non-zero, and a red
suite keeps `gate_reason=test_failure` with the unchanged "Full test suite
failed" wording.
`implement.py finish` reads the line from the push stderr and reports the
reason with its remedy instead of a generic "git push to dev failed".

**From codes to remedies.** The finish gate maps these codes onto the reasons it
reports (`timeout`, `killed`, `unparsed`, `slot_contention`, `test_failure`,
`infrastructure`) and attaches the deciding facts (`configured_timeout`,
`elapsed_seconds`, `slot_wait_seconds`, `command`, `exit_code`, `signal`). Read
the mapping — and the action per reason — in
[implement-skill-reference.md](implement-skill-reference.md#gate-failure-diagnostics-wl-0mvgut787000lndg)
and in `skill/implement/SKILL.md` (“Reading a gate failure”); a docs-vs-source
consistency test keeps the three documents and the producing constants in step.

#### Notice codes (stable strings)

| Code | Meaning |
| --- | --- |
| `slot_busy` | No host test slot freed within `TEST_LOCK_TIMEOUT`; no command ran. |
| `command_not_found` | The suite binary/executable could not be spawned. |
| `suite_timeout` | The command exceeded `timeoutPerCommand` (`TimeoutExpired`). |
| `killed` | The child was terminated by a signal (`returncode < 0`, e.g. an OOM kill or a worker crash) and produced no parsable failure. |
| `unparsed_failure` | The child exited non-zero and no structured test failure could be parsed from its output (bad flag, unloadable config, wrapper error). |
| `state_changed_during_run` | The guarded checkout moved while a command executed (HEAD and/or the tracked tree it resolves to changed mid-run, e.g. a concurrent `implement.py finish` fast-forward); the mixed-tree verdict is quarantined (no cache entry under either state), the command's parsed failures are dropped and the suite is reported non-green with the distinct exit status 75 (`EX_TEMPFAIL`). The result carries the detail (checkout path + before/after commits) on `state_changed_during_run` — a non-empty dict for the suite, a list under the `run_all` aggregate; the human output prints a `notice: [state_changed_during_run] …` line naming the checkout movement and the required re-run. Runs whose only difference is churn attributable to another session (start-of-run-dirty → end-clean resolutions) are never flagged (SA-0MW3FF18I003KGZE AC3). |

**Precedence.** Structured (parsable) test failures always win: `killed` and
`unparsed_failure` are set only when `failures` is empty, so a genuine code
failure is never reclassified as an infrastructure condition. The
`state_changed_during_run` flag is the single exception, and it wins **before**
the failure check: a moved checkout makes every parsed failure a mixed-tree
artefact that exists at no single commit, so a flagged run is always classified
as `infrastructure` (never `test_failure`) by the shared `gate_reason`
classifier and by `implement.py`'s finish gate. A non-zero result served
**from cache** is classified too — its stored exit code and output are enough,
and its elapsed time is reported as `null` (see below).

#### Diagnostic fact fields

Every `run_suite` result (and therefore every `run_all` per-suite entry)
carries these fields, on green and non-green runs alike, so no consumer has
to infer what happened from prose:

| Field | Unit / semantics |
| --- | --- |
| `configured_timeout` | Seconds (int) — the per-command `timeoutPerCommand` that was applied. |
| `elapsed_seconds` | Seconds (float) — wall time of the **command executions only**, queue wait excluded. `null` (never `0`) when nothing executed: a fully cache-served suite, or a slot wait that expired before spawning. On a `suite_timeout` result it is how long the command had been running when it was cut off. |
| `slot_wait_seconds` | Seconds (float) — time spent queued for a host test slot, measured **once** by the pacer's `on_wait` callback (`paced_runner`) and never folded into `elapsed_seconds`. On a `slot_busy` result it is the elapsed bounded wait (nothing executed, so the whole window was queue wait). `0` when no wait occurred. |
| `exit_code` | The command's process exit code (int), `null` when no child ran (timeout/slot busy/command not found). Negative means signal-terminated. |
| `signal` | The terminating signal number when `exit_code < 0`, otherwise `null`. |

**Aggregates (`run_all`).** Alongside the untouched per-suite detail under
`suites`, the aggregate carries `notice_codes`, `elapsed_seconds_total` (sum
of the measured command executions; `null` when no suite executed anything)
and `slot_wait_seconds` (sum of queue waits). Execution time and queue wait
are summed **separately**, so a queued run can never look like a slow suite
(or vice versa). The CLI's non-green output prints the same facts on a
`facts:` line.

**Staggered-wait contract (SA-0MVEDOF7A004TSWD):** under a saturated pool,
\*concurrent* waiters all enqueue with the same `TEST_LOCK_TIMEOUT` bound and,
without measures, would poll the slot pool at a fixed cadence and expire at one
shared wall-clock instant — then all report failure and re-run the suite in
lockstep, re-storming the pool (a thundering-herd wave). To break this, the
bounded wait is staggered per waiter:

- **Jittered deadline:** each waiter's deadline is reduced by a uniform random
  fraction of `TEST_LOCK_TIMEOUT` (default `TEST_LOCK_JITTER=0.1`, i.e.
  deadlines land in the final 10% of the bound). The configured bound is
  never extended — only staggered earlier — so no waiter ever waits longer
  than `TEST_LOCK_TIMEOUT`, and a herd that enqueues together expires across
  a spread of instants instead of one.
- **Jittered polling:** each retry sleeps `[0.5, 1.5] ×` the base 50ms cadence,
  so waiters do not hammer the flock in lockstep.
- **Retry guidance (AC3):** the saturation notice and `implement.py`'s pacer
  log explicitly instruct callers to wait a staggered delay (e.g. 30–120s)
  before retrying, rather than re-running immediately.

`TEST_LOCK_JITTER` accepts `[0, 1)`; `0` restores exact historical timing
(no stagger) for callers that prefer it. The jitter only affects the `"test"`
semaphore namespace — audit/`audit-host` semaphores keep `jitter=0.0`
byte-identical behaviour unless a caller opts in explicitly.

**Arrival-ordered turnstile (SA-0MVSJX5HS002DJHI).** Jitter only staggers
*deadlines*; it never orders arrivals. The slot pool alone grants a free slot
to whichever waiter polls next, so under a herd a late arrival can overtake a
waiter that has been queuing for hours (measured: one gate waited 2 h 9 min
before expiring with `slot_busy`). `process_semaphore.Semaphore` therefore
takes a monotonic ticket on every bounded/indefinite wait and only the waiter
holding the **lowest outstanding ticket** may attempt a slot. Tickets are
`flock`-held, so a waiter that acquires, times out, raises or dies (including
`SIGKILL`) releases its turn automatically from the kernel — a killed waiter's
turn is reclaimed within one retry cadence, with no stale tickets and no PID
bookkeeping. The wait is bounded by queue position: a waiter at position *N*
is served after at most *N* expected slot-hold times (plus one retry cadence).
Two compatibility guarantees are preserved: `timeout=0` fail-fast callers
(`audit_runner`) bypass the turnstile entirely (an immediate attempt still
succeeds when a slot is free and raises at once when the pool is full), and a
run isolated through `PI_SEMAPHORE_DIR` / `nested_semaphore_dir()` has its own
namespace and never queues in the host-wide turnstile. `TEST_MAX_CONCURRENCY`
is unchanged — fairness is about **ordering**, not capacity — and the fast path
adds one counter `flock` per acquire plus a single non-blocking scan when no
lower ticket is outstanding.

**Cache hits never block:** the semaphore is acquired ONLY when an actual
execution is about to happen (cache miss path). A cached result is served
without acquiring a slot, so read-only `--summary` queries and fast cached
hits are completely unaffected by concurrency saturation.

**Interaction with `--force`:** `--force` bypasses the cache lookup but still
acquires the concurrency slot before executing. Under saturation, a forced run
waits bounded rather than failing fast.

**Reusable pacer for other callers (`paced_runner`, SA-0MUKHCO02009EQFG):** the
test skill exposes `paced_runner(runner)` as the single entry point for other
code paths that execute suites through `run_cached`. It wraps a cache *runner*
so each real execution holds a `"test"` slot, while cache hits — which never
invoke the runner — consume no slot. `implement.py`'s finish gate (changed- and
full-scope, all tooling branches: pytest / npm / override / repo-script) now
passes its runners through `paced_runner`, so implement-gate executions are
bounded by the same host-wide ceiling as `run_tests.py` and can no longer
oversubscribe the host alongside audit-triggered or browser runs. On
saturation the gate returns a clear failed result (logged: `Implement
run_tests: could not acquire a test-run slot …`) and stores no cache entry —
raise `TEST_LOCK_TIMEOUT` to wait longer. `paced_runner(runner, on_wait=…)`
also reports `(queued_at, wait_seconds)` immediately after the slot is
acquired; implement-gate runs use this to emit a
`test slot acquired — queued_at=… wait_seconds=…` line, so contention is
observable rather than silent (SA-0MUA8BSAG000YZA2 AC3).

**Browser-suite contention (SA-0MUA8BSAG000YZA2):** with every execution path
paced by the shared `"test"` semaphore, cross-suite oversubscription is bounded
by `TEST_MAX_CONCURRENCY` — the historically observed 30+ concurrent
`vitest`/Chromium processes came from **unpaced** `run_cached` call paths
(implement.py finish gates and the audit F3 path, both now paced), not from a
raised cap. Repos whose suites spin up a 4-worker Chromium/browser stage and
that must never overlap another suite's browser stage should set
`TEST_MAX_CONCURRENCY=1` in the environment (env-var mechanism only; no config
file). Slot-file pruning beyond the ceiling is tracked separately
(ContextHub WL-0MUL1DSEF004IJU9).

### 0.6. Nested-run isolation (SA-0MUY6U05F008RG86)

A full-suite run holds a host-wide `"test"` semaphore slot for its whole
duration. Any test running inside it that invokes `run_tests.py` again —
in-process (`rt.main(...)`) or as a subprocess — is a **nested run**. If the
nested run acquires the same host-wide namespace, it queues behind the very
slot its own outer run holds; with two concurrent full suites and
`TEST_MAX_CONCURRENCY=2`, no slot can ever free and the nested acquire waits
out the whole `TEST_LOCK_TIMEOUT` (observed 2700s hangs in `implement.py
finish` and the pre-push gate). The observed offenders were
`tests/test_live_repo_mutation_guard.py::TestRunTestsGuardFires::*` and
`tests/test_pytest_path_resolution.py::test_run_tests_spawns_runner_interpreter_when_pytest_missing`.

**Contract:** a fixture that spawns (or calls) a nested runner sets
`PI_SEMAPHORE_DIR` to a per-test isolated directory for that invocation. The
nested run then bounds itself in its own namespace and never contends for the
host-wide pool. Top-level runs never do this, so the host-wide ceiling
(`TEST_MAX_CONCURRENCY`) and the bounded-wait semantics are unchanged for
genuine parallel suites — **only nested child runs are isolated.**

Use `nested_semaphore_dir()` from `skill/shared/process_semaphore.py` to name
the directory (re-exported by `run_tests.py`):

```python
from test.scripts.run_tests import nested_semaphore_dir

@pytest.fixture(autouse=True)
def _isolated_nested_semaphore(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_SEMAPHORE_DIR", str(nested_semaphore_dir(tmp_path)))
```

For a **subprocess** invocation, pass the overridden value in the child
environment (e.g. `_child_env()` in `tests/test_live_repo_mutation_guard.py`):
`env["PI_SEMAPHORE_DIR"] = os.environ["PI_SEMAPHORE_DIR"]`. For an
**in-process** call (`rt.main(...)` with the runner stubbed), set it on the
test process with `monkeypatch.setenv` before the call
(`tests/test_run_tests_cache.py` and `tests/test_pytest_path_resolution.py`).

Keep the isolated directory **outside** every fixture repository: slot files
newly created under the guarded checkout would otherwise be reported as a
live-repo mutation. `nested_semaphore_dir(tmp_path)` places it beside the
repo, not inside it.

**Suite-wide safety net.** The repo-root `conftest.py` additionally installs an
autouse fixture that points `PI_SEMAPHORE_DIR` at a fresh per-test directory for
**every** test in the session. That covers in-process runners whose file has no
bespoke override (e.g. `tests/test_run_tests_cache.py::test_strict_git_env_*`,
which was observed waiting ~287s for a host slot), so no test can leak a nested
acquire into the host-wide pool. The explicit fixture injections above stay as
the documented contract at the call sites; tests that exercise the semaphore
directly (`test_test_skill_semaphore.py`, the audit concurrency suites) override
`ENV_LOCK_DIR` themselves and are unaffected.

### 1. Suite-command resolution order (F2, SA-0MSTMYE79006NA61)

The full suite is `full_suite_commands(project_root)`, resolved in this
order (single source of truth shared by the test skill runner, the audit
runner's auto-execution path, and its read-only cache classification):

1. **`.pi/test-config.json` extension file** (repo root) — overrides
   convention detection entirely: `{"suiteCommands": ["..."], "timeoutPerCommand": 600}`.
   Use it for bespoke suites (e.g. a monorepo package command) that
   conventions would miss. `timeoutPerCommand` overrides the audit runner's
   default per-command timeout (600s, `AUDIT_TEST_SKILL_RUN_TIMEOUT` code
   constant — not an env var). A pinned `suiteCommands` list is returned
   **verbatim** — the hidden skill-test command is never appended to it; when
   the pin leaves project-local hidden skill test directories uncovered, a
   machine-readable, non-fatal notice reports the gap (D3, see *Hidden
   project-local skill tests* below).
2. **pytest** — `pytest -q -r a --disable-warnings` ONLY when the repo
   declares a pytest suite (`pytest.ini` / `[tool.pytest.ini_options]` /
   pytest-style `tests/**/test_*.py` files) — no phantom pytest for
   no-pytest repos (SA-0MSQ72BVV0011SRU AC3, `repo_has_pytest_suite`).
3. **Project-local hidden skill tests** — exactly **one** extra quiet pytest
   command scoped to all `.pi/skills/<name>/tests` (or `test`) candidates
   when any exist (SA-0MVQJSGLY0098DVB). pytest's default `norecursedirs`
   includes `.*` → `.pi/` is pruned from a recursive root run, so the
   canonical pytest command silently misses a project's skill tests; this
   command names every candidate directory **explicitly**, which pytest
   collects even though the default recursive pass prunes them. Emitted
   immediately after the canonical pytest command and before any node
   command. See *Hidden project-local skill tests* below for the rule, the
   D3 precedence matrix, the notice and the no-double-execution skip.
4. **Node suite dirs** — `node --test "tests/node/**/*.mjs"
   "tests/cli/**/*.mjs" "tests/unit/**/*.mjs"` (or `npm --silent test` per
   suite dir); only dirs that exist under the target repo are included.
5. **npm-test convention** — `npm --silent test`, emitted **only** when
   nothing above resolved (no pinned list, no pytest suite, no hidden
   skill-test candidates, no node suite dirs) and `package.json` declares a
   `test` script (TCE-like layouts).

An empty resolved set (no extension file, no pytest suite, no hidden skill
tests, no node dirs, no npm test script) is NOT an error: `run_tests.py`
reports zero commands and the audit skill treats the repo as
execution-impossible — fail-open partial, never blocks (F4 AC2).

### 1.1. Hidden project-local skill tests (SA-0MVQJSGLY0098DVB)

**Why this exists (the `norecursedirs` gap).** Pytest's default
`norecursedirs` includes `.*` — every dot-directory is pruned — so a
recursive `pytest` run from the repository root never traverses `.pi/`:
the tests a project ships with its *project-local* skills
(`<root>/.pi/skills/<name>/tests`) are silently absent from the "full"
suite. Measured on the llm repo: 5 264 collected recursively vs 529 in the
skill's own test directory (SA-0MVOBJGB4001TQ8X). Pytest's
`pytest_ignore_collect` hook only affects *explicit* initial paths, so
naming a directory on the command line collects it while the default
recursive run stays unchanged — that is the mechanism this rule
compensates for.

**Detection (D1).** A candidate is `<root>/.pi/skills/<name>/tests` or
`<root>/.pi/skills/<name>/test` that exists and contains at least one
`*.py` (recursive, mirroring `repo_has_pytest_suite()`).
`hidden_skill_test_dirs()` probes only those two literal path levels per
skill name — sorted and de-duplicated for deterministic command strings —
and never scans any other hidden root (`.claude/`, `.git/`, `.worklog/`).

**Emission (D2).** When at least one candidate exists,
`full_suite_commands()` returns **exactly one** additional command in the
canonical quiet pytest form listing every candidate, e.g.:

```
pytest -q -r a --disable-warnings .pi/skills/proxy-usage-analysis/tests
```

One command (not one per directory) keeps a single cache entry, a single
timeout budget and preserves the existing semaphore accounting
(`run_tests.py` acquires one host-wide slot per suite execution). Paths are
`shlex`-quoted. The command is emitted **immediately after the repo's
canonical pytest command and before any node command**, so the ordering is
stable. Emission is independent of `repo_has_pytest_suite()`: a project
with skill tests but no root `tests/` directory still gets the command. No
candidates → no extra command (byte-identical resolution to today).

**D3 — project-local configuration stays authoritative** (the framework
contract this must not weaken — SA-0MVO0EB1U007L7DS,
SA-0MV5SQZ0T004R8UG):

| Project-local input | Required behaviour |
| --- | --- |
| `.pi/test-config.json` `suiteCommands` (pinned list) | Returned **verbatim** — no appended command. The resolver records a machine-readable, non-fatal **notice** naming the hidden skill test dirs that the pinned list does not cover, so the gap is visible rather than silent. |
| `.pi/test-config.json` `timeoutPerCommand`, absent `suiteCommands` | Unchanged; convention detection (incl. the new rule) applies. |
| `.pi/test-config.json` `groups` | Group `commands` are project-owned and unchanged; the `full` group resolves through `full_suite_commands()` and therefore gains the extra command unless `suiteCommands` pins the list. `paths` matching unchanged. |
| `.pi/skills_extensions/test/extension.json` `types` | Type→command maps unchanged; the default/full type inherits the extra command through `full_suite_commands()`. |
| Project pytest configuration (`pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`) already listing a candidate in `testpaths` or `addopts` | That candidate is **skipped** (already collected by the canonical command) — no double execution. |

**No double execution (D3 coverage rule).**
`uncovered_hidden_skill_test_dirs()` drops any candidate the project's own
pytest `testpaths`/`addopts` already collects — read from the INI forms
`pytest.ini`/`setup.cfg`/`tox.ini` (sections `[pytest]`/`[tool:pytest]`)
and the `pyproject.toml` `[tool.pytest.ini_options]` form — from the extra
command, so those tests run exactly once. Coverage requires a token that
resolves to `.pi` itself or a strict descendant of `.pi`: a root-level
token (`.`, `tests`, `skill`) does **not** count, because the default
`norecursedirs` (`.*`) still prunes `.pi/` from those roots. Unreadable or
malformed pytest configuration is treated as absent (never an error) — the
coverage rule may only ever *add* the hidden skill-test command.

**Machine-readable notice (AC3).** When `suiteCommands` is pinned
(`extension_suite_commands()` returns a list) and that list does not cover
every candidate, `full_suite_notices()` — the single place consumers read
resolution notices from — carries a non-fatal notice:

```json
{"code": "hidden_skill_tests_uncovered", "source": ".pi/test-config.json",
 "directories": [".pi/skills/alpha/tests"], "message": "Pinned ..."}
```

with the stable `NOTICE_HIDDEN_SKILL_TESTS_UNCOVERED` code, the uncovered
`directories` and a human-readable `message`. The runner prints
`run_tests: notice [hidden_skill_tests_uncovered] …` on stderr. A notice
**never** changes the resolved command set or the exit status: the pinned
list stays authoritative and the gap is merely surfaced (a notice is never
a failure).

**Full scope only (D5/AC3 — changed scope untouched).** The extra command
belongs to `full_suite_commands()` — the `full` scope, the `full` group
and the default `full` type — and is **never** part of a changed-scope
subset: `changed_scope_commands()` continues to emit explicit-file subset
commands exactly as before, byte-identical whether or not the repo ships
candidates (regression-pinned by
`test_changed_scope_is_identical_with_and_without_candidates`). Hidden
skill tests are therefore enforced at **full scope only** — the dev-push
pre-push gate, the ship release gate and the audit's full-suite
verification. A changed `*.py` under `.pi/skills/<name>/tests` is still
selected individually by changed scope like any other test file, for fast
feedback (`test_hidden_skill_test_file_change_selects_that_file`).

**Project-specific note (SorraAgents):** this repo declares
`.pi/test-config.json` with `{"timeoutPerCommand": 1500}` (added under
SA-0MTYMFLZD004O2VV / commit 715806bb). The default 600s cap kills the full
suite (~412s measured wall-clock) before it can finish, so the timeout was
raised to 1500s (~3.6x margin) to allow both the test skill and the audit
runner's auto-execute path to complete. No `suiteCommands` override is set
— command detection continues to use the convention-based resolution above.

### 1b. pytest interpreter resolution (SA-0MVK154IE007OS3D)

The canonical pytest **command** (the cache key) is always
`pytest -q -r a --disable-warnings`. The **executable prefix** actually run is
resolved at execution time by `resolve_pytest_command()` in
`skill/test_runner.py`, replacing the historical bare-`pytest` rule
(SA-0MSQ012QG005N22S). Every candidate is **probed** (a cheap
`<candidate> --version`, run once per process via the memoised
`_PYTEST_COMMAND`) and skipped when it cannot run pytest — a failing or
foreign-interpreter candidate is never selected while a working one exists:

1. **`PYTEST_BIN`** — explicit pytest binary/command override, used verbatim
   when it runs.
2. **`TEST_PYTHON`** — explicit interpreter override (`<TEST_PYTHON> -m
   pytest`).
3. **`sys.executable -m pytest`** — the interpreter that started the runner,
   i.e. the project's interpreter. This is the fix for hosts where a PATH
   `pytest` script carries a system-Python shebang that cannot import the
   project's dependencies (collection then fails with exit 2 — e.g.
   `ModuleNotFoundError: No module named 'numpy'` — even though the repo's
   canonical `python3 -m pytest` run is green). Because `sys.executable`
   precedes the PATH `pytest`, the two orders can never disagree while the
   runner's interpreter can run pytest.
4. **`pytest` on PATH** (`shutil.which`) — only when earlier forms fail the
   probe.
5. **`~/.local/bin/pytest`** — retained user-install fallback
   (SA-0MSQ012QG005N22S; user-site bin dirs are absent from non-login
   shells). It sits between the PATH `pytest` and the final `python3`
   candidate: a concrete, probed, user-site install that carries the user's
   dependency set is preferred over the possibly-foreign PATH `python3`
   interpreter, while still never winning over the runner's interpreter.
6. **`python3 -m pytest`** — last resort when the runner's interpreter cannot
   run pytest but PATH `python3` can.

When every candidate fails the probe the resolved command falls back to bare
`pytest` and the spawn surfaces the error (never a silently narrowed suite).
Overrides are read on the first resolution in a process (the result is
memoised for the whole process), so export `PYTEST_BIN` / `TEST_PYTHON`
before invoking `run_tests.py` rather than mid-run.

**Auditability (AC2):** the concrete interpreter/executable selected is
recorded in the cache entry (`metadata.json` → `interpreter` field) and in
`run_tests.py` output (`[pytest interpreter: /usr/bin/python3]` on the
non-JSON suite line; `interpreter` in the per-suite JSON result and in the
`--summary` JSON `interpreters` map). The cache key stays git-state + command
+ type — the interpreter is informational metadata, so an entry is never
silently invalidated for having run under a different interpreter; the choice
remains visible and auditable. Direct `run_cached()` consumers — the audit
F3 auto-run and the implement finish gate — get the same behaviour through
`test_cache._default_runner`, which rewrites the executable prefix via
`executable_test_command()` exactly like `_cached_runner` does.

#### Field verification (AC4, SA-0MVK154IE007OS3D)

**Observed failure (host `RossGameDev`, repo ContentCreator, 2026-10-22, from
the origin report):** with the old bare-`pytest`-first resolution, a PATH
`~/.local/bin/pytest` carrying a system-Python shebang (`#!/usr/bin/python3`;
PEP 668 externally managed, no project dependencies) shadowed the runner's
interpreter. `python3 <skill>/test/scripts/run_tests.py --type full` then
exited 2 with 13 collection errors — the first
`ModuleNotFoundError: No module named 'numpy'` came from
`.pi/skills/wiki-tts-generate/scripts/tts_generate.py`, plus
`tests/test_podcast_parts_layout.py` and `tests/test_rss_process_notes.py` —
while the same tree was green under the repository's canonical runner
`./scripts/run_tests.sh` (2588 passed, 16 skipped). The red result was cached
by git state + scope, so one such run poisoned the audit-accepted full-suite
entry for that commit until a `--force` run replaced it.

**Post-fix behaviour:** the same command now resolves `sys.executable -m
pytest` (the interpreter carrying the project's dependencies, which precedes
a PATH `pytest` that fails its probe), executes the same tests the canonical
runner would, prints `[pytest interpreter: <path>]`, and records the choice
in the cache entry's `metadata.json` — so the green result matches
`./scripts/run_tests.sh` and is auditable. The resolution is executed by the
interpreter the agent started the runner with, which is the repository's
canonical interpreter (`python3 …/run_tests.py`).

**Measured parity (real ContentCreator checkout, 2026-10-29).** Verified on
host `rgardler-AI-Laptop` against ContentCreator `main` (`bf46a75`, the
`packages/ContextHub` submodule initialised, and the vendored
`.pi/packages/pi-llm-wiki-manual` dependencies installed with `npm ci` —
without them the repository's own runner fails three
`tests/test_rebuild_wiki_meta.py` cases on a fresh clone):

| Runner | Command | Result |
|--------|---------|--------|
| Canonical | `./scripts/run_tests.sh` | exit 0 — **3286 passed, 2 skipped** across the 7 suites |
| Test skill | `run_tests.py --type full --force --json` | success — **3286 passed, 2 skipped**, interpreter `/usr/bin/python3` |

Both runners therefore report the same green result, and the skill run names
the interpreter it used. In this repository the executed pytest command comes
from the repo-level pin (`.pi/test-config.json` → `suiteCommands` =
`python3 -m pytest -q -r a --disable-warnings`, tracked separately in
OSL-0MVJOKJXK009J8ZA), which agrees with the framework resolution. Per
producer decision 2026-10-29 (AC4), host-independent parity of this kind
satisfies the field verification; the literal `RossGameDev` host is not
required.

**Failure condition reproduced end to end (2026-10-29).** With a `pytest`
first on `PATH` whose `--version` succeeds but whose interpreter cannot
import the project's dependencies (the `RossGameDev` shape), the pre-fix
selection — a bare `pytest -q -r a --disable-warnings` — reproduces the field
failure exactly (`ModuleNotFoundError: No module named 'numpy'`, exit 2),
while the fixed resolver, run in the same checkout with the same poisoned
`PATH`, selects the runner's interpreter:

```text
which(pytest)               = <foreign-pytest-dir>/pytest
resolve_pytest_command()    = /usr/bin/python3 -m pytest
executable_test_command("pytest -q -r a --disable-warnings")
                            = /usr/bin/python3 -m pytest -q -r a --disable-warnings
```

**Repeatable verification (any host — no `RossGameDev` checkout required):**

```bash
# The green full-suite run names the interpreter actually used:
python3 <skill>/test/scripts/run_tests.py --type full --force --json \
  | jq -r '.suites | to_entries | map(.value.interpreter) | unique[]'
```

The configuration-independent regression
`tests/test_pytest_path_resolution.py::test_foreign_pytest_on_path_is_skipped_end_to_end`
reproduces the shadowing end to end (a temp repo whose PATH holds a
`pytest` bound to an interpreter that cannot run pytest), asserting that the
suite executes green under `sys.executable -m pytest`, that the JSON result
and output name that interpreter, and that the foreign shim never executes
the suite.

### 1a. Test types (`--type`) and the local extension contract (SA-0MTJQB2MA008HMO6)

`--type <TYPE>` selects the **command profile** (default `full`). It is
orthogonal to `--scope`: the type chooses *which* commands form the profile,
the scope chooses *full vs changed* within it.

**Minimum types** (accepted by every project):

| Type | Without a local extension |
|------|---------------------------|
| `full` (default) | `full_suite_commands(project_root)` (section 1) — unchanged. |
| `unit` | Convention subset: `pytest tests/unit` when the repo declares pytest and the dir holds `.py`; `node --test "tests/unit/**/*.mjs"` when it holds `.mjs`. |
| `smoke` | Convention subset: `tests/smoke` (same pytest/node rules). |

When no convention subset exists (e.g. `--type smoke` in a repo without
`tests/smoke`), the runner exits non-zero with a clear diagnostic — it never
silently runs the full suite.

**Local extension (machine-readable).** A project declares its own type→command
map at `<project_root>/.pi/skills_extensions/test/extension.json` (the
SA-0MSQ7MQEJ0064ZB0 convention, loaded via
`shared.skill_extensions.load_extension`):

```json
{ "types": { "unit": ["npx vitest run --project unit"], "e2e": ["npx playwright test"] } }
```

- Values are a command string or a **non-empty** list of command strings.
- A locally-defined type wins over the convention/minimum rules, so projects may
  add extra types (`dev`, `e2e`, `quick`, …).
- If the extension exists but omits a requested **minimum** type
  (`unit`/`smoke`), the runner fails with a diagnostic naming the type and the
  extension file — never a silent full-suite substitution. `full` that the
  extension omits still falls back to `full_suite_commands` so a bare invocation
  is never broken.
- A malformed `extension.json` (bad JSON, non-object top level, non-string
  commands) raises a clear error naming the file.
- Convention fallback applies only when no local `types` map exists (a
  prose-only `SKILL_PREFIX.md`/`SKILL_POSTFIX.md` extension does not define
  types).

**Unknown types** exit non-zero listing the allowed values — the minimum set
plus all locally-defined types.

**Cache and evidence.** Only `--type full` populates the audit-accepted
full-suite cache entry. The cache key namespaces non-`full` types
(`test_cache.cache_key(..., test_type=...)`), each entry's metadata records
`test_type`, and `query_cached`/`run_cached` match on it, so a typed run can
never be served as (or mistaken for) full-suite evidence. `run_suite`/`run_all`
results carry `type`; `--summary` reports the recorded type per suite
(`types` in JSON, `type=<T>` in text). The audit skill queries with the
default `test_type="full"` and is therefore unaffected.

```bash
python3 ./scripts/run_tests.py --type unit --json    # fast implementation feedback
python3 ./scripts/run_tests.py --type full --json    # release/audit evidence
```

### 2. Scope-aware execution (SA-0MT6BYQHB008DOGC)

`run_tests.py` supports execution **scope** so full-suite evidence is only
generated at the gates that need it (push to `dev`/`main`, release, audit),
while cheap changed-file-scoped runs back iterative validation.

**`--scope full|changed`** (default `full`):

- **`full`** — the complete suite (`full_suite_commands`, section 1). This is
the ONLY scope that populates the *full-suite* cache entry and therefore the
only scope the audit skill accepts as evidence of a green full suite.
- **`changed`** — only the tests affected by changes since the diff base.
Used for fast validation during feature-branch iteration (implement skill's
worktree test loop, ad-hoc agent validation); NEVER used as full-suite
evidence.

**`--target-branch <ref>`** sets the diff base for changed-file detection
(default `origin/dev`, falling back to `dev`). Changed files are computed as
`git diff --name-only <base> HEAD` (merge-base resolved automatically).

**Changed-file → test selection** combines:

1. **Named test groups** — the smallest group in `.pi/test-config.json`
   whose `paths` match a changed file wins and its `commands` run
   (SA-0MUJKF2HP001BR51). `suiteCommands` no longer disables selection: it
   defines the full suite only, never the changed-scope subset
   (SA-0MVL7YOMJ006UMDO).
2. **Convention mapping** — a changed file maps to its own tests:
   `src/foo.py` → `tests/test_foo.py`, a changed `tests/test_x.py` → itself,
   a changed `src/utils.ts` → `tests/unit/utils.test.ts`, a changed vitest
   `*.test.ts` / `*.spec.ts` (and the `.js`/`.tsx` variants) → itself. New
   (untracked) files are part of the change set so a test created before the
   first commit is selectable.
3. **AST import-graph expansion** (Python) — the mapping adds test files
   that import the changed module (deterministic, no heuristic coverage
   tools).

**TypeScript import-graph expansion is deliberately descoped
(SA-0MVL7YOMJ006UMDO, OPEN QUESTION 1 default).** The TypeScript side uses
only the conservative stem convention (`src/foo.ts` → `foo.test.ts` /
`foo.spec.ts` anywhere in the repo), plus a declared `groups` entry. A
trustworthy TS import graph needs a resolver for `tsconfig` paths and
package aliases; a wrong graph narrows the run and hides breakage, whereas
the stem convention can only ever *under*-match (and then fall back to the
full suite). The full-suite gate at the dev push still covers whatever a
narrowed selection misses, so under-matching costs time, not correctness.
Consequence: a changed `src/foo.ts` whose tests are named differently (e.g.
`tests/cli/commands.test.ts` importing it) falls back to the full suite
unless a `groups` entry matches the path.

Selected test files are passed explicitly (`pytest tests/test_foo.py ...`,
`node --test ...`, or `vitest run tests/unit/foo.test.ts`), so a scoped run
is deterministic and cache-keyed distinctly from the full suite.

**Deleted / non-existent paths (LP-0MTZYRTNF0092JKW).** A deleted or
renamed-away test file still appears in `git diff` even though it is absent
from the worktree. `map_changed_to_tests()` adds a changed test path only
when `(root / path).exists()`, and applies a final existence filter over the
whole selection (covering convention-mapped and import-graph entries too).
`changed_scope_commands()` repeats the filter defensively; if nothing
selectable remains (a deletion-only change) it returns `None`, forcing the
full-scope fallback. This prevents emitting `pytest <deleted-file>`, which
exits 4 ("file or directory not found") and would otherwise be reported as
a phantom test failure. The implement skill's changed-scope gate mirrors
this: a scoped pytest exit 4 is treated as "selection unavailable" →
full-scope fallback, while a genuine failure (exit 1) still blocks.

**Fallback to full scope** (with a logged warning) happens when no subset
can be selected, and the warning names the **specific** reason rather than a
generic list (SA-0MVL7YOMJ006UMDO):

| Reason | Meaning |
|---|---|
| `no changed files` | No diff against the base (and no untracked files). |
| `no changed source or test files` | Only docs/config changed. |
| `no matching group and no convention-matched test files` | Groups are declared but none match, and no test maps to the change. |
| `no convention-matched test files` | No group matched and no test maps to the change. |
| `selected test files have no runnable suite tooling` | A test is selected (e.g. a `.test.ts`) but no pytest/node/vitest runner is declared. |

A scoped run never silently skips testing. `changed_scope_selection()`
returns the reason alongside the commands (`None` commands = fallback);
`changed_scope_commands()` is the thin commands-only wrapper.

**The fallback honours pinned suite commands
(OSL-0MVNWYRQB0095WXQ).** When the fallback fires, the full-scope command set
is resolved through `full_suite_commands(project_root)` — the single source
of truth (section 1) — filtered by the requested suite name via
`filter_commands_for_suite()` (`pytest` → commands containing `pytest`,
`node` → commands containing `node`, `all` → every command).

- A repo that pins `suiteCommands` (`{"suiteCommands": ["python3 -m pytest -q"]}`)
therefore runs the **pinned** command for `--suite pytest --scope changed`,
exactly as `--suite all` does. The legacy bare `pytest` token is never
substituted on the fallback path, so a scoped run can no longer silently
re-resolve `pytest` to a different interpreter and report false collection
errors. Selection honours the pin *verbatim* (that is what
`run_suite`'s `command` result field reports); execution-time
`executable_test_command()` resolution (SA-0MVK154IE007OS3D) still normalises
only the pytest interpreter prefix, so `--scope changed` and `--scope full`
spawn byte-identical commands for the same suite.
- A **multi-command pin** (mixed pytest + node entries) maps each command to
its own suite name; `--suite pytest` runs the pytest entries only.
- When the filtered set is **empty** (e.g. a node-only pin queried as
`--suite pytest`), `run_suite` raises the unknown-suite-style
`ValueError` (`unknown suite: pytest — the repo's suite commands define no
matching command`) instead of silently running nothing and reporting success.
- The reporting contract is unchanged: a fallback still records
`scope: "full"`.
- Regression coverage: `skill/test/tests/test_run_tests_scope.py` →
`TestExplicitSuiteFallbackHonoursPins`. Because the execution-time
interpreter rewrite makes the legacy token and a `python3 -m pytest` pin look
alike in the spawned argv, the tests assert the selected command source
(`result["command"]`), which is the pin verbatim on the fixed path.

Direct full-scope runs (`--scope full`) keep the legacy name-based resolution
for `pytest`/`node` (section 1), and tool-less repos keep their legacy
fallback, so existing behaviour is untouched.

**Read-only summaries resolve the same way
(OSL-0MVNWYRQB0095WXQ).** `--summary` (`run_summary`) also resolves each suite
through `full_suite_commands(project_root)` filtered by suite name, so a
summary of `--suite pytest` in a pinned repo looks up the **pinned** cache
entry rather than missing it against a bare-token key. When the repo declares
suite commands but none match the requested suite (e.g. a node-only pin
queried as `--suite pytest`), the suite is reported as a miss (`missing`),
never as an empty success. A tool-less repo (empty full-suite command set)
keeps the legacy name-based lookup, unchanged.

**Result JSON** carries `scope` (`full`/`changed`) at the run level
(`run_all` outputs `scope` + per-suite `resolved_scopes`); `run_suite`
results carry `scope` on every path (normal, `FileNotFoundError`,
timeout). `--summary` prints `{suite} summary ({scope} scope):` and the JSON
summary carries per-suite `scopes` — a partial summary can never be
mistaken for full-suite evidence.

**Cache interaction:** scoped runs use independent cache keys
distinct from the full suite (the command differs; the stored metadata
also records `scope`). A `changed`-scope run NEVER populates the
full-suite cache entry — the audit's read-only full-suite query
(`query_cached`) filters by scope and rejects `changed` entries, so
full-suite verification always requires a genuine `full` run at the same
git state.

## 3. Disk-backed test temporary storage and the inode-exhaustion signature (SA-0MW2QED9M005FWOK)

### Why this exists

`/tmp` is frequently a small tmpfs whose **inode** budget — not its byte
budget — is exhausted first under multi-agent load. Stale
`pytest-of-<user>/pytest-*` roots (tens of thousands of tiny files each),
Playwright/Chromium browser profiles and node compile caches pushed `df -i
/tmp` to `IUse% 100` / `IFree 0` (~100 % of 1,048,576 inodes) while the byte
budget was only ~24 % used, and every suite/build subprocess silently defaulted
to that tmpfs because nothing set `TMPDIR`. The failure surfaced inside the
Playwright browser provider — `ENOSPC no space left on device` in
`PlaywrightBrowserProvider.createContext` — as a non-zero exit with no
parsable test failure, so the gate reported it as `unparsed`
(infrastructure-class). That classification was correct (the condition is host
infrastructure, never a code failure) but it blocked green work that a host
remedy, not a code fix, unblocks.

Two framework-side mitigations close the gap (feature epic
SA-0MW2QED9M005FWOK: F1 shared helper `skill/shared/test_tmpdir.py`, F2
runner wiring, F3 `implement.py` parity). Both are **fail-open**: an error is
logged with a reason string and the run proceeds unchanged.

### TMPDIR resolution (precedence)

`run_tests.py` resolves the test `TMPDIR` once per process
(`_suite_command_env()` → `shared.test_tmpdir.resolve_test_tmpdir()`) and every
suite command it spawns inherits it via the child environment. All suite
command strings, cache keys and scope resolutions are byte-identical — the
change is env-only.

`implement.py finish` resolves the same helper and exports the result into its
build, test and `git push` children, so the pre-push full-suite hook runs its
`run_tests.py` child on the same disk-backed storage. The explicit
`PI_TEST_TMPDIR` override therefore also steers the pre-push gate.

Precedence (first usable candidate wins):

1. **`PI_TEST_TMPDIR`** — explicit operator override. The directory is created
   when absent and must be writable; an unusable override is noted and the
   resolution falls through.
2. **`PI_TEST_TMPDIR_DISK_ROOT`** (default `/var/tmp`) — the configured
   disk-backed root; the candidate directory
   **`<root>/pi-test-<uid>`** is created when absent. Skipped when the root
   is missing, not a directory, or itself mounted tmpfs.
3. **`tempfile.gettempdir()`** — the portable final fallback for macOS,
   Windows and CI hosts without a usable disk-backed root.

The chosen directory is logged at INFO — `Test TMPDIR=<path> (<reason>)` from
the runner, `Finish TMPDIR=<path> …` from `implement.py` — so an operator can
verify which storage is active (`ls <root>/pi-test-<uid>` or `df -i <path>`).
Resolution failure is fail-open: the helper returns a last-resort path and
logs a warning; it never raises and never changes a run's exit status, cache
entry or scope resolution.

### Stale pytest-root pruning

Before a full-scope run, the runner calls
`shared.test_tmpdir.prune_stale_test_roots()` and removes stale
`pytest-of-<user>/pytest-*` roots under the resolved test TMPDIR *and* under
`tempfile.gettempdir()`. Rules:

- **user scope** — only the current user's `pytest-of-<user>` base is
  considered; another user's roots are never touched;
- **age threshold** — a root younger than `PI_TEST_TMPDIR_MAX_AGE_SECONDS`
  (default `3600` seconds = 60 minutes) is kept;
- **keep-current** — the `pytest-current` symlink target is always kept;
- **keep-newest-N** — the newest `PI_TEST_TMPDIR_KEEP_RECENT` roots (default
  `2`) are always kept, even when older than the threshold;
- **in-use skip** — a root referenced by a live `pytest` process is skipped
  (best-effort Linux `/proc` scan; a no-op elsewhere);
- **absent base** — no `pytest-of-<user>` base directory is a no-op.

The removed count and per-base outcome counts are logged at INFO; every error
path is caught and reported in the returned reason string (fail-open — a
prune problem never changes exit status, cache entry or scope resolution).

### Failure signature — recognise inode exhaustion for what it is

| Signal | Meaning |
| --- | --- |
| `ENOSPC: no space left on device` raised inside `PlaywrightBrowserProvider.createContext` (or another file-creation site) | The suite child hit the tmpfs inode (or byte) limit — host infrastructure, not a test failure. |
| `df -i /tmp` → `IUse% 100`, `IFree 0` while the byte budget looks fine | The `/tmp` tmpfs inode budget is exhausted; stale pytest roots and browser/node caches are usually the cause. |
| Gate `reason: unparsed` (runner notice code `unparsed_failure`) with no `failures` list | The gate correctly classifies the non-zero exit as infrastructure — never a code `test_failure`. |

A regression pin asserts the captured signature keeps this classification:
`skill/test/tests/test_gate_reason.py` (classifier) and
`skill/test/tests/test_run_tests_failure_notices.py` (runner), driven by the
shared fixtures in `skill/shared/tests/gate_failure_fixtures.py`.

### Operator remedy

1. **Prune stale pytest roots** — remove roots older than the threshold
   (`/tmp/pytest-of-<user>/pytest-*`), keeping the `pytest-current` target and
   the newest roots. The runner does this automatically before full-scope
   runs; a manual prune (roots with `mtime` older than ~20 minutes in the
   original incident) is the fastest intervention while a gate is blocked —
   the incident freed ~679k inodes this way.
2. **Free inodes** — inspect `df -i /tmp`; if pytest roots are not the cause,
   delete other churn (Playwright/Chromium profiles, node compile caches).
3. **Verify the disk-backed TMPDIR is active** — look for the runner's
   `Test TMPDIR=<path> …` INFO log (or `implement.py finish`'s
   `Finish TMPDIR=<path> …`) and confirm `df -i` on that path has inode
   headroom. Set `PI_TEST_TMPDIR` explicitly when a host needs a specific
   disk-backed location.

Related: `docs/dev/test-isolation.md` §H2 discusses `TMPDIR`/`--basetemp` in
the context of the live-repo-leak incident.
