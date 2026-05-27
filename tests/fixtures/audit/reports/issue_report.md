Ready to close: Yes

## Summary

All 3 acceptance criteria for work item SA-0MPDXYG3J001YQF3 are met. The audit runner core test suite covers CLI shape, wl invocation, AC extraction, and persistence delegation. All 35 tests pass green.

## Acceptance Criteria Status

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | Tests assert that `audit_runner.py issue <id>` and `audit_runner.py project` subcommands exist and parse the flags `--do-not-persist`, `--pi-bin`, and `--model`. | met | tests/test_audit_runner_core.py:43 — TestCLIParsing class covers all CLI flag combinations with explicit assertions |
| 2 | Tests fake `subprocess.run` for `wl show --children --json` and `wl dep list --json` and assert the exact argv and JSON-decoding behaviour. | met | tests/test_audit_runner_core.py:115 — TestRunWl class verifies argv capture and JSON decode on success/failure |
| 3 | Tests cover AC extraction from both `## Acceptance Criteria` and `### Acceptance Criteria` headings, with numbered and bulleted list variants. | met | tests/test_audit_runner_core.py:158-192 — TestExtractACs covers h2/h3, numbered/bulleted/asterisk variants |

## Children Status

No children.
