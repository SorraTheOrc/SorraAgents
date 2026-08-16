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
- `skill/cleanup/scripts/lib.py` (via `run_main` helper)
- `skill/effort-and-risk/scripts/orchestrate_estimate.py`
- `skill/triage/scripts/check_or_create.py`
- `skill/refactor/scripts/refactor.py`


## `pi_utils.py`

Shared Pi JSON-stream parsing utilities for skills that invoke `pi -p --mode json`
non-interactively. Canonical home for the parser (previously duplicated in
`skill/audit/scripts/audit_runner.py`).

### Usage

```python
from skill.scripts.pi_utils import extract_pi_text

raw = subprocess.run(..., capture_output=True).stdout
text = extract_pi_text(raw)
```

### Behavior

- `parse_pi_json_line(line)` parses one JSON-stream line into
  `(stream_text, should_print, complete_text)`.
- `extract_pi_text(raw)` assembles the final user-facing text, preferring
  complete blocks (`text_end`, `agent_end`) over accumulated deltas.
- User-role `message_start` events (the prompt echo) are skipped so a
  mid-stream model error never yields the prompt as extracted output.

### Consumers

- `skill/audit/scripts/audit_runner.py`
- `skill/audit/audit_pr.py`
