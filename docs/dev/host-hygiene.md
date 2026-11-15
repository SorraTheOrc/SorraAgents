# Host Hygiene — /tmp Inode Management

Age-based cleanup of high-churn temporary directories on the host, preventing
inode exhaustion in `/tmp` (typically a tmpfs on this workstation).

## Background

`/tmp` is a tmpfs with 1,048,576 inodes.  During active development, rapid
multi-agent test runs churn hundreds of temporary directories — `pytest-of-*`
roots, Playwright/Chromium profiles, tsx and node compile caches — which can
fill the inode table long before byte usage becomes concerning.  Observed in
practice: `/tmp` at 100 % inodes while byte usage was ~24 %.

This page documents the **systemd-tmpfiles** rules that reclaim inodes
automatically via the daily `systemd-tmpfiles-clean.timer` (every 24 hours).
They complement the framework-level disk-backed `TMPDIR` and stale-root
prune implemented in `run_tests.py` / `implement.py` (SA-0MW2QED9M005FWOK,
SA-0MW3EN72O005BMIP, SA-0MW3ENVLU0097JRD).

## Install

```bash
# 1. Copy the template into the system tmpfiles directory:
sudo cp scripts/pi-test-tmp.conf /etc/tmpfiles.d/pi-test-tmp.conf

# 2. Verify the syntax (dry-run — no files removed; run as root):
sudo systemd-tmpfiles --clean --dry-run
```

The config file lives in the repository at
`scripts/pi-test-tmp.conf` and is the canonical version.

## Verify

```bash
# Dry-run — shows what would be cleaned without removing anything.
# Run as root: unprivileged runs cannot age-clean these patterns (systemd
# restricts non-root age-based cleaning of custom subdirectories), so a
# uid-1000 dry-run lists nothing even when old churn exists.
sudo systemd-tmpfiles --clean --dry-run

# Before / after inode usage (replace <user> with your login name):
df -i /tmp
```

Example `systemd-tmpfiles --clean --dry-run` output (abridged — root run):

```
Would remove "/tmp/pytest-of-rgardler/pytest-1458/..."   (older than 2 days)
Would remove "/tmp/pytest-of-rgardler/pytest-1459/..."   (older than 2 days)
# /tmp/pytest-of-rgardler/pytest-current                 (excluded — active symlink)
Would remove "/tmp/playwright-artifacts-xyz/..."        (older than 2 days)
Would remove "/tmp/node-compile-cache-xyz/..."          (older than 2 days)
```

A repeatable functional check that needs no churn fixtures:

```bash
# 1. Make a throwaway old pytest root + symlink:
mkdir -p /tmp/pytest-of-demo/pytest-OLD /tmp/pytest-of-demo/pytest-FRESH
touch /tmp/pytest-of-demo/pytest-OLD/f
touch -d '4 days ago' /tmp/pytest-of-demo/pytest-OLD /tmp/pytest-of-demo/pytest-OLD/f
ln -s pytest-FRESH /tmp/pytest-of-demo/pytest-current

# 2. Dry-run must list pytest-OLD (and not pytest-current/pytest-FRESH):
sudo systemd-tmpfiles --clean --dry-run | grep pytest-of-demo

# 3. Clean up the demo:
rm -rf /tmp/pytest-of-demo
```

Age-based cleanup runs automatically via `systemd-tmpfiles-clean.timer`
every 24 hours — no cron entry needed on systemd hosts.  On hosts without
systemd-tmpfiles, install the equivalent cron job:

```cron
# /etc/cron.d/pi-test-tmp
# Remove pytest roots and churn caches older than 2 days.
17 4 * * * root find /tmp/pytest-of-* -mindepth 1 -maxdepth 1 -type d \
  ! -name 'pytest-current' -mtime +2 -exec rm -rf {} + \; && \
  find /tmp -maxdepth 1 \
  \( -name 'playwright-*' -o -name '.org.chromium.Chromium.*' \
  -o -name '.com.google.Chrome.*' -o -name 'tsx-*' \
  -o -name 'node-compile-cache*' -o -name 'v8-compile-cache*' \) \
  -mtime +2 -exec rm -rf {} +
```

## How it works

The rules use the `systemd-tmpfiles` format (`man tmpfiles.d`):

| Directive | Meaning |
|-----------|---------|
| `e` | Adjust existing directory and age-clean its **contents** — direct children older than the age are removed recursively; the directory itself is kept |
| `x` | Exclude a path/glob from age-based cleanup — never removed, even if it matches another rule |

### Age threshold

**2 days** — well above the measured worst-case full-suite run (~48 minutes),
providing comfortable margin so long-running test invocations are never
interrupted.

### Rules

```
# pytest test roots — clean all old roots under every user's base dir
# (covers all users; %u would wrongly expand to "root" in system scope)
e /tmp/pytest-of-* - - - 2d

# Active pytest root — never remove the newest symlink
x /tmp/pytest-of-*/pytest-current

# Playwright browser profiles (contents at 2d; shells swept by the stock
# 10-day /tmp rule)
e /tmp/playwright-* - - - 2d

# Chromium / Chrome per-user profile directories
e /tmp/.org.chromium.Chromium.* - - - 2d
e /tmp/.com.google.Chrome.* - - - 2d

# Build / compile caches
e /tmp/tsx-* - - - 2d
e /tmp/node-compile-cache* - - - 2d
e /tmp/v8-compile-cache* - - - 2d
```

- `e` age-cleans the **contents** of each matched directory: every direct
  child older than the age is removed recursively, and the matched base
  directory itself is never removed.
- The `x` exclusion for `pytest-current` follows the `e` glob, so the active
  (newest) root symlink is never removed.
- `%u` would expand to `root` when the cleanup runs as root in system scope,
  so the `pytest-of-*` glob is used instead: it covers every user's per-UID
  base directory.  On a per-user `systemd-tmpfiles --user` instance, `%u` is
  the real login name and a `pytest-of-%u` pattern is equally correct.
- The `playwright-*`, `tsx-*`, `node-compile-cache*` and browser-profile
  dirs live directly under `/tmp`, so `e` cleans their (potentially large)
  contents at 2 days while the empty shells are swept later by the stock
  `q /tmp 1777 root root 10d` rule — fresh profiles/caches are untouched.

### Automatic execution

The `systemd-tmpfiles-clean.timer` runs the cleanup every 24 hours
(15 minutes after boot, then periodically).  It executes as **root**, which
is required for age-based cleaning of these patterns — an unprivileged
`systemd-tmpfiles --clean` cannot clean custom subdirectories (confirm with
`sudo systemd-tmpfiles --clean --dry-run`; a uid-1000 dry-run lists nothing
for these rules even with stale churn present).  No cron entry is needed on
systems with systemd.  On hosts without systemd-tmpfiles, use the cron
equivalent in the Verify section.

## Safety guarantees

1. **Active pytest root is never removed.**  The `x` (exclude) directive on
   `pytest-current` ensures the newest test root symlink always survives, and
   the newest root is by definition freshly touched so it never crosses the
   age threshold while current.
2. **Age threshold exceeds max run time.**  2 days >> 48 minutes.  No running
   test invocation is at risk.
3. **User-scoped.**  The `pytest-of-*` glob covers every user's per-UID base
   directory (in system scope `%u` would expand to `root`; a per-user
   `systemd-tmpfiles --user` instance may use `pytest-of-%u` instead).
4. **No repo code change.**  The repository contains only the config template
   (`scripts/pi-test-tmp.conf`) and this runbook — no auto-installer, no
   code changes.

## Rollback

```bash
# Remove the rule file:
sudo rm /etc/tmpfiles.d/pi-test-tmp.conf

# Trigger an immediate clean to verify removal has no effect:
sudo systemd-tmpfiles --clean

# Reboot the system or restart the timer (optional):
sudo systemctl daemon-reload
sudo systemctl restart systemd-tmpfiles-clean.timer
```

## Failure signatures

If inode exhaustion occurs despite these rules, check for:

| Symptom | Likely cause |
|---------|-------------|
| `ENOSPC` in Playwright `createContext` | `/tmp` inode table full |
| `df -i /tmp` shows 100 % usage | Cleanup timer hasn't run or rules not installed |
| Test gate `reason: unparsed` / `infrastructure` | Downstream symptom of ENOSPC |

### Quick remediation

```bash
# Manual clean (emergency):
sudo systemd-tmpfiles --clean

# Or remove old pytest roots manually:
find /tmp/pytest-of-$(whoami)/ -maxdepth 1 -type d -mtime +2 ! -name 'pytest-current' -exec rm -rf {} +

# Verify:
df -i /tmp
```

## See also

- `man tmpfiles.d(5)` — tmpfiles configuration format
- `man systemd-tmpfiles(8)` — tmpfiles utility
- `man systemd-tmpfiles-clean.timer(8)` — cleanup timer
- [Worktree isolation docs](worktree-isolation.md) — test runner tmp handling
- SA-0MW2QED9M005FWOK — framework-level disk-backed TMPDIR + stale-root prune
- [Test skill reference](test-skill-reference.md) — `run_tests.py` integration
