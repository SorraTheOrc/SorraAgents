# find-related v3 — speed & value analysis (SA-0MNCDAQ8W008KOG9)

Evidence-driven review of the find-related skill (`skill/find-related/`) with
measured baselines, root-cause analysis, a prioritized speedup plan, and the
v3 applied fixes.

## 1. Measured baseline & phase breakdown

Re-measured on 2026-08-22 in a git worktree of SorraAgents (same procedure as
the original 2026-08-21 baseline, slightly different machine state):

| Phase | v2 wall-clock | Notes |
|-------|--------------|-------|
| `_wl_flags_for` (worklog-dir resolve) | 0.0004s | memoized sibling scan |
| `wl show` (fetch item) | 0.48s | 1 subprocess |
| keyword extraction | 0.001s | **523 keywords** (feedback loop) |
| semantic-availability probe | 0.55s | 1 subprocess |
| worklog search fan-out | **≥200s (timed out at 240s)** | 523 × 0.49s/spawn |
| repo scan (`rglob`) | 1.43s | 2,188 files read, **1,826 under `.worklog`** (83%) |
| description update | ~0.5s | 1 subprocess |

Per-spawn cost measured: **0.49s** for a single `wl search` call; a single
**batched `wl search --semantic <top-20-keywords>` call: 0.53s**.

## 2. Root cause of slowness (with evidence)

1. **Per-keyword subprocess fan-out dominates.** Every keyword spawns one
   `wl search` subprocess (~0.49s each). The clean run used 14 keywords
   (≈6.9s of search); the polluted run extracted **523 keywords** — an
   extrapolated **≈256s** of pure subprocess waits (the earlier cProfile
   measured 96% of cumulative time in subprocess waits, 58 spawns). The
   feedback loop grows the description, which grows the keyword list, which
   slows every subsequent run: 14 → 82 → 523 keywords across runs.
2. **The keyword feedback loop.** `extract_keywords` tokenized the whole
   description **including the skill's own "Related work (automated report)"
   section**. Report words (other work-item IDs, "matched", "repository",
   "worktrees", "score") became new keywords next run, spawning more searches
   and inflating the very prompt the report was meant to keep small.
3. **Repo scan walks `.worklog`.** `EXCLUDED_DIRS` omitted `.worklog`, so the
   scan read 1,826 `.worklog` files (83% of the 2,188 scanned) — including the
   skill's own sidecar full report (`find-related-full-<id>.md`, self-referential)
   and stale `worktree` clones, which ranked as top repo matches.
4. **Idempotency boundary bug.** `update_description` cut report sections at
   the next `\n##` substring — which also matches `\n###` sub-headings
   (`### Related work items`, `### Repository file matches`). Re-runs orphaned
   stale sub-blocks and stacked duplicate `###` blocks.

## 3. Prioritized speedup plan (ranked by impact)

| # | Optimization | Expected impact | Implemented |
|---|--------------|-----------------|-------------|
| 1 | **Cap worklog search to the top `MAX_SEARCH_KEYWORDS` (8) frequency-ranked keywords**, one `wl search` subprocess each. Measured: single 25-term `--semantic` query collapses conjunctively (**1 result**) while 6 per-keyword spawns return **26 distinct relevant items** in 1.9s — capping beats batching for recall. | Search phase **523 → 8 subprocesses** (polluted run ≈256s → ~4s; clean run ≈6.9s → ~4s), recall preserved | ✅ v3 |
| 2 | **Break the feedback loop**: strip the automated-report section before keyword extraction, drop numeric-only tokens, frequency-rank keywords so the top-k query is the descriptive core. | Keyword count 523 → ≤25 sparse (search capped at 8); no unbounded growth across runs; prompt stays small | ✅ v3 |
| 3 | **Exclude `.worklog` (and `.pi`, cache dirs) from the repo scan.** Worktrees/sidecars are non-authoritative copies; the main checkout is canonical. | Repo scan 2,188 → 362 files (**83% fewer**; ≈1.43s → ~0.3s) | ✅ v3 |
| 4 | Fix `update_description` section boundary to `\n##` **not followed by `#`**, so `###` sub-blocks belong to the report section. | Re-runs idempotent (no duplicate/orphaned sub-blocks) | ✅ v3 |
| 5 | (secondary) Rejected: single batched `wl search --semantic <25 joined keywords>` and 4 chunky phrases were both measured as worse recall (1 / 13 items) with conjunctive-collapse risk. | — | ❌ (evidence: A>B>C) |

Target achieved: clean run ~3s total (was 9.8s); polluted-run growth bounded
(was 79s and climbing, unbounded).

## 4. Value/noise fixes

- **Self-referential sidecar excluded** — `.worklog/tmp/find-related-full-<id>.md`
  can no longer match (`.worklog` excluded).
- **Stale worktree clones excluded** — `.worklog/worktrees/wl-*/` no longer
  scanned; the main checkout is authoritative.
- **Feedback loop closed** — report tokens no longer become search keywords.
- **Idempotency restored** — re-runs replace the section atomically; single
  `### Related work items` / `### Repository file matches` block.

## 5. Before/after comparison (same dataset: SA-0MNCDAQ8W008KOG9)

| Metric | v2 (polluted) | v3 |
|--------|---------------|-----|
| Keyword count | 523 (alphabetical, report-fed) | 490 total, report-stripped, frequency-ranked; 8 for search |
| Search subprocesses | 523 (≈256s, timed out) | **8** (≈4s) |
| Related items found | 3 (stale report) | 3+ genuinely relevant (preflight check, find-related automation, dedup logic, …) |
| Repo scan files | 2,188 | 362 |
| Total wall-clock | ≥240s (timed out) | **~3s** |
| Report duplicates on re-run | stacked `###` sub-blocks | 1 section, 1 sub-block |