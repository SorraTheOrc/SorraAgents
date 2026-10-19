# Background job etiquette

Canonical guidance for launching and waiting on long-running **background**
processes — audits, builds, releases, batch jobs, test sweeps, PR status
checks. This is the single source of truth referenced by the
[implement](../implement/SKILL.md), [audit](../audit/SKILL.md),
[ship](../ship/SKILL.md), [plan](../plan/SKILL.md) and
[test](../test/SKILL.md) skills, and by
[`AGENTS_GLOBAL.md`](../../AGENTS_GLOBAL.md).

Delivered by SA-0MVCX570P003VLBK (helper + tests) and rolled out to all skills
by SA-0MVDP5JTK009HWOT.

## The pattern

**launch → prepare other work → one completion-driven wait with progress →
collect result**

1. **Launch** the job detached with its output captured to a tailable log,
   e.g.

   ```bash
   nohup python3 ./scripts/audit_runner.py issue <id> > "$LOG" 2>&1 &
   echo "PID $! LOG $LOG"   # record both for the wait step
   ```

2. **Prepare other work** while it runs — read context, draft docs, update
   the work item. Do **not** idle-sleep: every wake-up costs a full model
   round-trip and almost always returns "no change".

3. **Wait once, completion-aware**, with the shared helper
   [`wait_for_job.py`](wait_for_job.py):

   ```bash
   python3 skill/shared/wait_for_job.py --pid "$PID" \
     --log "$LOG" --pattern "Audit complete" \
     --timeout 3600 --interval 60 --message "audit SA-XXXX"
   ```

4. **Collect** the result from the log / exit code and continue.

## The helper contract

`wait_for_job.py` waits for one or more completion **markers** and returns
**as soon as any marker completes** — never after a fixed sleep:

| Marker | CLI flag | Completes when |
|---|---|---|
| Process exit | `--pid <pid>` | the PID is gone (zero-exit or zombie) |
| Checkpoint file | `--marker <path>` | the file appears |
| Log pattern | `--log <path> --pattern <regex>` | a new log line matches |

- **Heartbeat:** a timestamped progress line is emitted at least once per
  `--interval` (default 60 s), so an operator can `tail -f` the pane and watch
  progress without polling.
- **Exit codes:** `0` completed, `1` timeout, `2` usage error, `130`
  interrupted (SIGINT/SIGTERM).
- **Timeout is mandatory** — there is no unbounded wait.
- **Herdr sidebar (optional):** when `HERDR_SOCKET_PATH` and `HERDR_PANE_ID`
  are set the helper reports `working`/`blocked` to the sidebar; without them
  it silently degrades with identical behaviour.

## Deprecated: fixed-length `sleep N` polling

Fixed-length `sleep N` polling of a background process —

```bash
# DEPRECATED — do not do this
sleep 900; grep <log>; ps aux | grep <pid>
```

— is **deprecated**. It is blind: if the job finishes at minute 3 the agent
still waits until minute 15 to notice; if it stalls, the agent waits the full
interval before reacting. Every wake-up costs a full model round-trip (tokens,
latency, context growth) and nearly always returns "no change".

**Evidence:** a single `/skill:implement` session on an epic ran for 23.7 h
with 246 bash calls, of which **95 were `sleep` calls totalling 21.0 h —
88.6 % of the session spent sleeping** (73 × `sleep 900`), each polled with the
pattern above. Use the completion-driven wait instead.

Fine-grained polling (`sleep 30`) is **worse** (~2,400 round-trips over 20 h)
and is likewise an anti-pattern.

### Out of scope

Intra-process retry/backoff sleeps **inside** a script (e.g. queue-admission
or lock retry loops between attempts in `audit_runner.py`) are not
background-job waits and are unaffected by this guidance.

## Applicability

Any skill step that launches a long-running process in the background, or
waits on an external job (a CI/PR status check, a merge wait, a batch run),
should follow this pattern. Steps that are already synchronous/foreground (a
blocking subprocess call with a timeout) need no change.

## Script wiring decisions

Per-script decisions from the wiring investigation (SA-0MVDP9B83004UD5E).
Each candidate that waits on a background subprocess/job is either **wired**
(invokes `wait_for_job.py`, grep-verifiable) or **not appropriate**, with a
reason.

| Script | Decision | Reason |
|---|---|---|
| `skill/ship/scripts/run-release.js` | **wired** | `waitForPRMerge()` polled external CI with a fixed `execSync('sleep 10')` loop. It now launches the background poller [`wait_for_pr_checks.py`](../ship/scripts/release/wait_for_pr_checks.py) and waits on it completion-aware with `wait_for_job.py`. |
| `skill/audit/scripts/audit_runner.py` | not appropriate | Its sleeps (`AUDIT_QUEUE_POLL_SECONDS = 0.05`, `_PI_RETRY_BACKOFF_SECONDS`) are intra-process queue-admission and provider-error retry backoffs — explicitly out of scope above. It launches no detached background job of its own; its `pi` subprocess is waited on synchronously with stall detection. |
| `skill/implement/scripts/implement.py` | not appropriate | `git_push_to_dev()` waits on the pre-push hook with a bounded `communicate(timeout=…)` (foreground, synchronous); the `time.sleep(2)` in `cleanup_worktree_processes()` is a SIGTERM→SIGKILL grace period, not a job wait. |
| `skill/shared/git_sandbox.py` | not appropriate | Every `git` invocation is a synchronous `subprocess.run(…, timeout=…)`; there is no background job to wait on. |
| `skill/test/scripts/run_tests.py` | not appropriate | Suite commands run via synchronous `subprocess.run(…, timeout=…)`; the runner is itself the long job other callers wait on, not a launcher that then polls. |

Re-investigate with `rg 'time\\.sleep|execSync\\(.sleep|nohup' skill/` before
adding a new background-and-wait step.
