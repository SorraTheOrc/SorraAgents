# Test skill — implementation reference

Deep implementation-reference detail relocated from `skill/test/SKILL.md`
(relocation tracked by SA-0MSLK7SAE0032V9K). The SKILL.md is the agent-facing
operational brief; this document preserves the full implementation reference
for maintainers. Workflow semantics are unchanged — every command documented
here is still valid.

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
- **Invalidation**: git-state fingerprint (HEAD sha + working-tree changes) +
  2-hour TTL. A changed tree, an expired TTL, or a corrupt entry triggers a
  fresh run that replaces the stale entry. `--force` bypasses lookup (still
  stores); `--no-cache` bypasses lookup and storage entirely.
- **Failed-run TTL (SA-0MSJELL44009XYIL)**: runs with a non-zero exit get a
  short 5-minute TTL instead of the full 2 hours, so a transient infra
  failure (e.g. `/tmp` disk quota) is never re-served as a current result for
  the full TTL — it expires quickly and the next query/run re-executes fresh.
  Green runs keep the full 2-hour TTL.
- **Pipeline normalization**: output-filtering pipelines (e.g.
  `npm test 2>&1 | grep -E "Test Files|failed"`, `| tail -30`, `| head`,
  `| tee`) normalize to the underlying run and share one cache entry.
- **Visibility**: non-JSON output marks cache hits with `[cached]`.

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

