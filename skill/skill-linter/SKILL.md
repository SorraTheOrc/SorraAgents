---
name: skill-linter
description: "Lint SKILL.md and AGENTS docs for duplicated content and traceability noise; apply safe fixes only. Use when: 'lint skills'."
---

# Skill Linter

Scan the repository's agent-instruction documents for duplicated content and
traceability noise, report every finding with full context, and apply only the
clearly-safe fixes. Read-only by default; the scheduled run is always a dry run.

## Triggers

- `/skill:skill-linter`
- "lint skills", "skill hygiene", "deduplicate SKILL.md"

## Invocation

Resolve the skill directory at runtime; never assume a fixed repository layout.

```bash
python3 $(skill_path skill-linter)/scripts/lint_skills.py            # dry run (default)
python3 $(skill_path skill-linter)/scripts/lint_skills.py --json     # machine-readable report
python3 $(skill_path skill-linter)/scripts/lint_skills.py --apply    # apply safe fixes only
python3 $(skill_path skill-linter)/scripts/lint_skills.py --install-cron weekly
```

`--target PATH` scans one file or directory instead of the default scope;
`--allow-id ID` preserves a work-item ID; `--min-lines` and `--min-chars` tune
the duplicate thresholds; an explicit `--dry-run` overrides `--apply`.

## Lint scope

The default scope is every `skill/<name>/SKILL.md` one level deep, plus
`AGENTS.md` and `AGENTS_GLOBAL.md` at the repository root. Nothing else is
scanned unless `--target` names it. Directory targets expand recursively over
the same document names.

## Detection rules

Two families of finding:

- **Duplicates** — exact three-or-more-line blocks repeated within or across
  documents, where removing the extra copies is safe; and near-duplicate
  blocks that need human judgement and are recommendation-only.
- **Traceability noise** — non-allow-listed work-item IDs and workflow-comment
  lines such as session logging, dispatch and phase chatter, which carry no
  instruction and can be stripped.

The precise thresholds, heuristics, allow-list and report schema are defined
once in the [skill-linter reference](../../docs/dev/skill-linter-reference.md);
this brief does not restate them.

## Permission flow

1. Run the default dry run. It reports what would change and writes nothing.
2. Review each finding: file, section, line span, current text, proposed text,
   fix class and rationale.
3. **Safe fixes** — approve by re-running with `--apply`. It removes exact
   duplicate copies after the first occurrence, strips non-essential IDs and
   deletes workflow-comment lines. Commit the result.
4. **Recommendations** — near-duplicates are reported with full context and are
   never modified automatically. Decide per finding, edit the document by hand,
   and commit.
5. Re-run until the report is clean, then review the git diff.

## Weekly run

`--install-cron weekly` prints (does not install) a crontab line that runs a
dry run every Monday at 03:00 and appends a JSON report to
`.worklog/skill-linter-cron.log`. Add the printed line with `crontab -e`; the
schedule is not active until the operator installs it.

## Exit codes

- `0` — clean, or `--apply` cleared every safe finding.
- `1` — findings are still reported.
- `2` — usage or scan error (bad threshold, missing target, unknown schedule).

## Reference

- [Skill-linter reference](../../docs/dev/skill-linter-reference.md) — rule
  set, report schema, exit codes, `--apply` safety contract and cron helper.
- `$(skill_path skill-linter)/tests/` — detector, scan scope, report/CLI and
  documentation-drift tests.
