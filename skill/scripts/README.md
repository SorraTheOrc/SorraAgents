# Shared Scripts Utility

This directory contains shared utility modules used across multiple skills.

## `failure_notice.py`

A standardized utility for surfacing script execution failures prominently in
skill outputs. When an automated script fails (non-zero exit code, timeout,
unavailable dependency, or runtime exception), the `FailureNotice` class wraps
the output with a prominent notice as both the **first AND last lines** of the
report.

### Usage

```python
from skill.scripts.failure_notice import FailureNotice

notice = FailureNotice(
    script_name="audit_runner.py",
    reason="Non-zero exit code: 1",
    stderr_context="... captured stderr ...",
)

# Wrap an existing report with the failure notice
wrapped = notice.wrap(report)
print(wrapped)
```

### Failure Notice Format

The notice uses the following format:

```
════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
The following output was produced manually.
════════════════════════════════════════════════════════

<existing report content>

════════════════════════════════════════════════════════
⚠ Script Execution Failure: <script_name> — <reason>
The following output was produced manually.
════════════════════════════════════════════════════════
```

### When to Use

Use `FailureNotice.wrap()` when a skill's automated script fails during
execution. The notice should appear as both the first and last lines of the
output, wrapping whatever partial or error output the script produced.

### Behavior

- The notice is purely informational/textual — no workflow state changes
- The existing output format sections are preserved; the notice is additive
- If stderr_context is provided, it's included in the notice block
- If the report is None or empty, a "(no output produced)" note is added

### Skills Using This Utility

The following skills use `FailureNotice` to surface script execution failures:

- `skill/audit/scripts/audit_runner.py`
- `skill/find-related/scripts/find_related.py`
- `skill/implementall/scripts/implementall.py`
- `skill/intakeall/scripts/intakeall.py`
- `skill/planall/scripts/planall.py`
- `skill/cleanup/scripts/lib.py` (via `run_main` helper)
- `skill/effort-and-risk/scripts/orchestrate_estimate.py`
- `skill/triage/scripts/check_or_create.py`
- `skill/refactor/scripts/refactor.py`
- `skill/ralph/scripts/ralph_loop.py`
