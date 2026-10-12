#!/usr/bin/env python3
"""Test Churn Analysis — quantify how often test code is rewritten.

The tool inspects a git history (a single mainline branch, non-merge commits)
and reports how much of the test code added over time is later *rewritten*
rather than extended.  It is the evidence base for assessing the return on
investment of test-driven development (TDD): a low rewrite ratio means tests
survive feature evolution; a high ratio means tests are rewritten almost as
often as they are written.

Unlike the ad-hoc script it replaces (ContextHub
``scripts/test_churn_analysis.py`` at commit ``141ceb8b``), this version:

* takes the project root as an argument — nothing is hardcoded;
* reads the real git name-status for every commit (A/M/D/R/T) instead of
  assuming every change is a modification;
* documents and reports *two* rewrite-ratio denominators (modified test
  files only, and including newly added test files);
* counts **distinct commits** for the temporal trend (a rate can never exceed
  100%);
* accounts for whole-file test deletions; and
* counts each churned file once when aggregating by domain.

Scope and limitations are documented in the generated report.  The tool never
mutates the analysed repository: it reads git history and writes its two
outputs (``test_churn_report.md`` and ``test_churn_data.json``) to the chosen
output directory.

Usage::

    python3 test_churn_analysis.py --project-root /path/to/repo
    python3 test_churn_analysis.py /path/to/repo --branch main \\
        --output-dir /tmp/churn --format both
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANALYSIS_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Filename patterns that identify a test file.  Deliberately language-neutral
#: so the tool works on any project without code changes.
DEFAULT_TEST_FILENAME_PATTERNS: tuple[str, ...] = (
    r"(^|/)test_[^/]+\.py$",
    r"(^|/)[^/]+_test\.py$",
    r"(^|/)test_[^/]+\.(?:sh|bash)$",
    r"\.test\.(?:ts|tsx|js|jsx|mjs|cjs)$",
    r"\.spec\.(?:ts|tsx|js|jsx|mjs|cjs)$",
)

#: Path segments that identify a test directory.
DEFAULT_TEST_DIR_SEGMENTS: tuple[str, ...] = (
    "test",
    "tests",
    "__tests__",
    "spec",
    "specs",
)

#: File extensions treated as source code when located outside test paths.
DEFAULT_CODE_EXTENSIONS: tuple[str, ...] = (
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".cs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".swift",
    ".php",
    ".scala",
    ".sh",
    ".bash",
    ".vue",
    ".svelte",
)

#: Path segments excluded from source classification (generated/vendor/docs).
DEFAULT_EXCLUDED_SEGMENTS: tuple[str, ...] = (
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "docs",
    "doc",
    "documentation",
    "site-packages",
    ".git",
    ".venv",
    "venv",
)


@dataclass
class ChurnConfig:
    """Tunable classification rules for the analysis.

    Every field has a portable default so the tool runs against any project
    without configuration.  A JSON config file (``--config``) may override any
    subset of the fields.
    """

    test_filename_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_FILENAME_PATTERNS)
    )
    test_dir_segments: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_DIR_SEGMENTS)
    )
    code_extensions: list[str] = field(
        default_factory=lambda: list(DEFAULT_CODE_EXTENSIONS)
    )
    excluded_segments: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_SEGMENTS)
    )
    #: Optional ordered area rules: ``[{"pattern": "<regex>", "area": "name"}]``.
    #: The first matching rule wins; otherwise a generic classifier is used.
    area_rules: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChurnConfig:
        """Build a config from a (possibly partial) mapping."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                "Unknown config key(s): " + ", ".join(sorted(unknown))
            )
        return cls(**data)

    @classmethod
    def from_file(cls, path: str | Path) -> ChurnConfig:
        """Load a config JSON file, merging over the defaults."""
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise TypeError("Config file must contain a JSON object")
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A single file change within a commit."""

    path: str
    status: str  # A, M, D, R, T
    additions: int = 0
    deletions: int = 0
    old_path: str | None = None

    @property
    def is_rename(self) -> bool:
        return self.status == "R"


@dataclass
class Commit:
    """A parsed git commit."""

    sha: str
    short_sha: str
    message: str
    date: str
    author: str
    files: list[FileChange] = field(default_factory=list)


class GitError(RuntimeError):
    """Raised when a git command fails."""


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _path_segments(path: str) -> list[str]:
    return [seg for seg in path.replace("\\", "/").split("/") if seg]


def is_test_file(path: str, config: ChurnConfig) -> bool:
    """Return True when *path* is a test file under *config*."""
    normalised = path.replace("\\", "/")
    for pattern in config.test_filename_patterns:
        if re.search(pattern, normalised):
            return True
    segments = _path_segments(normalised)
    return any(seg in config.test_dir_segments for seg in segments[:-1])


def _is_excluded(path: str, config: ChurnConfig) -> bool:
    return any(seg in config.excluded_segments for seg in _path_segments(path))


def is_src_file(path: str, config: ChurnConfig) -> bool:
    """Return True when *path* is a non-test source file under *config*."""
    if is_test_file(path, config):
        return False
    if _is_excluded(path, config):
        return False
    suffix = Path(path).suffix.lower()
    return suffix in {ext.lower() for ext in config.code_extensions}


def classify_area(path: str, config: ChurnConfig) -> str:
    """Classify a file into a reporting area.

    Explicit ``area_rules`` (first regex match wins) take precedence.  The
    generic fallback returns the segment following the last test directory for
    test files, otherwise the first path segment (or ``root``).
    """
    for rule in config.area_rules:
        pattern = rule.get("pattern")
        area = rule.get("area")
        if pattern and area and re.search(pattern, path):
            return area

    segments = _path_segments(path)
    for index, segment in enumerate(segments):
        if segment in config.test_dir_segments:
            # ``segments[-1]`` is the filename; only intermediate directories
            # can name a sub-area, otherwise the test directory itself is the
            # area (never the filename).
            sub_dirs = segments[index + 1 : -1]
            return f"{segment}/{sub_dirs[0]}" if sub_dirs else segment
    return segments[0] if segments else "root"


# ---------------------------------------------------------------------------
# Git data extraction
# ---------------------------------------------------------------------------

def run_git(repo_root: str | Path, *args: str) -> str:
    """Run a git command in *repo_root* and return stdout.

    Raises:
        GitError: when git exits non-zero.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def resolve_branch(repo_root: str | Path, branch: str | None) -> str:
    """Resolve the branch to analyse.

    An explicit *branch* is returned unchanged.  Otherwise ``dev``, ``main``
    and ``master`` are tried in that order, falling back to the current HEAD.
    """
    if branch:
        return branch
    for candidate in ("dev", "main", "master"):
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{candidate}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def get_mainline_commits(
    repo_root: str | Path,
    branch: str,
    since: str | None = None,
    max_commits: int = 0,
) -> list[dict[str, str]]:
    """Return non-merge commits on *branch*, oldest first."""
    args = [
        "log",
        branch,
        "--no-merges",
        "--pretty=format:%H%x1f%h%x1f%s%x1f%ad%x1f%an",
        "--date=short",
        "--reverse",
    ]
    if since:
        args.insert(2, f"--since={since}")
    output = run_git(repo_root, *args)
    commits: list[dict[str, str]] = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        commits.append(
            {
                "sha": parts[0],
                "short_sha": parts[1],
                "message": parts[2],
                "date": parts[3],
                "author": parts[4],
            }
        )
    if max_commits and len(commits) > max_commits:
        commits = commits[-max_commits:]
    return commits


def _parse_name_status(raw: str) -> list[dict[str, Any]]:
    """Parse ``git diff-tree --name-status -z`` output."""
    tokens = raw.split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if not status:
            index += 1
            continue
        if status[0] in ("R", "C"):
            old = tokens[index + 1] if index + 1 < len(tokens) else ""
            new = tokens[index + 2] if index + 2 < len(tokens) else ""
            entries.append({"status": status[0], "path": new, "old_path": old})
            index += 3
        else:
            path = tokens[index + 1] if index + 1 < len(tokens) else ""
            entries.append({"status": status[0], "path": path, "old_path": None})
            index += 2
    return entries


def _parse_numstat(raw: str) -> list[dict[str, Any]]:
    """Parse ``git diff-tree --numstat -z`` output."""
    tokens = raw.split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "":
            index += 1
            continue
        parts = token.split("\t")
        if len(parts) < 2:
            index += 1
            continue
        additions = _as_int(parts[0])
        deletions = _as_int(parts[1])
        path_field = "\t".join(parts[2:]) if len(parts) > 2 else ""
        if path_field:
            entries.append(
                {
                    "additions": additions,
                    "deletions": deletions,
                    "path": path_field,
                    "old_path": None,
                }
            )
            index += 1
        else:
            old = tokens[index + 1] if index + 1 < len(tokens) else ""
            new = tokens[index + 2] if index + 2 < len(tokens) else ""
            entries.append(
                {
                    "additions": additions,
                    "deletions": deletions,
                    "path": new,
                    "old_path": old,
                }
            )
            index += 3
    return entries


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        # Binary files report ``-`` for additions/deletions.
        return 0


def get_commit_diffs(repo_root: str | Path, sha: str) -> list[FileChange]:
    """Return the file changes for *sha* with real statuses and line counts.

    Combines ``--name-status`` (A/M/D/R/T) with ``--numstat`` (line counts),
    both with rename detection (``-M``) and ``--root`` so the initial commit
    is handled.  ``--diff-filter=ADMRT`` keeps additions, deletions,
    modifications, renames and type changes (copy events are excluded to keep
    the ratio arithmetic honest).
    """
    name_status = run_git(
        repo_root,
        "diff-tree", "-r", "-M", "--root", "--no-commit-id",
        "--diff-filter=ADMRT", "--name-status", "-z", sha,
    )
    numstat = run_git(
        repo_root,
        "diff-tree", "-r", "-M", "--root", "--no-commit-id",
        "--diff-filter=ADMRT", "--numstat", "-z", sha,
    )
    status_entries = _parse_name_status(name_status)
    numstat_entries = _parse_numstat(numstat)
    changes: list[FileChange] = []
    for index, status_entry in enumerate(status_entries):
        line_entry = (
            numstat_entries[index] if index < len(numstat_entries) else {}
        )
        changes.append(
            FileChange(
                path=status_entry["path"],
                status=status_entry["status"],
                additions=int(line_entry.get("additions", 0)),
                deletions=int(line_entry.get("deletions", 0)),
                old_path=status_entry.get("old_path")
                or line_entry.get("old_path"),
            )
        )
    return changes


def parse_commits(
    repo_root: str | Path,
    commits_data: Sequence[dict[str, str]],
    progress: bool = False,
) -> list[Commit]:
    """Parse every commit into a :class:`Commit` object."""
    commits: list[Commit] = []
    total = len(commits_data)
    for index, data in enumerate(commits_data):
        if progress and index % 200 == 0:
            sys.stderr.write(f"\rParsing commit {index}/{total}...")
            sys.stderr.flush()
        try:
            files = get_commit_diffs(repo_root, data["sha"])
        except GitError as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"\nError parsing {data['sha']}: {exc}\n")
            continue
        commits.append(
            Commit(
                sha=data["sha"],
                short_sha=data["short_sha"],
                message=data["message"],
                date=data["date"],
                author=data["author"],
                files=files,
            )
        )
    if progress and total:
        sys.stderr.write("\n")
    return commits


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------

def _is_rewrite(change: FileChange) -> bool:
    """A *rewrite* is a modified/renamed test file with churn both ways."""
    return (
        change.status in ("M", "R")
        and change.additions > 0
        and change.deletions > 0
    )


def _is_modification(change: FileChange) -> bool:
    """Any modified/renamed test file (including pure additions/deletions)."""
    return change.status in ("M", "R")


def analyse_feature_commits(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, Any]:
    """Primary analysis of feature commits (those touching source files)."""
    feature_commits = [
        commit
        for commit in commits
        if any(is_src_file(f.path, config) for f in commit.files)
    ]
    total_feature = len(feature_commits)

    commits_with_rewrites = 0
    commits_with_new_tests = 0
    commits_touching_test = 0

    modified_deletions = 0
    modified_additions = 0
    new_file_additions = 0
    deleted_test_files = 0
    deleted_test_lines = 0

    area_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "feature_commits": 0,
            "rewrites": 0,
            "new_files": 0,
            "deleted_files": 0,
            "total_deleted": 0,
            "total_added": 0,
        }
    )

    for commit in feature_commits:
        test_files = [f for f in commit.files if is_test_file(f.path, config)]
        if not test_files:
            continue
        commits_touching_test += 1

        has_rewrite = False
        has_new = False
        for change in test_files:
            area = classify_area(change.path, config)
            area_stats[area]["feature_commits"] += 1
            if _is_rewrite(change):
                has_rewrite = True
                area_stats[area]["rewrites"] += 1
            if change.status == "A":
                has_new = True
                area_stats[area]["new_files"] += 1
                new_file_additions += change.additions
            if _is_modification(change):
                modified_deletions += change.deletions
                modified_additions += change.additions
                area_stats[area]["total_deleted"] += change.deletions
                area_stats[area]["total_added"] += change.additions
        if has_rewrite:
            commits_with_rewrites += 1
        if has_new:
            commits_with_new_tests += 1

    # Whole-file deletion accounting spans the entire history (not just feature
    # commits): a test file can be deleted in a test-only commit, and such
    # deletions must not be invisible (child AC: whole-file deletion
    # accounting).  Each deleted file is counted exactly once.
    for commit in commits:
        for change in commit.files:
            if not is_test_file(change.path, config):
                continue
            if change.status == "D":
                deleted_test_files += 1
                deleted_test_lines += change.deletions
                area_stats[classify_area(change.path, config)]["deleted_files"] += 1

    modified_only_denominator = modified_deletions + modified_additions
    including_new_denominator = modified_only_denominator + new_file_additions

    def _pct(numerator: int, denominator: int) -> float:
        return round(numerator / denominator * 100, 1) if denominator else 0.0

    def _ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator * 100, 1) if denominator else 0.0

    return {
        "total_commits": len(commits),
        "total_feature": total_feature,
        "feature_pct": _pct(total_feature, len(commits)),
        "commits_touching_test": commits_touching_test,
        "test_touch_pct": _pct(commits_touching_test, total_feature),
        "commits_with_rewrites": commits_with_rewrites,
        "rewrite_pct": _pct(commits_with_rewrites, total_feature),
        "commits_with_new_tests": commits_with_new_tests,
        "new_test_pct": _pct(commits_with_new_tests, total_feature),
        # Rewrite ratio, modified test files only (denominator = modified
        # deletions + modified additions, including pure-addition and
        # pure-deletion modifications).
        "rewrite_ratio_modified_only_pct": _ratio(
            modified_deletions, modified_only_denominator
        ),
        "modified_only_denominator": modified_only_denominator,
        # Rewrite ratio including newly added test files in the denominator.
        "rewrite_ratio_including_new_pct": _ratio(
            modified_deletions, including_new_denominator
        ),
        "including_new_denominator": including_new_denominator,
        "modified_test_deletions": modified_deletions,
        "modified_test_additions": modified_additions,
        "new_test_file_additions": new_file_additions,
        "deleted_test_files": deleted_test_files,
        "deleted_test_lines": deleted_test_lines,
        "area_stats": {area: dict(stats) for area, stats in area_stats.items()},
    }


def analyse_temporal_trends(
    commits: Sequence[Commit], config: ChurnConfig
) -> list[dict[str, Any]]:
    """Rewrite activity per calendar month, counting **distinct commits**."""
    monthly: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "features": 0,
            "feature_commits_with_rewrites": 0,
            "lines_deleted": 0,
            "lines_added": 0,
        }
    )
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        period = commit.date[:7]
        monthly[period]["features"] += 1
        rewrites = [
            f
            for f in commit.files
            if is_test_file(f.path, config) and _is_rewrite(f)
        ]
        if rewrites:
            monthly[period]["feature_commits_with_rewrites"] += 1
            monthly[period]["lines_deleted"] += sum(r.deletions for r in rewrites)
            monthly[period]["lines_added"] += sum(r.additions for r in rewrites)

    result: list[dict[str, Any]] = []
    for period in sorted(monthly):
        data = monthly[period]
        features = data["features"]
        distinct = data["feature_commits_with_rewrites"]
        result.append(
            {
                "period": period,
                "feature_commits": features,
                "feature_commits_with_rewrites": distinct,
                "rewrite_rate_pct": round(distinct / features * 100, 1)
                if features
                else 0.0,
                "lines_deleted": data["lines_deleted"],
                "lines_added": data["lines_added"],
            }
        )
    return result


def analyse_per_file_stability(
    commits: Sequence[Commit], config: ChurnConfig, limit: int = 20
) -> dict[str, Any]:
    """Identify the most frequently modified test files."""
    file_churn: dict[str, dict[str, int]] = defaultdict(
        lambda: {"commits": 0, "total_additions": 0, "total_deletions": 0}
    )
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        for change in commit.files:
            if not is_test_file(change.path, config):
                continue
            file_churn[change.path]["commits"] += 1
            file_churn[change.path]["total_additions"] += change.additions
            file_churn[change.path]["total_deletions"] += change.deletions

    ordered = sorted(
        file_churn.items(), key=lambda item: item[1]["commits"], reverse=True
    )
    top = [
        {
            "file": path,
            **stats,
            "area": classify_area(path, config),
        }
        for path, stats in ordered[:limit]
    ]
    return {"total_test_files_modified": len(file_churn), "top_files": top}


def analyse_domain_churn(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, dict[str, int]]:
    """Churn aggregated by area, counting each file exactly once."""
    domain_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "test_files": 0,
            "src_files": 0,
            "rewrites": 0,
            "new_tests": 0,
            "deleted_tests": 0,
            "total_deletions": 0,
            "total_additions": 0,
        }
    )
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        for change in commit.files:
            if is_src_file(change.path, config):
                domain_stats["src"]["src_files"] += 1
            elif is_test_file(change.path, config):
                area = classify_area(change.path, config)
                domain_stats[area]["test_files"] += 1
                if _is_rewrite(change):
                    domain_stats[area]["rewrites"] += 1
                    domain_stats[area]["total_deletions"] += change.deletions
                    domain_stats[area]["total_additions"] += change.additions
                elif change.status == "A":
                    domain_stats[area]["new_tests"] += 1
                elif change.status == "D":
                    domain_stats[area]["deleted_tests"] += 1
    return {area: dict(stats) for area, stats in domain_stats.items()}


def analyse_test_change_pattern(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, Any]:
    """Describe how tests change relative to source changes.

    Git cannot prove that a test was written *before* its source, so this
    metric reports *co-evolution* (test files added in a feature commit) and
    *test-only maintenance* (commits that change tests without touching
    source).  It must not be interpreted as a test-first percentage.
    """
    feature_commits = 0
    tests_added_with_feature = 0
    tests_modified_with_feature = 0
    test_only_commits = 0

    for commit in commits:
        test_files = [f for f in commit.files if is_test_file(f.path, config)]
        src_files = [f for f in commit.files if is_src_file(f.path, config)]
        if src_files:
            feature_commits += 1
            if any(f.status == "A" for f in test_files):
                tests_added_with_feature += 1
            if any(f.status in ("M", "R", "D") for f in test_files):
                tests_modified_with_feature += 1
        elif test_files:
            test_only_commits += 1

    return {
        "feature_commits": feature_commits,
        "tests_added_with_feature": tests_added_with_feature,
        "co_evolution_pct": round(
            tests_added_with_feature / feature_commits * 100, 1
        )
        if feature_commits
        else 0.0,
        "tests_modified_with_feature": tests_modified_with_feature,
        "test_only_commits": test_only_commits,
    }


def analyse_rewrite_intensity(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, Any]:
    """Distribution of rewritten lines per commit."""
    sizes: list[int] = []
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        rewrites = [
            f
            for f in commit.files
            if is_test_file(f.path, config) and _is_rewrite(f)
        ]
        if rewrites:
            sizes.append(sum(f.additions + f.deletions for f in rewrites))

    if not sizes:
        return {"total_rewrite_commits": 0, "distribution": {}}

    ordered = sorted(sizes)
    count = len(ordered)
    total = sum(ordered)
    mean = total / count

    def _percentile(fraction: float) -> int:
        index = min(count - 1, int(count * fraction))
        return ordered[index]

    return {
        "total_rewrite_commits": count,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": round(mean),
        "median": ordered[count // 2],
        "p95": _percentile(0.95),
        "p99": _percentile(0.99),
        "stddev": round(
            (sum((x - mean) ** 2 for x in ordered) / count) ** 0.5
        ),
        "distribution": {
            "0-10_lines": len([x for x in ordered if x <= 10]),
            "11-50_lines": len([x for x in ordered if 11 <= x <= 50]),
            "51-100_lines": len([x for x in ordered if 51 <= x <= 100]),
            "101-250_lines": len([x for x in ordered if 101 <= x <= 250]),
            "251-500_lines": len([x for x in ordered if 251 <= x <= 500]),
            "500+_lines": len([x for x in ordered if x > 500]),
        },
    }


def analyse_source_volatility_correlation(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, Any]:
    """Pearson correlation between source churn and test rewrite churn."""
    data_points: list[tuple[int, int]] = []
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        src_lines = sum(
            f.additions + f.deletions
            for f in commit.files
            if is_src_file(f.path, config)
        )
        test_rewrite_lines = sum(
            f.additions + f.deletions
            for f in commit.files
            if is_test_file(f.path, config) and _is_rewrite(f)
        )
        if src_lines > 0:
            data_points.append((src_lines, test_rewrite_lines))

    if len(data_points) < 2:
        return {"correlation": None, "data_points": len(data_points)}

    count = len(data_points)
    x_mean = sum(point[0] for point in data_points) / count
    y_mean = sum(point[1] for point in data_points) / count
    numerator = sum(
        (point[0] - x_mean) * (point[1] - y_mean) for point in data_points
    )
    denom_x = sum((point[0] - x_mean) ** 2 for point in data_points) ** 0.5
    denom_y = sum((point[1] - y_mean) ** 2 for point in data_points) ** 0.5
    correlation = (
        round(numerator / (denom_x * denom_y), 3)
        if denom_x * denom_y
        else None
    )
    return {
        "correlation": correlation,
        "data_points": count,
        "avg_src_lines_per_feature": round(x_mean),
        "avg_test_rewrite_lines_per_feature": round(y_mean),
    }


def analyse_efficiency_metrics(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, Any]:
    """Lines of test code churned per line of source changed."""
    total_src_changed = 0
    total_test_churned = 0
    src_file_count = 0
    test_file_count = 0

    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        for change in commit.files:
            if is_src_file(change.path, config):
                total_src_changed += change.additions + change.deletions
                src_file_count += 1
            elif is_test_file(change.path, config):
                test_file_count += 1
                if _is_modification(change):
                    total_test_churned += change.additions + change.deletions

    return {
        "total_src_files_touched": src_file_count,
        "total_test_files_touched": test_file_count,
        "total_src_lines_changed": total_src_changed,
        "total_test_lines_churned": total_test_churned,
        "test_to_source_ratio": round(test_file_count / src_file_count, 2)
        if src_file_count
        else 0,
        "test_churn_per_source_line": round(
            total_test_churned / total_src_changed, 2
        )
        if total_src_changed
        else 0,
    }


def analyse_commit_patterns(
    commits: Sequence[Commit], config: ChurnConfig
) -> dict[str, int]:
    """Count feature commits by the shape of their test changes."""
    patterns: dict[str, int] = defaultdict(int)
    for commit in commits:
        if not any(is_src_file(f.path, config) for f in commit.files):
            continue
        test_files = [f for f in commit.files if is_test_file(f.path, config)]
        rewrites = [f for f in test_files if _is_rewrite(f)]
        additions = [f for f in test_files if f.status == "A"]
        deletions = [f for f in test_files if f.status == "D"]

        if rewrites and additions:
            patterns["both_rewrite_and_new"] += 1
        elif rewrites:
            patterns["rewrite_only"] += 1
        elif additions:
            patterns["new_tests_only"] += 1
        elif deletions:
            patterns["deleted_tests_only"] += 1
        else:
            patterns["modify_only"] += 1
    return dict(patterns)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _assess_tdd(analysis: dict[str, Any]) -> dict[str, Any]:
    """Return a structured TDD-value assessment from the corrected metrics."""
    rewrite_ratio = analysis["rewrite_ratio_including_new_pct"]
    modified_only = analysis["rewrite_ratio_modified_only_pct"]

    if modified_only > 50:
        verdict = "HIGH rewrite rate — tests are frequently rewritten."
        score = 0
    elif modified_only > 30:
        verdict = (
            "MODERATE rewrite rate — a meaningful share of test code is "
            "rewritten."
        )
        score = 1
    else:
        verdict = "LOW rewrite rate — test code is relatively stable."
        score = 2

    if analysis["deleted_test_files"]:
        deletion_note = (
            f"{analysis['deleted_test_files']} test file(s) were deleted "
            f"wholesale ({analysis['deleted_test_lines']} lines)."
        )
    else:
        deletion_note = "No whole-file test deletions were observed."

    return {
        "rewrite_ratio_including_new_pct": rewrite_ratio,
        "rewrite_ratio_modified_only_pct": modified_only,
        "verdict": verdict,
        "score": score,
        "deletion_note": deletion_note,
    }


def generate_report(report: dict[str, Any]) -> str:
    """Render the self-contained Markdown report."""
    meta = report["metadata"]
    analysis = report["primary_metrics"]
    temporal = report["temporal_trends"]
    stability = report["file_stability"]
    domain = report["domain_churn"]
    pattern = report["test_change_pattern"]
    intensity = report["rewrite_intensity"]
    correlation = report["source_correlation"]
    efficiency = report["efficiency_metrics"]
    commit_patterns = report["commit_patterns"]
    assessment = report["tdd_value_assessment"]

    lines: list[str] = []
    add = lines.append

    add("# Test Churn Report")
    add("")
    add(f"- **Project root:** `{meta['project_root']}`")
    add(f"- **Branch analysed:** `{meta['branch']}`")
    add(f"- **Commit range:** `{meta['first_commit'] or 'n/a'}` … "
        f"`{meta['last_commit'] or 'n/a'}`")
    add(f"- **Commits analysed:** {analysis['total_commits']:,} (non-merge)")
    add(f"- **Generated (UTC):** {meta['generated_at']}")
    add(f"- **Analyser version:** {meta['analyser_version']}")
    add("")

    add("## 1. Scope and definitions")
    add("")
    add("**Scope.** Mainline branch only (`" + meta["branch"] + "`), non-merge")
    add("commits, read from the git history of the project root above. No")
    add("working-tree state, branches other than the mainline, or uncommitted")
    add("changes are considered.")
    add("")
    add("**Definitions.**")
    add("")
    add("- **Test file** — a file whose name matches a test-filename pattern or")
    add("  that lives in a test directory. Patterns are configurable; the")
    add("  defaults are language-neutral (see `ChurnConfig`).")
    add("- **Source file** — a non-test file with a recognised code extension,")
    add("  excluding documentation, vendor and build directories.")
    add("- **Feature commit** — a commit that changes at least one source file.")
    add("- **Rewrite** — a modified or renamed test file with both additions and")
    add("  deletions, i.e. existing test lines were replaced.")
    add("- **New test file** — a test file added (git status `A`).")
    add("- **Deleted test file** — a test file removed wholesale (git status `D`).")
    add("")
    add("**Limitations.**")
    add("")
    add("- Git cannot prove that a test was written before its source; the")
    add("  co-evolution metric is not a test-first percentage.")
    add("- A rewrite can reflect a legitimate interface change rather than poor")
    add("  test design; the report is evidence, not a verdict.")
    add("- Rename detection is heuristic and bounded by git's similarity")
    add("  threshold.")
    if meta.get("max_commits"):
        add(f"- Only the most recent {meta['max_commits']:,} commits were analysed.")
    if meta.get("since"):
        add(f"- Commits before `{meta['since']}` were excluded.")
    add("")

    add("## 2. Primary metric — test code rewrite ratio")
    add("")
    add(f"- Feature commits: **{analysis['total_feature']:,}** "
        f"({analysis['feature_pct']}% of all commits)")
    add(f"- Feature commits touching tests: "
        f"{analysis['commits_touching_test']:,} "
        f"({analysis['test_touch_pct']}%)")
    add(f"- Feature commits with rewrites: "
        f"{analysis['commits_with_rewrites']:,} "
        f"({analysis['rewrite_pct']}%)")
    add(f"- Feature commits adding new test files: "
        f"{analysis['commits_with_new_tests']:,} "
        f"({analysis['new_test_pct']}%)")
    add("")
    add("Two denominators are reported, as required, so the headline number is")
    add("not sensitive to how pure-addition modifications are treated:")
    add("")
    add(f"1. **Modified test files only:** "
        f"{analysis['rewrite_ratio_modified_only_pct']}% "
        f"(`deletions / (deletions + additions)` over modified test files; "
        f"denominator {analysis['modified_only_denominator']:,}).")
    add(f"2. **Including new test files:** "
        f"{analysis['rewrite_ratio_including_new_pct']}% "
        f"(adds new test-file additions to the denominator; "
        f"denominator {analysis['including_new_denominator']:,}).")
    add("")
    add(f"- Lines deleted from modified tests: "
        f"{analysis['modified_test_deletions']:,}")
    add(f"- Lines added to modified tests: "
        f"{analysis['modified_test_additions']:,}")
    add(f"- Lines added in new test files: "
        f"{analysis['new_test_file_additions']:,}")
    add(f"- Whole test files deleted: {analysis['deleted_test_files']:,} "
        f"({analysis['deleted_test_lines']:,} lines)")
    add("")

    add("## 3. Temporal trend (distinct commits per month)")
    add("")
    add("| Period | Feature commits | With rewrites | Rate | "
        "Lines deleted | Lines added |")
    add("|---|---|---|---|---|---|")
    for row in temporal:
        add(f"| {row['period']} | {row['feature_commits']:,} | "
            f"{row['feature_commits_with_rewrites']:,} | "
            f"{row['rewrite_rate_pct']}% | {row['lines_deleted']:,} | "
            f"{row['lines_added']:,} |")
    add("")
    add("The rate counts *distinct* feature commits with at least one rewrite,")
    add("so it cannot exceed 100%.")
    add("")

    add("## 4. Churn by area")
    add("")
    add("| Area | Feature commits | Rewrites | New files | Deleted | "
        "Lines deleted | Lines added |")
    add("|---|---|---|---|---|---|---|")
    for area, stats in sorted(
        analysis["area_stats"].items(),
        key=lambda item: item[1]["rewrites"],
        reverse=True,
    ):
        add(f"| {area} | {stats['feature_commits']:,} | "
            f"{stats['rewrites']:,} | {stats['new_files']:,} | "
            f"{stats['deleted_files']:,} | {stats['total_deleted']:,} | "
            f"{stats['total_added']:,} |")
    add("")

    add("## 5. Most volatile test files")
    add("")
    add(f"Total test files modified: {stability['total_test_files_modified']:,}")
    add("")
    add("| Rank | File | Commits | Added | Deleted |")
    add("|---|---|---|---|---|")
    for rank, row in enumerate(stability["top_files"], 1):
        add(f"| {rank} | `{row['file']}` | {row['commits']:,} | "
            f"{row['total_additions']:,} | {row['total_deletions']:,} |")
    add("")

    add("## 6. Rewrite intensity")
    add("")
    if intensity["total_rewrite_commits"]:
        add(f"- Rewrite commits: {intensity['total_rewrite_commits']:,}")
        add(f"- Min / median / mean / max lines: {intensity['min']} / "
            f"{intensity['median']} / {intensity['mean']} / "
            f"{intensity['max']}")
        add(f"- P95 / P99: {intensity['p95']} / {intensity['p99']}")
        add(f"- Standard deviation: {intensity['stddev']}")
        add("")
        add("| Bucket | Count |")
        add("|---|---|")
        for bucket, count in intensity["distribution"].items():
            add(f"| {bucket} | {count:,} |")
    else:
        add("No rewrite commits were observed.")
    add("")

    add("## 7. Test change pattern (co-evolution, not test-first)")
    add("")
    add(f"- Feature commits: {pattern['feature_commits']:,}")
    add(f"- Feature commits adding test files: "
        f"{pattern['tests_added_with_feature']:,} "
        f"({pattern['co_evolution_pct']}%)")
    add(f"- Feature commits modifying/removing tests: "
        f"{pattern['tests_modified_with_feature']:,}")
    add(f"- Test-only commits (no source change): "
        f"{pattern['test_only_commits']:,}")
    add("")

    add("## 8. Source volatility correlation")
    add("")
    add(f"- Pearson correlation (source churn vs test rewrite lines): "
        f"{correlation['correlation']}")
    add(f"- Data points: {correlation['data_points']:,}")
    if correlation.get("avg_src_lines_per_feature") is not None:
        add(f"- Avg source lines per feature: "
            f"{correlation['avg_src_lines_per_feature']:,}")
        add(f"- Avg test rewrite lines per feature: "
            f"{correlation['avg_test_rewrite_lines_per_feature']:,}")
    add("")

    add("## 9. Efficiency metrics")
    add("")
    add(f"- Source files touched: {efficiency['total_src_files_touched']:,}")
    add(f"- Test files touched: {efficiency['total_test_files_touched']:,}")
    add(f"- Test-to-source file ratio: {efficiency['test_to_source_ratio']}")
    add(f"- Source lines changed: {efficiency['total_src_lines_changed']:,}")
    add(f"- Test lines churned: {efficiency['total_test_lines_churned']:,}")
    add(f"- Test churn per source line: "
        f"{efficiency['test_churn_per_source_line']}")
    add("")

    add("## 10. Commit patterns")
    add("")
    add("| Pattern | Count |")
    add("|---|---|")
    for pattern_name, count in sorted(commit_patterns.items()):
        add(f"| {pattern_name} | {count:,} |")
    add("")

    add("## 11. Domain churn (each file counted once)")
    add("")
    add("| Area | Test files | Source files | Rewrites | New tests | "
        "Deleted tests | Lines deleted | Lines added |")
    add("|---|---|---|---|---|---|---|---|")
    for area, stats in sorted(
        domain.items(), key=lambda item: item[1]["rewrites"], reverse=True
    ):
        add(f"| {area} | {stats['test_files']:,} | {stats['src_files']:,} | "
            f"{stats['rewrites']:,} | {stats['new_tests']:,} | "
            f"{stats['deleted_tests']:,} | {stats['total_deletions']:,} | "
            f"{stats['total_additions']:,} |")
    add("")

    add("## 12. TDD value assessment")
    add("")
    add(f"- Modified-only rewrite ratio: "
        f"**{assessment['rewrite_ratio_modified_only_pct']}%**")
    add(f"- Including-new rewrite ratio: "
        f"**{assessment['rewrite_ratio_including_new_pct']}%**")
    add(f"- {assessment['verdict']}")
    add(f"- {assessment['deletion_note']}")
    add("")
    add("Reading the result: a low modified-only rewrite ratio together with")
    add("frequent additions of test files is the pattern expected when tests")
    add("survive feature evolution. A high ratio suggests tests are coupled to")
    add("implementation details and are rewritten alongside them.")
    add("")
    add("---")
    add("")
    add(f"_Generated by test-churn analysis {meta['analyser_version']}; "
        "UK English; do not edit by hand._")
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_report(
    project_root: str | Path,
    branch: str | None = None,
    since: str | None = None,
    max_commits: int = 0,
    config: ChurnConfig | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    """Run every analysis and return the structured report dictionary."""
    config = config or ChurnConfig()
    root = Path(project_root).expanduser().resolve()
    if not (root / ".git").exists():
        raise GitError(f"{root} is not a git repository")

    resolved_branch = resolve_branch(root, branch)
    commit_data = get_mainline_commits(
        root, resolved_branch, since=since, max_commits=max_commits
    )
    commits = parse_commits(root, commit_data, progress=progress)

    primary = analyse_feature_commits(commits, config)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    primary["generated_at"] = generated_at

    report: dict[str, Any] = {
        "metadata": {
            "analyser_version": ANALYSIS_VERSION,
            "generated_at": generated_at,
            "project_root": str(root),
            "branch": resolved_branch,
            "first_commit": commits[0].sha if commits else "",
            "last_commit": commits[-1].sha if commits else "",
            "since": since or "",
            "max_commits": max_commits,
            "config": asdict(config),
        },
        "primary_metrics": primary,
        "area_stats": primary["area_stats"],
        "temporal_trends": analyse_temporal_trends(commits, config),
        "file_stability": analyse_per_file_stability(commits, config),
        "domain_churn": analyse_domain_churn(commits, config),
        "test_change_pattern": analyse_test_change_pattern(commits, config),
        "rewrite_intensity": analyse_rewrite_intensity(commits, config),
        "source_correlation": analyse_source_volatility_correlation(
            commits, config
        ),
        "efficiency_metrics": analyse_efficiency_metrics(commits, config),
        "commit_patterns": analyse_commit_patterns(commits, config),
    }
    report["tdd_value_assessment"] = _assess_tdd(report["primary_metrics"])
    return report


def write_outputs(
    report: dict[str, Any],
    output_dir: str | Path,
    report_name: str = "test_churn_report.md",
    json_name: str = "test_churn_data.json",
    emit: Iterable[str] = ("report", "json"),
) -> dict[str, str]:
    """Write the report and/or JSON output; return the written paths."""
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    if "report" in emit:
        report_path = out / report_name
        report_path.write_text(generate_report(report), encoding="utf-8")
        written["report"] = str(report_path)
    if "json" in emit:
        json_path = out / json_name
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written["json"] = str(json_path)
    return written


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_churn_analysis",
        description=(
            "Analyse git history to quantify how often test code is rewritten "
            "(TDD value assessment)."
        ),
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="Path to the git repository to analyse (default: current dir).",
    )
    parser.add_argument(
        "--project-root",
        dest="project_root_opt",
        default=None,
        help="Path to the git repository to analyse (overrides positional).",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Mainline branch to analyse (default: dev, main, master, HEAD).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only analyse commits after this date (git --since syntax).",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=0,
        help="Bound the analysis to the most recent N commits (0 = all).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Directory for the generated outputs (default: the project root)."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a JSON classification config (overrides defaults).",
    )
    parser.add_argument(
        "--format",
        choices=("md", "json", "both"),
        default="both",
        help="Which outputs to write (default: both).",
    )
    parser.add_argument(
        "--report-name",
        default="test_churn_report.md",
        help="Filename for the Markdown report.",
    )
    parser.add_argument(
        "--json-name",
        default="test_churn_data.json",
        help="Filename for the JSON output.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Also print the Markdown report to stdout.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output on stderr.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    project_root = args.project_root_opt or args.project_root
    output_dir = args.output_dir or project_root

    config = ChurnConfig()
    if args.config:
        config = ChurnConfig.from_file(args.config)

    emit = {
        "md": ["report"],
        "json": ["json"],
        "both": ["report", "json"],
    }[args.format]

    try:
        report = build_report(
            project_root,
            branch=args.branch,
            since=args.since,
            max_commits=args.max_commits,
            config=config,
            progress=not args.quiet,
        )
    except GitError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    written = write_outputs(
        report,
        output_dir,
        report_name=args.report_name,
        json_name=args.json_name,
        emit=emit,
    )
    if not args.quiet:
        for kind, path in written.items():
            sys.stderr.write(f"{kind}: {path}\n")
    if args.stdout:
        sys.stdout.write(generate_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
