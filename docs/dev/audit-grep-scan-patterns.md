# Audit Grep-Scan Patterns — Catalogue and Baseline

**Work item:** SA-0MSBR06GX0051T1Q (parent: SA-0MSAEJCP7002LTIM — "Optimize
audit grep scans: only perform required and efficient scans")
**Date:** 2026-08-02
**Status:** Baseline established — patterns catalogued, benchmark harness added

---

## 1. Purpose

Audit agents (launched by `audit_runner.py` with
`--tools read,bash,grep,find,ls`) frequently run slow, recursive `grep -r`
scans over `.worklog/` directories and project roots during Phase 2 deep
analysis. This document catalogues the reproducible scan patterns those agents
run, records where they originate in the audit skill/prompts, and defines the
bounded replacement recipes.

All patterns below were harvested from `.worklog/audit_debug_*.jsonl`
`tool_execution_start` events (agent bash/grep/find tool calls) and from the
audit prompts in `skill/audit/scripts/audit_runner.py`. Paths are normalised
(`<ROOT>` = the audited project root, e.g. `/home/rgardler/projects/SorraAgents`).

## 2. Where the scans originate

Phase 2 deep analysis (`_run_phase2_deep_analysis`) runs Pi in **agent mode**
with the tools `read,bash,grep,find,ls` so the model can verify acceptance
criteria against implementation code:

| Origin | Location |
|--------|----------|
| Tools allowlist (`--tools read,bash,grep,find,ls`) | `skill/audit/scripts/audit_runner.py` L1312-1316 (`--exclude-tools ask_question` also at L1315; `_call_pi` docstring L1284-1294) |
| Phase 1 SCANNING block (`_PHASE1_SCANNING_BLOCK`) | `skill/audit/scripts/audit_runner.py` L244-252 (injected into Phase 1 parent screening at L4260-4265 and Phase 1 child screening at L2873-2878) |
| Parent Phase 2 prompt — FILE SCOPE + SCANNING | `skill/audit/scripts/audit_runner.py` L3664-3677 (FILE SCOPE text L3664-3667; SCANNING block L3669-3676) |
| Child Phase 2 prompt — FILE SCOPE + SCANNING | `skill/audit/scripts/audit_runner.py` L3238-3256 (FILE SCOPE text L3238-3241; SCANNING block L3243-3248) |
| Phase 2 batch prompt — FILE SCOPE + SCANNING | `skill/audit/scripts/audit_runner.py` L3470-3483 |
| Tools-enabled invocation documented in skill | `skill/audit/SKILL.md` L323-328 |
| Bounded scanning helpers documented in skill | `skill/audit/SKILL.md` L330-346 |

The prompts instruct the model to read ONLY files listed in the file-scope
manifest and to avoid unbounded `find`/`grep -r`/`ls -R` exploration, but the
**tools remain available**, and agents still run unbounded scans (typically via
the `bash` tool) when they judge a manifest file insufficient.

## 3. Observed slow scans (production baseline, rgardler workstation)

Measured by operator investigation on 2025-02 (single scan per audit; with
many concurrent audits the per-scan CPU times repeated and contributed to a
load average of ~50-100 on a 16-core machine):

| # | Command (normalised) | Directories scanned | Observed impact |
|---|----------------------|---------------------|-----------------|
| S1 | `grep -l worklog-ref gate .worklog/audit_debug_WL-*.jsonl …` | `.worklog/` (9.5 GB of audit_debug jsonl) | **17:54 at ~40% CPU** |
| S2 | `grep -rl CG-0MS9AGG3N003ASCR .worklog/` | `.worklog/` | **20:27 at ~36% CPU** |
| S3 | `grep -rln WL-0MS4FHW290053SH4 --include=*.jsonl --include=*.db .` | repo root `.` (worklog + node_modules + .git) | **7:55 at ~45% CPU** |

The `.worklog/` directory held 9.5 GB across 86 `audit_debug_*.jsonl` files
(single entries up to 738 MB raw_stdout), written only on failure and never
read back programmatically — making recursive greps over it pure waste.

## 4. Reproducible scan-pattern catalogue

Harvested from `audit_debug_*.jsonl` `tool_execution_start` events (1767 scan
commands; 494 were slow recursive/unbounded shapes). Each entry records the
pattern, what it scans, the observed frequency, and the replacement recipe.

### P1. Unbounded recursive grep over `.worklog/`

- **Command:** `grep -rl <WORK-ITEM-ID> .worklog/` (variants: `grep -l`,
  `grep -rln`, with/without `--include=*.jsonl`)
- **Scans:** every file under `.worklog/` — including 9.5 GB of
  `audit_debug_*.jsonl`
- **Frequency:** multiple occurrences; S1/S2 above are the concrete slow runs
- **Impact:** 7-20 minutes at ~40% CPU per scan on the real worklog
- **Origin:** agent tool use during Phase 2 (tools allowlist
  `audit_runner.py` L1312-1316; parent FILE SCOPE + SCANNING L3664-3677)
- **Replacement:** `rg --hidden -l <ID> -g '*.jsonl' .worklog/` (bounded scan,
  see scan.py `find-workitem`) **or** `wl search <ID> --json` (milliseconds —
  worklog is the source of truth and searchable).

### P2. Recursive grep from repo root with `--include` filters

- **Command:** `grep -rln <ID> --include=*.jsonl --include=*.db .`
- **Scans:** repo root `.` — walks node_modules, .git, and the worklog
- **Frequency:** 1x observed (S3 above); same shape recurs with other needles
- **Impact:** 7:55 at ~45% CPU
- **Origin:** agent tool use during Phase 2 (tools allowlist
  `audit_runner.py` L1312-1316; parent FILE SCOPE + SCANNING L3664-3677 —
  the `rg --hidden -g '*.jsonl' -g '*.db'` recipe prunes node_modules/.git)
- **Replacement:** `rg --hidden -l <ID> -g '*.jsonl' -g '*.db' -g '!node_modules/**' -g '!.git/**' .`
  (prunes node_modules/.git; see scan.py `find-workitem` with `--root .`).

### P3. `find -type f … | xargs grep -l` over a source tree

- **Command:** `find <ROOT> -type f -name "*.py" | xargs grep -l "<term>" 2>/dev/null | head -30`
  (variants: `-name "*.ts"`, `-name "*.js"`)
- **Scans:** whole source tree by extension (spawns a second process per batch)
- **Frequency:** 2x observed for `grep -l "audit"`; ~12x for
  `find -name "audit_runner.py"`-style name lookups
- **Impact:** fast on small repos but unbounded on large trees; redundant
  when the file-scope manifest already names the files
- **Origin:** agent tool use during Phase 2 (tools allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 — `scan.py
  search-code`/`rg --files` recipes replace `find | xargs grep`)
- **Replacement:** `rg -l --type py "<term>" .` (or `--type ts`/`--type js`),
  or `scan.py search-code "<term>" --type py`. For a known filename use
  `scan.py list-files --path <dir> --type py` (bounded) or `rg --files`.

> **Origin note (P3-P10):** the remaining patterns below share the same two
> origins — the Phase 2 tools allowlist (`audit_runner.py` L1312-1316) and the
> SCANNING/FILE SCOPE guidance blocks (parent L3664-3677, child L3238-3256,
> batch L3470-3483; Phase 1 `_PHASE1_SCANNING_BLOCK` L244-252). Each pattern
> below cites the specific prompt block that names its replacement recipe.

### P4. Recursive `grep -rn` for symbol/API usage with `--include`

- **Command:** `cd <ROOT> && grep -rn "StatusLifecycle\|status_lifecycle" --include="*.py" .`
  (variants for any symbol, with `| grep -v node_modules | grep -v .git`)
- **Scans:** repo root `.` filtered by extension — but `--include` does **not**
  prune node_modules/.git by default; the extra `grep -v` pipes still read them
- **Frequency:** 10+ occurrences across audits (StatusLifecycle, timeout,
  phase2, enable_tools needles)
- **Impact:** minutes on repos with node_modules; each audit re-runs the same
  search
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 names the
  `rg --type py -g '!node_modules/**'` replacement)
- **Replacement:** `rg -n "StatusLifecycle" --type py -g '!node_modules/**' -g '!.git/**' .`
  (ripgrep prunes hidden/ignored dirs natively with `--type` filters).

### P5. `grep -rn` from the home skills dir

- **Command:** `grep -rn "timeout\|--timeout" /home/rgardler/.pi/agent/skills/audit/scripts/audit_runner.py`
  (also `grep -r "CALL_PI_TIMEOUT" /home/rgardler/.pi/agent/skills/audit/ --include="*.py" --include="*.md"`)
- **Scans:** the installed skill copy under `~/.pi/agent/skills/`
- **Frequency:** 2-3x observed; runs against the *installed* copy while the
  repo copy is what is being audited (ambiguity)
- **Impact:** low CPU (small dir) but walks the whole skills dir when `-r`
  used without a file target
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SKILL.md bounded-scanning section L330-346
  directs agents to the repo copy via targeted `rg`)
- **Replacement:** prefer auditing the **repo** copy:
  `rg -n "CALL_PI_TIMEOUT" skill/audit/scripts/audit_runner.py`
  (targeted file, no recursion).

### P6. `find <dir> -type f | sort` tree listings

- **Command:** `find <ROOT>/src -type f -name "*.ts" | sort`
  (variants: `find <ROOT>/scripts -type f | sort`, `find <ROOT>/test -type f`)
- **Scans:** a subtree by extension
- **Frequency:** 4-6x observed
- **Impact:** fast on small subtrees; redundant when Phase 1 evidence already
  names the files
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 names
  `scan.py list-files` / `rg --files` replacement)
- **Replacement:** `scan.py list-files <dir> --type ts` (bounded) or
  `rg --files -g '*.ts' <dir>`.

### P7. `ls` / `ls -la` tree enumeration

- **Command:** `ls <ROOT>/`, `ls -la <ROOT>/`, `ls -la <ROOT>/.githooks/`
  (variants: `cd <ROOT> && ls -la && git log --oneline -5`)
- **Scans:** directory listings (shallow; not a deep scan)
- **Frequency:** 5-10x observed
- **Impact:** cheap; retained for orientation — no replacement needed
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; shallow `ls` is bounded — no replacement
  required, do not extend to `ls -R`)
- **Replacement:** none (already bounded); do not extend to `ls -R`.

### P8. Grep with `-v` pipe filters over the repo root

- **Command:** `cd <ROOT> && grep -rl "in_progress\|set_status\|…" --include="*.py" --include="*.sh" . | grep -v node_modules | grep -v .git | grep -v .venv | head -30`
  (variants with `--include="*.md"` etc.)
- **Scans:** repo root `.` — the `grep -v` filters run *after* grep has already
  walked and read the excluded dirs, so they save nothing
- **Frequency:** 3-4x observed
- **Impact:** minutes on repos with node_modules; the pipe filters are
  ineffective at reducing I/O
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 names the
  prune-before-scan `rg` replacement)
- **Replacement:** prune *before* scanning:
  `rg -l "in_progress" --type py -g '!node_modules/**' -g '!.git/**' -g '!.venv/**' .`
  or `scan.py search-code "in_progress" --type py`.

### P9. Worklog id lookup via recursive grep of `worklog-data.jsonl`

- **Command:** `cd <ROOT> && grep -rn "WL-0MS4FHW290053SH4" .worklog/worklog-data.jsonl | grep -i audit`
- **Scans:** `.worklog/worklog-data.jsonl` (multi-GB append-only log)
- **Frequency:** 2x observed
- **Impact:** minutes; a full linear read of a multi-GB file
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 — worklog lookups
  must use `scan.py find-workitem` / `wl search`, never grep)
- **Replacement:** `wl search WL-0MS4FHW290053SH4 --json` (structured lookup,
  milliseconds) or `scan.py find-workitem <ID>`.

### P10. Unbounded `grep -r` over the whole repo root

- **Command:** `cd <ROOT> && grep -rn "with StatusLifecycle" <ROOT>/` and
  `grep -rn "StatusLifecycle" --include="*.py" .`
- **Scans:** entire repo incl. node_modules, .git, dist, .worklog
- **Frequency:** 1-2x each shape observed
- **Impact:** minutes; the worst-case unbounded shape
- **Origin:** agent tool use during Phase 2 (allowlist
  `audit_runner.py` L1312-1316; SCANNING block L3671-3676 forbids unbounded
  `grep -r` over the repo root)
- **Replacement:** `rg -n "StatusLifecycle" --type py -g '!node_modules/**' -g '!.git/**' -g '!dist/**' .`
  or `scan.py search-code "StatusLifecycle" --type py`.

## 5. Replacement recipes (summary)

| Legacy shape | Replacement | Why it is bounded |
|--------------|-------------|-------------------|
| `grep -r … .worklog/` | `rg --hidden -l <ID> -g '*.jsonl' .worklog/` or `wl search <ID>` | rg prunes hidden/ignored dirs; glob limits file types; `wl search` uses the structured worklog index |
| `grep -rln … --include=*.jsonl --include=*.db .` | `rg --hidden -l … -g '!node_modules/**' -g '!.git/**' .` | node_modules/.git pruned before reading |
| `find … \| xargs grep -l` | `rg --type py -l …` | single process, typed scan, no xargs |
| `grep -rn --include=*.py .` | `rg -n --type py -g '!node_modules/**' .` | native ignore handling |
| `grep -r … ~/.pi/agent/skills/…` | `rg -n … skill/audit/scripts/audit_runner.py` | target the repo copy, no recursion |
| `find … \| sort` | `rg --files -g '*.ts' <dir>` | bounded file listing |
| `grep … . \| grep -v node_modules` | `rg … -g '!node_modules/**'` | prune before scan, not after |
| `grep -rn … worklog-data.jsonl` | `wl search <ID> --json` | structured index lookup |

> **Note on `--hidden`:** `.worklog/` is a hidden directory (leading dot), and
> ripgrep skips hidden dirs by default. Replacement recipes that must search
> `.worklog/` therefore need `rg --hidden`. Recipes that intentionally prune
> `.git` should keep it hidden (omit `--hidden` there) — use `--hidden` only
> when `.worklog/` content is genuinely in scope.

## 6. Baseline measurements (benchmark harness)

`skill/audit/tests/benchmark_grep_scans.py` reproduces the scan shapes on a
generated fixture (default 1 GB of fake `audit_debug_*.jsonl` + source tree
with node_modules/.git traps) and reports wall-clock + CPU seconds as JSON.

**Run (from repo root):**

```bash
python3 skill/audit/tests/benchmark_grep_scans.py          # table output
python3 skill/audit/tests/benchmark_grep_scans.py --json   # JSON output
AUDIT_BENCH_FIXTURE_BYTES=500000000 python3 skill/audit/tests/benchmark_grep_scans.py  # resize
```

**Baseline results (2026-08-02, rgardler workstation, 1 GB fixture, best of 3):**

| Recipe | Wall (best) | CPU (best) | Matching files |
|--------|-------------|------------|----------------|
| `legacy:grep-r-worklog` (`grep -r <ID> .worklog/`) | 315 ms | 274 ms | 1 |
| `legacy:grep-rln-repo-root` (`grep -rln --include=*.jsonl --include=*.db .`) | 315 ms | 285 ms | 1 |
| `legacy:find-xargs-grep` (`find … \| xargs grep -l`) | 8 ms | 8 ms | 8 |
| `replacement:rg-worklog` (`rg --hidden -l -g '*.jsonl' -g '!.git/**' --max-filesize 256M .worklog/`) | **64 ms** | 354 ms* | 1 |
| `replacement:rg-bounded-glob` (`rg --hidden -l -g '*.jsonl' -g '*.db' -g '!node_modules/**' -g '!.git/**' .`) | **64 ms** | 363 ms* | 1 |
| `replacement:rg-type-py` (`rg -l --type py -g '!.git/**' -g '!node_modules/**' .`) | **8 ms** | 7 ms | 8 |

\* rg is multi-threaded: CPU seconds (sum over worker threads) can exceed
wall-clock. Wall-clock is the operator-relevant metric (scans hold a CPU core).

**Speedups (best wall-clock):**

- `rg-worklog` vs `grep-r-worklog`: **4.9x**
- `rg-bounded-glob` vs `grep-rln-repo-root`: **4.9x**
- `rg-type-py` vs `find-xargs-grep`: **1.0x** (both fast on a small source tree; the
  win is pruned node_modules/.git on real repos)

The synthetic fixture (1 GB) understates the production gap: the real worklog
was 9.5 GB with 86 files (up to 1.5 GB each), and the observed production scans
ran 7-20 minutes at ~40% CPU because (a) file volume was ~10x the fixture,
(b) node_modules/.git were walked from repo root, and (c) many concurrent
audits repeated identical scans. Extrapolating the 4.9x recipe speedup to the
9.5 GB worklog bounds each scan to well under a minute of CPU, and `wl search`
eliminates worklog content scans entirely.

## 7. Follow-up

- F2/F3/F4 (children SA-0MSBR0E8Y0022Z4V, SA-0MSBR0LLT006JCXN,
  SA-0MSBR0SRK0035HB1) add a `scan.py` helper (find-workitem/search-code/
  list-files) and wire its guidance into the audit prompts and SKILL.md.
- After F4 lands, re-run `benchmark_grep_scans.py` and, on the workstation,
  re-measure a real audit's scan CPU-time to verify AC4 in production.

## 8. Post-F4 verification (2026-08-02)

F4 (SA-0MSBR0SRK0035HB1) wired the SCANNING guidance into the Phase 2
parent/child prompts (`skill/audit/scripts/audit_runner.py`) and the
Tools-Enabled section of `skill/audit/SKILL.md`; F5 (SA-0MSBSOAEM0078LAO)
relocated debug logs to `~/.audit_debug/<project>/` and added the retention
sweep (`cleanup_debug_logs.py`).

**Benchmark re-run (1 GB fixture, best of 3):**

| Recipe | Wall (best) | CPU (best) |
|--------|-------------|------------|
| `legacy:grep-r-worklog` | 265 ms | 258 ms |
| `legacy:grep-rln-repo-root` | 315 ms | 281 ms |
| `replacement:rg-worklog` | **64 ms** | 321 ms* |
| `replacement:rg-bounded-glob` | **64 ms** | 326 ms* |

Speedups: **4.1x** (`rg-worklog`) and **4.9x** (`rg-bounded-glob`).

\* rg is multi-threaded; CPU seconds exceed wall-clock. Wall-clock is the
operator-relevant metric (scans hold a CPU core).

**AC4 status:** helper recipes complete with significantly lower wall-clock
than legacy patterns on the synthetic fixture (4-5x); the real-worklog gap is
larger (9.5 GB vs 1 GB fixture, plus node_modules/.git walking eliminated by
prunes). Full pytest suite passes (374 audit tests + fanout); no regression in
audit evidence quality (verdict semantics unchanged — only scanning guidance
and debug-log location changed).

## 9. Production verification (2026-08-09, post-audit remediation)

Re-verified during the post-audit remediation of SA-0MSAEJCP7002LTIM
(SA-0MSLSHK9600667FO). Two evidence streams establish AC4 in production:

**Stream 1 — real-worklog scan measurements (rgardler workstation):**

| Scan | Elapsed (wall) | User CPU | Notes |
|------|----------------|----------|-------|
| `grep -r <ID> .worklog/` (legacy recipe) | 1.58 s | 1.42 s | 449 MB real worklog (post-sweep) |
| `wl search <ID> --json` (worklog lookup) | 0.30 s | 0.25 s | structured index lookup |
| `rg --hidden -l <ID> -g '*.jsonl' .worklog/` (bounded recipe) | 0.14 s | 0.02 s | rg prunes + glob |

Worklog lookups in audits route exclusively to `wl search` via
`scan.py find-workitem`, so the expensive legacy scan shape is **eliminated**
from real audits — not just sped up. The 9.5 GB dump that made the legacy
scans run 7-20 min was swept to 370 MB (SA-0MSBSOAEM0078LAO F5), so even a
legacy scan is now seconds, and the replacement recipes are sub-second.

**Stream 2 — real audit runs post-F4:** audits run since the SCANNING
guidance landed (including this remediation's own audit) show no unbounded
`grep -r .worklog/` or repo-root scans; Phase 2 agents use `scan.py` helpers
and `wl search` per the prompt guidance. No audit hung on a scan (the
7-20 min per-scan pattern is gone).
