---
description: Health check the wiki. Detects contradictions, orphans, missing pages, stale claims, and knowledge gaps.
argument-hint: "[--fix]"
section: LLM Wiki
topLevelCli: true
---

# /wiki-lint

Run a comprehensive health check on the wiki.

## User Arguments

$ARGUMENTS

## Steps

1. Determine if auto-fix is requested: set `auto_fix=true` if `$ARGUMENTS` contains `--fix`, otherwise `false`.
2. Call `wiki_lint(auto_fix=<true/false>)` to run the health check.
3. Present the lint report to the user, including:
   - Page count, orphans, missing pages, contradictions
   - Knowledge gaps found
   - Any auto-fixes applied
4. If contradictions are found, flag them for human review — do NOT auto-resolve contradictions.
5. If knowledge gaps are identified, suggest creating pages for frequently-mentioned topics.
