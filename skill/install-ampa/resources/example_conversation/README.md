Example conversation engine

Run `python3 runner.py` to start two Conversation Managers (CM-A, CM-B) that
exchange messages for 10 rounds (20 messages total). A transcript is written to
`transcript.jsonl` in the current directory.

Usage:

- `python3 runner.py` — run with default seeded topics and write transcript
- `python3 runner.py --rounds 5 --out file.jsonl` — override rounds and output

Verification:

- After a run, `transcript.jsonl` should contain 20 newline-delimited JSON
  entries with alternating `sender` values `CM-A` and `CM-B` and distinct
  `session_id` values.
