# Bug Report: `intakeall` script crashes on `_contains_questions` due to `_extract_pi_text` returning non-string

**Reported**: 2026-06-24
**Script**: `skill/intakeall/scripts/intakeall.py`
**Severity**: High — blocks batch intake for all `idea`-stage work items

---

## Symptom

When `IntakeAllEngine.run_all()` processes an item that requires `/intake` invocation, the script crashes with:

```
AttributeError: 'list' object has no attribute 'lower'
```

Full traceback:

```
File ".../intakeall.py", line 536, in _contains_questions
    lower_text = text.lower()
                 ^^^^^^^^^^
AttributeError: 'list' object has no attribute 'lower'
```

The caller is at line 342:

```python
if self._contains_questions(intake_text):
```

where `intake_text` is the return value of `self._extract_pi_text(raw_stdout)`.

---

## Root Cause 1: `_extract_pi_text` returns a `list` instead of `str`

The method `_extract_pi_text()` (around line 515) is supposed to return a `str` — either the last element of `complete_blocks` or `"".join(delta_parts)`. Both code paths should produce a `str`.

However, when the `pi` JSON-stream output does not match any of the expected event types (`message_update`, `message_end`, `turn_end`), both `complete_blocks` and `delta_parts` remain empty. The function then falls through to:

```python
return "".join(delta_parts)
```

This returns `""` (a string). So the empty-output case is handled correctly.

**The more likely root cause**: The `pi --mode json` output format may contain events or payload shapes that the parser doesn't handle. For example, if `message.get("parts")` contains a part whose `.get("text")` returns a list rather than a string (e.g., a structured tool-call response), then `complete_blocks.append(part_text)` could append a list to `complete_blocks`. Subsequently, `complete_blocks[-1]` returns that list, and the function returns a `list` instead of a `str`.

Let's trace the exact scenario:

```python
for part in (message.get("parts") or []):
    if isinstance(part, dict):
        part_text = part.get("text", "") or part.get("content", "")
        if part_text:
            complete_blocks.append(part_text)
```

If the `pi` JSON output includes a part where `text` is a list (possible with tool-call or multi-part responses), `part_text` would be that list object, which is truthy, so it gets appended. Then `complete_blocks[-1]` returns a list.

**Evidence**: The crash occurred during the first invocation of `/intake`, meaning the pi output contained a message format that `_extract_pi_text` did not correctly handle as a plain text string.

---

## Root Cause 2: `/intake` command is interactive and blocks in batch mode

Even if the text-extraction bug is fixed, the `_invoke_intake` method invokes:

```python
intake_cmd = ["pi", "-p", "--mode", "json", f"/intake {item_id}"]
```

The `/intake` command runs an interactive interview loop (it asks clarifying questions and waits for user responses). When run in batch mode via `subprocess.run()`, the `pi` process blocks indefinitely waiting for stdin input, until the `item_timeout` expires (default 600s).

This means the intakeall script cannot work for items that require `/intake` — it can only auto-complete items with sufficient detail. Any item that triggers the `/intake` subprocess will time out and fail.

---

## Impact

- All `idea`-stage items that do NOT meet auto-complete criteria (no explicit "Acceptance Criteria" / "## Implementation" sections) will fail to process.
- The first such item will crash the entire batch with `AttributeError` or timeout.
- Items that were claimed before the crash are left in orphaned state (`status: in_progress` + `stage: idea` or `in_progress`).
- The orphan recovery logic in `_recover_orphans()` handles resetting these on the next run, but batch throughput is effectively zero for non-auto-completable items.

---

## Fix Recommendations

### Fix 1 (Critical): Type-safe guard in `_extract_pi_text`

At a minimum, ensure the return value is always a `str`:

```python
@staticmethod
def _extract_pi_text(raw: str) -> str:
    ...
    if complete_blocks:
        last = complete_blocks[-1]
        if isinstance(last, str):
            return last
        # Fallback: convert non-string to its repr or empty
        return str(last) if last else ""
    return "".join(delta_parts)
```

Better: add type assertions at every `.append()` call:

```python
for part in (message.get("parts") or []):
    if isinstance(part, dict):
        part_text = part.get("text", "") or part.get("content", "")
        if isinstance(part_text, str) and part_text:
            complete_blocks.append(part_text)
```

And also add a defensive check in `_contains_questions`:

```python
@staticmethod
def _contains_questions(text: str) -> bool:
    if not isinstance(text, str):
        return False
    ...
```

### Fix 2 (Design): Make `_invoke_intake` non-blocking or skip the subprocess

Several options:

**Option A — Skip `/intake` for items needing input**: Instead of invoking `pi -p --mode json /intake <id>` as a subprocess, the intakeall script could directly use the pi agent framework SDK or API to process intake without interactive input. This would avoid the blocking subprocess entirely.

**Option B — Feed defaults via stdin**: Pipe default answers ("no additional input needed") into the `pi` subprocess to allow it to proceed without blocking:

```python
intake_result = self.runner(
    intake_cmd,
    input="\n".join(["No additional questions needed", ""]),
    timeout=self.item_timeout,
)
```

**Option C — Pre-filter to auto-complete only**: Modify `has_sufficient_detail` to be more permissive (e.g., treat "## Recommendation" sections as implementation guidance), so more items qualify for auto-complete. Items still needing `/intake` would be left in `idea` stage and flagged for manual processing. This is the simplest fix but skips intake for items that may need it.

**Option D — Mark items needing intake as `needs_input`**: If an item doesn't meet auto-complete criteria, mark it as `needs_input` (or `blocked`) with a note rather than attempting the subprocess, then move on. This preserves batch progress for remaining items.

### Recommended Fix Combination

1. Apply **Fix 1** (type safety in `_extract_pi_text` and `_contains_questions`) immediately — it's a clear bug.
2. Apply **Fix 2 Option C** (broaden `has_sufficient_detail` to cover "## Recommendation" headings) or **Option D** (skip the /intake subprocess, mark as needs_input) to unblock batch intake.
3. Consider removing the `_invoke_intake` subprocess approach entirely, or document that the `/intake` subprocess route is a known limitation requiring user interaction.

---

## Workaround (used during discovery)

Instead of fixing the script, we processed the 8 `idea`-stage items directly:

1. Read each item's existing description (Issue + Evidence + Recommendation)
2. Appended a formal `## Acceptance Criteria` section derived from the recommendation
3. Updated each item via `wl update <id> --description-file <file> --stage intake_complete --status open --json`
4. Added a comment documenting the intake completion

This approach works for items that have clear, well-structured descriptions but lack formal AC headers. Items with insufficient detail would still need the interactive `/intake` process.

---

## Related Code

- `skill/intakeall/scripts/intakeall.py` — `_extract_pi_text()` ~line 515, `_contains_questions()` ~line 530, `_invoke_intake()` ~line 340
- `skill/intakeall/tests/test_intakeall.py` — existing tests do not cover the `_extract_pi_text` parsing edge case with non-string parts

## Resolution (SA-0MQRAMZ4V0056K14)

Both root causes were fixed in the same work item:

### Fix 1 (Root Cause 1): Type-safe guard in `_extract_pi_text`

Added `isinstance(part_text, str)` guard before appending to `complete_blocks` in the parts iteration loop. This prevents non-string values (e.g., lists from tool-call responses, null values, integers, dicts) from being appended to `complete_blocks`, which previously caused `_extract_pi_text` to return a `list` instead of `str`.

Also added a defensive `isinstance(last, str)` check on the return path for `complete_blocks[-1]`, with a fallback to `str(last)` if needed.

Added `isinstance(text, str)` guard at the start of `_contains_questions()`, returning `False` for non-string input instead of crashing with `AttributeError: 'list' object has no attribute 'lower'`.

### Fix 2 (Root Cause 2): Skip interactive /intake subprocess in batch mode

Instead of invoking the interactive `/intake` subprocess (which blocks indefinitely waiting for stdin), items that fail `has_sufficient_detail` are now marked as `needs_input` in the summary report without making any wl changes. The item stays in `idea` stage and the batch continues to the next item.

Additionally, `has_sufficient_detail` was broadened to recognize `## Recommendation` headings as implementation guidance (added to `SUFFICIENT_INDICATORS`), so more items can be auto-completed without needing manual intake.

### Files changed

- `skill/intakeall/scripts/intakeall.py`:
  - `_extract_pi_text()`: Added `isinstance(part_text, str)` guard in parts loop, added `isinstance(last, str)` guard on return
  - `_contains_questions()`: Added `isinstance(text, str)` guard at start
  - `run_all()`: Replaced `_invoke_intake()` call with direct `needs_input` outcome
  - `SUFFICIENT_INDICATORS`: Added `"## Recommendation"`
- `skill/intakeall/tests/test_intakeall.py`: Added 20 new tests covering:
  - `_extract_pi_text` type safety (list, null, integer, dict, empty, mixed JSON)
  - `_contains_questions` type safety (non-string inputs)
  - Broadened `has_sufficient_detail` for Recommendation sections
  - Needs-input skip behavior in batch mode
- `docs/bug-intakeall-extract-pi-text.md`: Added this resolution section

### Status

All 1561 tests pass, including 20 new tests and 69 existing tests.

## Test Recommendation

Add a test case to `test_intakeall.py` that exercises `_extract_pi_text` with JSON-stream input containing non-string `"text"` payloads (e.g., list values, null values, tool-call response formats). Verify the return value is always `str`.
