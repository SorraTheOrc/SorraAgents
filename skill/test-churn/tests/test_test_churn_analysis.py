"""Unit tests for the corrected test-churn analysis.

Coverage is organised around the four corrected metrics from the re-homed
ContextHub defect item (``SA-0MV2JE6MG003B0SW``): real git status detection,
rewrite-ratio denominators, distinct-commit temporal aggregation, and
whole-file deletion accounting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "tests/test_app.py",
        "src/test_helpers.py",
        "pkg/mod_test.py",
        "app/component.test.ts",
        "app/component.spec.js",
        "spec/thing.py",
    ],
)
def test_is_test_file_recognises_generic_patterns(churn, path):
    config = churn.ChurnConfig()
    assert churn.is_test_file(path, config) is True


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "src/component.ts", "scripts/build.sh", "plan/main.go"],
)
def test_is_src_file_recognises_code_outside_tests(churn, path):
    config = churn.ChurnConfig()
    assert churn.is_test_file(path, config) is False
    assert churn.is_src_file(path, config) is True


def test_is_src_file_ignores_non_code_files(churn):
    config = churn.ChurnConfig()
    assert churn.is_src_file("docs/guide.md", config) is False
    assert churn.is_src_file("README.md", config) is False


def test_is_src_file_excludes_documentation_and_vendor(churn):
    config = churn.ChurnConfig()
    assert churn.is_src_file("docs/example.py", config) is False
    assert churn.is_src_file("vendor/lib.py", config) is False
    assert churn.is_src_file("node_modules/pkg/index.js", config) is False


def test_classify_area_never_returns_a_filename(churn):
    config = churn.ChurnConfig()
    assert churn.classify_area("tests/test_app.py", config) == "tests"
    assert churn.classify_area("tests/unit/test_app.py", config) == "tests/unit"
    assert churn.classify_area("src/app.py", config) == "src"


def test_classify_area_honours_explicit_rules(churn):
    config = churn.ChurnConfig(
        area_rules=[{"pattern": r"^packages/([^/]+)/", "area": "packages"}]
    )
    assert (
        churn.classify_area("packages/herdr/src/x.py", config) == "packages"
    )


# ---------------------------------------------------------------------------
# Git status detection
# ---------------------------------------------------------------------------

def test_parse_name_status_handles_renames_and_plain_statuses(churn):
    raw = "R100\0old.py\0new.py\0A\0added.py\0D\0gone.py\0"
    entries = churn._parse_name_status(raw)
    assert entries[0] == {
        "status": "R",
        "path": "new.py",
        "old_path": "old.py",
    }
    assert entries[1]["status"] == "A"
    assert entries[2]["status"] == "D"


def test_parse_numstat_handles_renames_and_plain_counts(churn):
    raw = "2\t1\t\0old.py\0new.py\0" "5\t0\tadded.py\0" "-\t-\timg.png\0"
    entries = churn._parse_numstat(raw)
    assert entries[0] == {
        "additions": 2,
        "deletions": 1,
        "path": "new.py",
        "old_path": "old.py",
    }
    assert entries[1]["path"] == "added.py"
    assert entries[2]["additions"] == 0  # binary file marker
    assert entries[2]["deletions"] == 0


def test_get_commit_diffs_uses_real_name_status(churn, git_repo):
    log = churn.run_git(
        git_repo, "log", "dev", "--reverse", "--pretty=format:%H"
    ).splitlines()
    add_changes = churn.get_commit_diffs(git_repo, log[0])
    statuses = {c.path: c.status for c in add_changes}
    assert statuses["tests/test_app.py"] == "A"
    assert statuses["src/app.py"] == "A"

    modify_changes = churn.get_commit_diffs(git_repo, log[1])
    assert {c.status for c in modify_changes} == {"M"}

    rename_changes = churn.get_commit_diffs(git_repo, log[2])
    renames = [c for c in rename_changes if c.status == "R"]
    assert renames, "expected a rename to be detected"
    assert renames[0].path == "tests/test_math.py"
    assert renames[0].old_path == "tests/test_app.py"

    delete_changes = churn.get_commit_diffs(git_repo, log[3])
    deleted = [c for c in delete_changes if c.status == "D"]
    assert [c.path for c in deleted] == ["tests/test_math.py"]
    assert deleted[0].deletions > 0


# ---------------------------------------------------------------------------
# Rewrite-ratio arithmetic
# ---------------------------------------------------------------------------

def _commit(churn, files, date="2026-01-01"):
    return churn.Commit(
        sha="a" * 40,
        short_sha="aaaaaaa",
        message="test",
        date=date,
        author="tester",
        files=files,
    )


def test_rewrite_ratio_reports_both_denominators(churn):
    commits = [
        _commit(
            churn,
            [
                churn.FileChange("src/x.py", "M", additions=10, deletions=2),
                # rewrite: 3 deletions / 5 additions
                churn.FileChange("tests/test_x.py", "M", additions=5, deletions=3),
                # pure-addition modification must enter the denominator
                churn.FileChange("tests/test_y.py", "M", additions=4, deletions=0),
                # new test file only enters the "including-new" denominator
                churn.FileChange("tests/test_z.py", "A", additions=8, deletions=0),
            ],
        )
    ]
    result = churn.analyse_feature_commits(commits, churn.ChurnConfig())

    # Modified-only: deletions 3 / (3 + (5 + 4)) = 3 / 12 = 25.0%
    assert result["modified_test_deletions"] == 3
    assert result["modified_only_denominator"] == 12
    assert result["rewrite_ratio_modified_only_pct"] == 25.0
    # Including new: 3 / (12 + 8) = 15.0%
    assert result["including_new_denominator"] == 20
    assert result["rewrite_ratio_including_new_pct"] == 15.0


def test_rewrite_ratio_zero_denominator_is_safe(churn):
    commits = [_commit(churn, [churn.FileChange("src/x.py", "A", 1, 0)])]
    result = churn.analyse_feature_commits(commits, churn.ChurnConfig())
    assert result["rewrite_ratio_modified_only_pct"] == 0.0
    assert result["rewrite_ratio_including_new_pct"] == 0.0


# ---------------------------------------------------------------------------
# Temporal aggregation
# ---------------------------------------------------------------------------

def test_temporal_counts_distinct_commits_not_events(churn):
    # One commit with two rewritten test files must count as ONE commit.
    commit = _commit(
        churn,
        [
            churn.FileChange("src/x.py", "M", 4, 1),
            churn.FileChange("tests/a.py", "M", 5, 5),
            churn.FileChange("tests/b.py", "M", 5, 5),
        ],
        date="2026-05-10",
    )
    temporal = churn.analyse_temporal_trends([commit], churn.ChurnConfig())
    assert len(temporal) == 1
    row = temporal[0]
    assert row["period"] == "2026-05"
    assert row["feature_commits"] == 1
    assert row["feature_commits_with_rewrites"] == 1
    assert row["rewrite_rate_pct"] == 100.0


def test_temporal_rate_never_exceeds_one_hundred(churn):
    commits = []
    for day in ("2026-05-01", "2026-05-02", "2026-05-03"):
        commits.append(
            _commit(
                churn,
                [
                    churn.FileChange("src/x.py", "M", 2, 1),
                    churn.FileChange("tests/a.py", "M", 5, 5),
                    churn.FileChange("tests/b.py", "M", 5, 5),
                ],
                date=day,
            )
        )
    temporal = churn.analyse_temporal_trends(commits, churn.ChurnConfig())
    row = temporal[0]
    assert row["feature_commits"] == 3
    assert row["feature_commits_with_rewrites"] == 3
    assert row["rewrite_rate_pct"] == 100.0


# ---------------------------------------------------------------------------
# Whole-file deletion accounting
# ---------------------------------------------------------------------------

def test_deletion_accounting_includes_test_only_commits(churn):
    feature = _commit(
        churn,
        [
            churn.FileChange("src/x.py", "A", 3, 0),
            churn.FileChange("tests/old.py", "D", 0, 7),
        ],
        date="2026-01-01",
    )
    test_only = _commit(
        churn,
        [churn.FileChange("tests/other.py", "D", 0, 3)],
        date="2026-01-02",
    )
    result = churn.analyse_feature_commits(
        [feature, test_only], churn.ChurnConfig()
    )
    assert result["deleted_test_files"] == 2
    assert result["deleted_test_lines"] == 10


# ---------------------------------------------------------------------------
# Growth accounting (added / deleted / modified / net)
# ---------------------------------------------------------------------------

def test_growth_accounting_totals_and_area(churn):
    commits = [
        _commit(
            churn,
            [
                churn.FileChange("src/app.py", "A", additions=3, deletions=0),
                # new test file alongside a source change (feature commit)
                churn.FileChange("tests/test_app.py", "A", additions=5, deletions=0),
            ],
            date="2026-01-01",
        ),
        _commit(
            churn,
            [
                # rewrite: renamed test file counts as modified
                churn.FileChange("tests/test_app.py", "R", additions=2, deletions=1),
            ],
            date="2026-02-01",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/test_app.py", "D", additions=0, deletions=4)],
            date="2026-03-01",
        ),
        # a test-only commit adding a file must count toward "added"
        _commit(
            churn,
            [churn.FileChange("tests/test_new.py", "A", additions=3, deletions=0)],
            date="2026-04-01",
        ),
    ]
    growth = churn.analyse_growth_accounting(commits, churn.ChurnConfig())

    # Totals: 2 additions (feature + test-only), 1 deletion, 1 rename-modified.
    assert growth["files_added"] == 2
    assert growth["files_deleted"] == 1
    assert growth["files_modified"] == 1
    assert growth["files_net_growth"] == 1
    assert growth["lines_added"] == 5 + 2 + 3
    assert growth["lines_deleted"] == 1 + 4
    assert growth["lines_net_growth"] == (5 + 2 + 3) - (1 + 4)

    # Per-area: both test files classify under the ``tests`` area.
    area = growth["area"]["tests"]
    assert area["files_added"] == 2
    assert area["files_deleted"] == 1
    assert area["files_modified"] == 1
    assert area["files_net_growth"] == 1


def test_growth_accounting_counts_delete_and_readd_once_each(churn):
    """A file deleted and re-added is an addition and a deletion (AC5).

    Per-event accounting must not under- or double-count: two add events and
    one delete event yield net growth +1.
    """
    commits = [
        _commit(
            churn,
            [churn.FileChange("tests/feature.py", "A", additions=6, deletions=0)],
            date="2026-01-01",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/feature.py", "D", additions=0, deletions=6)],
            date="2026-02-01",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/feature.py", "A", additions=4, deletions=0)],
            date="2026-03-01",
        ),
    ]
    growth = churn.analyse_growth_accounting(commits, churn.ChurnConfig())
    assert growth["files_added"] == 2
    assert growth["files_deleted"] == 1
    assert growth["files_net_growth"] == 1
    assert growth["lines_added"] == 10
    assert growth["lines_deleted"] == 6
    assert growth["lines_net_growth"] == 4


def test_growth_accounting_ignores_non_test_changes(churn):
    commits = [
        _commit(
            churn,
            [
                churn.FileChange("src/app.py", "A", additions=9, deletions=0),
                churn.FileChange("docs/guide.md", "A", additions=7, deletions=0),
            ],
        )
    ]
    growth = churn.analyse_growth_accounting(commits, churn.ChurnConfig())
    assert growth["files_added"] == 0
    assert growth["lines_added"] == 0
    assert growth["files_net_growth"] == 0
    assert growth["area"] == {}


# ---------------------------------------------------------------------------
# Quarterly growth trend
# ---------------------------------------------------------------------------

def test_quarterly_trend_groups_by_calendar_quarter(churn):
    commits = [
        _commit(
            churn,
            [churn.FileChange("tests/a.py", "A", additions=3, deletions=0)],
            date="2026-01-10",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/b.py", "A", additions=4, deletions=0)],
            date="2026-04-10",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/c.py", "A", additions=5, deletions=0)],
            date="2026-10-10",
        ),
    ]
    trend = churn.analyse_growth_trend_by_quarter(commits, churn.ChurnConfig())
    periods = [row["quarter"] for row in trend]
    # Continuous Q1..Q4: the empty quarter (Q3) is reported as zero, not omitted.
    assert periods == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    by_quarter = {row["quarter"]: row for row in trend}
    assert by_quarter["2026-Q1"]["files_added"] == 1  # 2026-01-10
    assert by_quarter["2026-Q2"]["files_added"] == 1  # 2026-04-10
    assert by_quarter["2026-Q3"]["files_added"] == 0  # no commits -> zero row
    assert by_quarter["2026-Q4"]["files_added"] == 1  # 2026-10-10
    assert all(row["files_net_growth"] >= 0 for row in trend)


def test_quarterly_trend_spans_year_boundary(churn):
    commits = [
        _commit(
            churn,
            [churn.FileChange("tests/a.py", "D", additions=0, deletions=2)],
            date="2025-11-15",
        ),
        _commit(
            churn,
            [churn.FileChange("tests/b.py", "A", additions=3, deletions=0)],
            date="2026-02-01",
        ),
    ]
    trend = churn.analyse_growth_trend_by_quarter(commits, churn.ChurnConfig())
    # Range covers first..last commit quarter and rolls over the year boundary.
    assert [row["quarter"] for row in trend] == ["2025-Q4", "2026-Q1"]
    assert trend[0]["files_deleted"] == 1
    assert trend[1]["files_added"] == 1


def test_quarterly_trend_empty_history_is_empty(churn):
    assert churn.analyse_growth_trend_by_quarter([], churn.ChurnConfig()) == []


# ---------------------------------------------------------------------------
# Domain churn
# ---------------------------------------------------------------------------

def test_domain_churn_counts_each_file_once(churn):
    commit = _commit(
        churn,
        [
            churn.FileChange("src/a.py", "A", 1, 0),
            churn.FileChange("src/b.py", "A", 1, 0),
            churn.FileChange("tests/test_c.py", "A", 2, 0),
        ],
    )
    result = churn.analyse_domain_churn([commit], churn.ChurnConfig())
    assert result["src"]["src_files"] == 2
    assert result["tests"]["test_files"] == 1
    assert result["tests"]["new_tests"] == 1


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_are_portable(churn):
    config = churn.ChurnConfig()
    # No ContextHub-specific path may remain in the defaults.
    blob = json.dumps(churn.asdict(config))
    assert "ContextHub" not in blob
    assert "packages/herdr" not in blob


def test_config_from_file_merges_and_rejects_unknown_keys(churn, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"test_dir_segments": ["spec"]}), encoding="utf-8"
    )
    config = churn.ChurnConfig.from_file(path)
    assert config.test_dir_segments == ["spec"]
    assert config.code_extensions  # defaults retained

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        churn.ChurnConfig.from_file(bad)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def test_build_report_is_self_contained(churn, git_repo):
    report = churn.build_report(git_repo, branch="dev")
    assert report["metadata"]["project_root"] == str(git_repo.resolve())
    assert report["primary_metrics"]["total_commits"] == 4
    assert report["primary_metrics"]["deleted_test_files"] == 1
    assert report["tdd_value_assessment"]["rewrite_ratio_modified_only_pct"] >= 0
    # Fixture history: A (test_app.py), M, R, D -> 1 added, 2 modified, 1 deleted.
    growth = report["growth_metrics"]
    assert growth["files_added"] == 1
    assert growth["files_deleted"] == 1
    assert growth["files_modified"] == 2
    assert growth["files_net_growth"] == 0
    assert growth["lines_net_growth"] == growth["lines_added"] - growth["lines_deleted"]
    trend = report["quarterly_growth_trends"]
    assert trend and trend[0]["quarter"] == "2026-Q1"
    assert trend[-1]["quarter"] == "2026-Q2"
    text = churn.generate_report(report)
    assert "TDD value assessment" in text
    assert "Scope and definitions" in text
    assert "Rewrite" in text
    assert "## 3. Test growth — added, deleted, net" in text
    assert "## 4. Quarterly growth trend (calendar quarters)" in text


def test_write_outputs_produces_both_files(churn, git_repo, tmp_path):
    report = churn.build_report(git_repo, branch="dev")
    written = churn.write_outputs(report, tmp_path)
    assert Path(written["report"]).read_text(encoding="utf-8").startswith(
        "# Test Churn Report"
    )
    data = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    assert data["metadata"]["branch"] == "dev"
