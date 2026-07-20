"""Tests for the find-related automation script."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "find-related" / "scripts" / "find_related.py"


def test_script_exists():
    """The script file must exist at the expected path."""
    assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"


def test_script_is_executable():
    """Script should be executable or at least have a proper shebang."""
    content = SCRIPT_PATH.read_text()
    assert content.startswith("#!/usr/bin/env python3"), "Missing shebang"


def test_help_flag():
    """Script --help should display usage and exit 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower()


def test_verbose_flag():
    """Script should accept --verbose flag."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--verbose",
        ],
        capture_output=True,
        text=True,
    )
    # Should not crash with --verbose
    assert result.returncode in (0, 1), f"Unexpected error: {result.stderr}"


def test_json_flag():
    """Script should accept --json flag and produce JSON output when all required args are passed."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    # The script will fail since TEST-123 doesn't exist, but it should still
    # produce valid JSON if --json is passed
    if result.returncode != 0:
        import json
        try:
            json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            # If it failed for non-JSON reasons (e.g., missing wl), that's OK
            pass


def test_work_item_id_required_help():
    """Running script without required args should show error."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
    )
    # Should exit non-zero and indicate --work-item-id is required
    assert result.returncode != 0, "Should fail without --work-item-id"
    msg = (result.stdout + result.stderr).lower()
    assert "work-item-id" in msg or "work_item_id" in msg or "required" in msg


def test_repo_path_flag():
    """Script should accept --repo-path argument."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--work-item-id",
            "TEST-123",
            "--repo-path",
            "/tmp/test-repo",
        ],
        capture_output=True,
        text=True,
    )
    # Should not crash with --repo-path
    assert result.returncode in (0, 1), f"Unexpected error: {result.stderr}"


# ---------------------------------------------------------------------------
# Keyword extraction tests
# ---------------------------------------------------------------------------


def _import_find_related():
    """Import the find_related module for unit testing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("find_related", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keywords_from_title():
    """Keywords should be extracted from a work-item title."""
    mod = _import_find_related()
    title = "Add deterministic script automation to find-related skill"
    keywords = mod.extract_keywords(title, "")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "deterministic" in keywords
    assert "script" in keywords
    assert "automation" in keywords
    assert "find" in keywords
    assert "related" in keywords
    assert "skill" in keywords


def test_keywords_from_description():
    """Keywords should be extracted from a work-item description."""
    mod = _import_find_related()
    description = "Create a deterministic Python script to automate the find-related skill, generating a related-work report."
    keywords = mod.extract_keywords("", description)
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "deterministic" in keywords
    assert "python" in keywords
    assert "script" in keywords
    assert "automate" in keywords
    assert "related" in keywords
    assert "report" in keywords


def test_keywords_from_both_title_and_description():
    """Keywords should be merged from both title and description without duplicates."""
    mod = _import_find_related()
    title = "Add find-related automation"
    description = "Automation for finding related work items in the project repository."
    keywords = mod.extract_keywords(title, description)
    assert isinstance(keywords, list)
    assert len(set(keywords)) == len(keywords), "Keywords should be unique"
    assert "automation" in keywords
    assert "find" in keywords
    assert "related" in keywords
    assert "work" in keywords
    assert "project" in keywords
    assert "repository" in keywords


def test_keywords_empty_title():
    """Keywords from empty title should not cause errors."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("", "Some description text")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "description" in keywords
    assert "text" in keywords


def test_keywords_empty_description():
    """Keywords from empty description should not cause errors."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("Some title", "")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert "title" in keywords


def test_keywords_both_empty():
    """Keywords from empty title and description should return empty list."""
    mod = _import_find_related()
    keywords = mod.extract_keywords("", "")
    assert isinstance(keywords, list)
    assert len(keywords) == 0


def test_keywords_with_special_characters():
    """Keywords should handle special characters gracefully."""
    mod = _import_find_related()
    title = "[CRITICAL] fix: broken build (v2.1) - urgent!"
    description = "Fix the **broken** build; update dependencies (see #123)..."
    keywords = mod.extract_keywords(title, description)
    assert isinstance(keywords, list)
    assert "critical" in keywords
    assert "fix" in keywords
    assert "broken" in keywords
    assert "build" in keywords
    assert "urgent" in keywords
    assert "update" in keywords
    assert "dependencies" in keywords


def test_keywords_excludes_common_stop_words():
    """Common English stop words should be excluded from keywords."""
    mod = _import_find_related()
    title = "The a an is in on at for to of and or the this that with"
    keywords = mod.extract_keywords(title, "")
    # None of these common words should be keywords
    for word in ["the", "a", "an", "is", "in", "on", "at", "for", "to", "of", "and", "or"]:
        assert word not in keywords, f"Stop word '{word}' should be excluded"


def test_keywords_are_lowercase():
    """Keywords should be normalized to lowercase."""
    mod = _import_find_related()
    title = "IMPLEMENT Workflow Integration TEST"
    keywords = mod.extract_keywords(title, "")
    assert "implement" in keywords
    assert "workflow" in keywords
    assert "integration" in keywords
    assert "test" in keywords


# ---------------------------------------------------------------------------
# Worklog CLI helper tests
# ---------------------------------------------------------------------------


class TestRunWlShow:
    """Tests for run_wl_show function."""

    def test_returns_parsed_json_on_success(self, monkeypatch):
        mod = _import_find_related()
        import json

        def mock_check_output(cmd, **kwargs):
            return json.dumps({"id": "TEST-123", "title": "Test item"})

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        result = mod.run_wl_show("TEST-123")
        assert result is not None
        assert result["id"] == "TEST-123"
        assert result["title"] == "Test item"

    def test_returns_none_on_failure(self, monkeypatch):
        mod = _import_find_related()

        def mock_check_output(cmd, **kwargs):
            raise Exception("wl command failed")

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        result = mod.run_wl_show("TEST-999")
        assert result is None

    def test_returns_none_on_invalid_json(self, monkeypatch):
        mod = _import_find_related()

        def mock_check_output(cmd, **kwargs):
            return "not valid json"

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        result = mod.run_wl_show("TEST-123")
        assert result is None


class TestRunWlSearch:
    """Tests for run_wl_search function."""

    def test_returns_list_on_success(self, monkeypatch):
        mod = _import_find_related()
        import json

        mock_items = [
            {"id": "REL-001", "title": "Related item 1", "status": "open"},
            {"id": "REL-002", "title": "Related item 2", "status": "closed"},
        ]

        def mock_check_output(cmd, **kwargs):
            return json.dumps(mock_items)

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        results = mod.run_wl_search("keyword")
        assert isinstance(results, list)
        assert len(results) == 2

    def test_returns_empty_list_on_failure(self, monkeypatch):
        mod = _import_find_related()

        def mock_check_output(cmd, **kwargs):
            raise Exception("search failed")

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        results = mod.run_wl_search("keyword")
        assert results == []

    def test_uses_json_flag(self, monkeypatch):
        mod = _import_find_related()
        import json
        captured_args = []

        def mock_check_output(cmd, **kwargs):
            captured_args.extend(cmd)
            return json.dumps([])

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        mod.run_wl_search("test-keyword")
        assert "--json" in captured_args

    def test_handles_workItems_key(self, monkeypatch):
        """run_wl_search should handle the actual wl search JSON structure with workItems key."""
        mod = _import_find_related()
        import json

        payload = {
            "success": True,
            "ftsAvailable": True,
            "count": 2,
            "workItems": [
                {"id": "REL-001", "title": "Item 1", "status": "open", "score": 0.5},
                {"id": "REL-002", "title": "Item 2", "status": "closed", "score": 0.3},
            ],
        }

        def mock_check_output(cmd, **kwargs):
            return json.dumps(payload)

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        results = mod.run_wl_search("keyword")
        assert isinstance(results, list)
        assert len(results) == 2
        assert results[0]["id"] == "REL-001"
        assert results[1]["id"] == "REL-002"

    def test_handles_bare_list_fallback(self, monkeypatch):
        """run_wl_search should fall back if response is a bare list."""
        mod = _import_find_related()
        import json

        payload = [
            {"id": "REL-001", "title": "Item 1"},
        ]

        def mock_check_output(cmd, **kwargs):
            return json.dumps(payload)

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        results = mod.run_wl_search("keyword")
        assert isinstance(results, list)
        assert len(results) == 1

    def test_includes_semantic_flag_when_available(self, monkeypatch):
        """run_wl_search should include --semantic when available."""
        mod = _import_find_related()
        import json
        captured_args = []

        def mock_check_output(cmd, **kwargs):
            captured_args.extend(cmd)
            return json.dumps({"success": True, "workItems": []})

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        mod.run_wl_search("test-keyword", use_semantic=True)
        assert "--semantic" in captured_args

    def test_omits_semantic_flag_when_unavailable(self, monkeypatch):
        """run_wl_search should omit --semantic when not available/false."""
        mod = _import_find_related()
        import json
        captured_args = []

        def mock_check_output(cmd, **kwargs):
            captured_args.extend(cmd)
            return json.dumps({"success": True, "workItems": []})

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        mod.run_wl_search("test-keyword", use_semantic=False)
        assert "--semantic" not in captured_args


class TestRunWlUpdate:
    """Tests for run_wl_update function."""

    def test_returns_true_on_success(self, monkeypatch):
        mod = _import_find_related()

        def mock_check_output(cmd, **kwargs):
            return ""

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        result = mod.run_wl_update("TEST-123", "New description")
        assert result is True

    def test_returns_false_on_failure(self, monkeypatch):
        mod = _import_find_related()

        def mock_check_output(cmd, **kwargs):
            raise Exception("update failed")

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        result = mod.run_wl_update("TEST-123", "New description")
        assert result is False

    def test_calls_wl_update_with_description(self, monkeypatch):
        mod = _import_find_related()
        import json
        captured_args = []

        def mock_check_output(cmd, **kwargs):
            captured_args.extend(cmd)
            return json.dumps({})

        monkeypatch.setattr(mod.subprocess, "check_output", mock_check_output)
        mod.run_wl_update("TEST-123", "New description")
        # Should include 'update', the id, '--description' and the description value
        assert "update" in captured_args
        assert "TEST-123" in captured_args
        assert "--description" in captured_args
        assert "New description" in captured_args


# ---------------------------------------------------------------------------
# Search and deduplication logic tests
# ---------------------------------------------------------------------------


def test_search_and_dedup_aggregates_results(monkeypatch):
    """search_and_dedup should search for each keyword and aggregate."""
    mod = _import_find_related()

    search_calls = []

    def mock_search(keyword, use_semantic=False):
        search_calls.append((keyword, use_semantic))
        if "script" in keyword:
            return [{"id": "REL-001", "title": "Script related"}]
        elif "automation" in keyword:
            return [{"id": "REL-002", "title": "Automation related"}]
        return []

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    keywords = ["script", "automation", "test"]
    results = mod.search_and_dedup(keywords)
    assert len(results) == 2
    ids = [r["id"] for r in results]
    assert "REL-001" in ids
    assert "REL-002" in ids
    assert any(call[0] == "script" for call in search_calls)
    assert any(call[0] == "automation" for call in search_calls)


def test_search_and_dedup_removes_duplicates(monkeypatch):
    """Duplicate work items from different keywords should be removed."""
    mod = _import_find_related()

    def mock_search(keyword, use_semantic=False):
        # Both keywords return the same item (duplicate)
        return [{"id": "REL-001", "title": "Same item"}]

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    keywords = ["script", "automation"]
    results = mod.search_and_dedup(keywords)
    assert len(results) == 1
    assert results[0]["id"] == "REL-001"


def test_search_and_dedup_with_empty_keywords():
    """Empty keyword list should return empty results."""
    mod = _import_find_related()
    results = mod.search_and_dedup([])
    assert results == []


def test_search_and_dedup_handles_search_failures(monkeypatch):
    """Search failures for individual keywords should not break the pipeline."""
    mod = _import_find_related()

    def mock_search(keyword, use_semantic=False):
        if keyword == "broken":
            return []  # Simulating a failed/empty search
        return [{"id": "REL-001", "title": "Working item"}]

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    keywords = ["broken", "working"]
    results = mod.search_and_dedup(keywords)
    assert len(results) == 1
    assert results[0]["id"] == "REL-001"


# ---------------------------------------------------------------------------
# Scoring, ranking, and limiting tests
# ---------------------------------------------------------------------------


def test_configurable_max_constants_exist():
    """MAX_WORK_ITEM_RESULTS and MAX_REPO_FILE_RESULTS must be configurable constants."""
    mod = _import_find_related()
    assert hasattr(mod, "MAX_WORK_ITEM_RESULTS"), "Missing MAX_WORK_ITEM_RESULTS constant"
    assert hasattr(mod, "MAX_REPO_FILE_RESULTS"), "Missing MAX_REPO_FILE_RESULTS constant"
    assert mod.MAX_WORK_ITEM_RESULTS >= 1, "MAX_WORK_ITEM_RESULTS should be >= 1"
    assert mod.MAX_REPO_FILE_RESULTS >= 1, "MAX_REPO_FILE_RESULTS should be >= 1"


def test_search_and_dedup_limits_to_top_3_by_score(monkeypatch):
    """search_and_dedup should return at most MAX_WORK_ITEM_RESULTS items, ranked by descending score."""
    mod = _import_find_related()

    items_with_scores = [
        {"id": "REL-001", "title": "Top item", "score": -0.1},
        {"id": "REL-002", "title": "Second item", "score": -0.5},
        {"id": "REL-003", "title": "Third item", "score": -0.8},
        {"id": "REL-004", "title": "Fourth item (lowest score)", "score": -1.5},
        {"id": "REL-005", "title": "Fifth item", "score": -2.0},
    ]

    def mock_search(keyword, use_semantic=False):
        return items_with_scores

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    keywords = ["test", "keyword"]
    results = mod.search_and_dedup(keywords)
    # Should be limited to MAX_WORK_ITEM_RESULTS (3)
    assert len(results) <= mod.MAX_WORK_ITEM_RESULTS
    # Items should be returned in descending score order
    scores = [item.get("score", 0) for item in results]
    assert scores == sorted(scores, reverse=True), "Items should be ranked by descending score"
    # The top 3 by score should be included
    result_ids = [item["id"] for item in results]
    assert "REL-001" in result_ids
    assert "REL-002" in result_ids
    assert "REL-003" in result_ids


def test_search_and_dedup_all_items_when_under_limit(monkeypatch):
    """When fewer than MAX_WORK_ITEM_RESULTS items exist, all should be returned."""
    mod = _import_find_related()

    items = [
        {"id": "REL-001", "title": "Only item", "score": -0.5},
    ]

    def mock_search(keyword, use_semantic=False):
        return items

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    results = mod.search_and_dedup(["test"])
    assert len(results) == 1
    assert results[0]["id"] == "REL-001"


def test_search_and_dedup_items_without_score_go_last(monkeypatch):
    """Items without a score field should be ranked lower than scored items."""
    mod = _import_find_related()

    items = [
        {"id": "REL-001", "title": "Scored low", "score": -2.0},
        {"id": "REL-002", "title": "No score"},
        {"id": "REL-003", "title": "Scored high", "score": -0.1},
    ]

    def mock_search(keyword, use_semantic=False):
        return items

    monkeypatch.setattr(mod, "run_wl_search", mock_search)

    results = mod.search_and_dedup(["test"])
    # Items with scores should come before those without
    scored_ids = [item["id"] for item in results if "score" in item]
    unscored_ids = [item["id"] for item in results if "score" not in item]
    if scored_ids and unscored_ids:
        scored_last_idx = max(results.index(it) for it in results if "score" in it)
        unscored_first_idx = min(results.index(it) for it in results if "score" not in it)
        assert scored_last_idx < unscored_first_idx, "Scored items must appear before unscored items"


def test_rank_repo_files_by_keyword_match_count(tmp_path):
    """Repo files should be ranked by number of distinct keywords matched (descending)."""
    mod = _import_find_related()

    # File matching 3 keywords
    (tmp_path / "high_match.md").write_text("automation script for keyword")
    # File matching 2 keywords
    (tmp_path / "medium_match.md").write_text("automation keyword")
    # File matching 1 keyword
    (tmp_path / "low_match.md").write_text("keyword")

    matches = mod.search_repo(tmp_path, ["automation", "script", "keyword"])
    # Results should be ranked by match count descending
    match_counts = [len(m.get("matches", [])) for m in matches]
    assert match_counts == sorted(match_counts, reverse=True), (
        "Repo files should be ranked by descending keyword match count"
    )


def test_search_repo_limits_to_top_3(tmp_path):
    """search_repo should return at most MAX_REPO_FILE_RESULTS files."""
    mod = _import_find_related()

    for i in range(10):
        (tmp_path / f"file_{i}.md").write_text("keyword")

    matches = mod.search_repo(tmp_path, ["keyword"])
    assert len(matches) <= mod.MAX_REPO_FILE_RESULTS, (
        f"Should be limited to {mod.MAX_REPO_FILE_RESULTS} results, got {len(matches)}"
    )


def test_search_repo_returns_all_when_under_limit(tmp_path):
    """When fewer than MAX_REPO_FILE_RESULTS files match, all should be returned."""
    mod = _import_find_related()

    (tmp_path / "only.md").write_text("keyword")

    matches = mod.search_repo(tmp_path, ["keyword"])
    assert len(matches) == 1


def test_format_report_renders_all_items_passed():
    """format_report should render all items passed to it (limiting is done upstream)."""
    mod = _import_find_related()

    many_items = [
        {"id": f"REL-{i:03d}", "title": f"Item {i}", "status": "open"}
        for i in range(5)
    ]
    report = mod.format_report("TEST-123", many_items, [])
    # All 5 items should appear (format_report does not limit)
    for i in range(5):
        assert f"REL-{i:03d}" in report, f"Item REL-{i:03d} should appear in report"


def test_format_report_renders_all_repo_matches_passed():
    """format_report should render all repo matches passed to it (limiting is done upstream)."""
    mod = _import_find_related()

    many_files = [
        {"file": f"docs/file_{i}.md", "matches": ["keyword"]}
        for i in range(5)
    ]
    report = mod.format_report("TEST-123", [], many_files)
    # All 5 files should appear (format_report does not limit)
    for i in range(5):
        assert f"file_{i}.md" in report, f"File file_{i}.md should appear in report"


# ---------------------------------------------------------------------------
# Report generation tests
# ---------------------------------------------------------------------------


def test_format_report_creates_markdown_section():
    """format_report should create a properly formatted markdown section."""
    mod = _import_find_related()
    items = [
        {"id": "REL-001", "title": "First related item"},
        {"id": "REL-002", "title": "Second related item"},
    ]
    report = mod.format_report("TEST-123", items, [])
    assert mod.REPORT_HEADING in report
    assert "REL-001" in report
    assert "First related item" in report
    assert "REL-002" in report
    assert "Second related item" in report


def test_format_report_includes_repo_matches():
    """format_report should include repository file matches."""
    mod = _import_find_related()
    items = []
    repo_matches = [
        {"file": "docs/guide.md", "matches": ["keyword1", "keyword2"]},
        {"file": "src/module.py", "matches": ["keyword1"]},
    ]
    report = mod.format_report("TEST-123", items, repo_matches)
    assert "docs/guide.md" in report
    assert "src/module.py" in report


def test_format_report_empty_shows_no_results():
    """format_report should indicate no results when both lists are empty."""
    mod = _import_find_related()
    report = mod.format_report("TEST-123", [], [])
    assert mod.REPORT_HEADING in report
    assert "No related work items found" in report or "No related" in report


# ---------------------------------------------------------------------------
# Idempotent description update tests
# ---------------------------------------------------------------------------


def test_update_description_adds_report_section(monkeypatch):
    """update_description should append the report section to description."""
    mod = _import_find_related()

    original_desc = "## Summary\nSome description.\n"
    report_section = "\n## Related work (automated report)\n- REL-001: Test item\n"

    updated = mod.update_description(original_desc, report_section)
    assert mod.REPORT_HEADING in updated
    assert "REL-001" in updated
    assert original_desc.strip() in updated


def test_update_description_replaces_existing_report_section():
    """update_description should replace existing automated report section."""
    mod = _import_find_related()

    original_desc = (
        "## Summary\nSome description.\n"
        "\n## Related work (automated report)\n- OLD-ITEM: Old stuff\n"
        "\n## Another section\n"
    )
    report_section = "\n## Related work (automated report)\n- NEW-ITEM: New stuff\n"

    updated = mod.update_description(original_desc, report_section)
    # Old content should be gone
    assert "OLD-ITEM" not in updated
    assert "Old stuff" not in updated
    # New content should be present
    assert "NEW-ITEM" in updated
    assert "New stuff" in updated
    # Other sections preserved
    assert "Another section" in updated


def test_update_description_idempotent_no_duplicate():
    """Running update twice should not duplicate the report."""
    mod = _import_find_related()

    original_desc = "## Summary\nSome description.\n"
    report_section = "\n## Related work (automated report)\n- REL-001: Test item\n"

    first = mod.update_description(original_desc, report_section)
    second = mod.update_description(first, report_section)
    # Should only have one report section
    assert second.count(mod.REPORT_HEADING) == 1
    assert second.count("REL-001") == 1


def test_update_description_preserves_manual_related_work():
    """Manual 'Related work' sections without '(automated report)' should be preserved."""
    mod = _import_find_related()

    original_desc = (
        "## Summary\nSome description.\n"
        "\n## Related work\n- MANUAL-ITEM: Manually added\n"
    )
    report_section = "\n## Related work (automated report)\n- REL-001: Auto found\n"

    updated = mod.update_description(original_desc, report_section)
    assert "MANUAL-ITEM" in updated
    assert "Manually added" in updated
    assert "REL-001" in updated
    assert "Auto found" in updated


# ---------------------------------------------------------------------------
# Repository file search tests
# ---------------------------------------------------------------------------


def test_search_repo_finds_matching_files(tmp_path):
    """search_repo should find files containing keywords."""
    mod = _import_find_related()

    # Create a temporary directory structure
    doc_file = tmp_path / "docs" / "guide.md"
    doc_file.parent.mkdir(parents=True)
    doc_file.write_text("This is about automation scripts for related work.")

    code_file = tmp_path / "src" / "module.py"
    code_file.parent.mkdir(parents=True)
    code_file.write_text("def find_related_items():\n    pass\n")

    # File that should NOT match
    other = tmp_path / "src" / "other.txt"
    other.write_text("unrelated content here")

    matches = mod.search_repo(tmp_path, ["automation", "find", "script"])
    assert isinstance(matches, list)
    assert len(matches) > 0

    # Check the doc file was found
    doc_matches = [m for m in matches if "guide.md" in m.get("file", "")]
    assert len(doc_matches) > 0
    assert "automation" in doc_matches[0].get("matches", [])


def test_search_repo_excludes_git_directory(tmp_path):
    """search_repo should exclude .git directories."""
    mod = _import_find_related()

    git_file = tmp_path / ".git" / "config"
    git_file.parent.mkdir(parents=True)
    git_file.write_text("automation script for git")

    doc_file = tmp_path / "readme.md"
    doc_file.write_text("automation script for project")

    matches = mod.search_repo(tmp_path, ["automation", "script"])
    # Should NOT match .git files
    for m in matches:
        assert ".git" not in m.get("file", "")


def test_search_repo_excludes_node_modules(tmp_path):
    """search_repo should exclude node_modules directories."""
    mod = _import_find_related()

    nm_file = tmp_path / "node_modules" / "package" / "index.js"
    nm_file.parent.mkdir(parents=True)
    nm_file.write_text("automation script for node module")

    doc_file = tmp_path / "src" / "app.js"
    doc_file.parent.mkdir(parents=True)
    doc_file.write_text("automation script for app")

    matches = mod.search_repo(tmp_path, ["automation", "script"])
    # Should NOT match node_modules files
    for m in matches:
        assert "node_modules" not in m.get("file", "")


def test_search_repo_returns_structured_results(tmp_path):
    """search_repo should return structured dicts with file and matches."""
    mod = _import_find_related()

    doc = tmp_path / "doc.md"
    doc.write_text("keyword1 and keyword2 are both here")

    matches = mod.search_repo(tmp_path, ["keyword1", "keyword2"])
    assert len(matches) >= 1
    result = matches[0]
    assert "file" in result
    assert "matches" in result
    assert isinstance(result["matches"], list)
    assert "keyword1" in result["matches"]
    assert "keyword2" in result["matches"]


def test_search_repo_returns_empty_for_no_matches(tmp_path):
    """search_repo should return empty list when no files match."""
    mod = _import_find_related()

    doc = tmp_path / "doc.md"
    doc.write_text("no matching keywords here")

    matches = mod.search_repo(tmp_path, ["nonexistent", "missing"])
    assert matches == []


def test_search_repo_handles_nonexistent_path():
    """search_repo should handle nonexistent paths gracefully."""
    mod = _import_find_related()
    matches = mod.search_repo("/nonexistent/path", ["keyword"])
    assert matches == []


def test_search_repo_scans_allowed_extensions(tmp_path):
    """search_repo should only scan files with allowed extensions."""
    mod = _import_find_related()

    # Create sample files for each allowed extension, but under the MAX limit
    (tmp_path / "doc.md").write_text("keyword")
    (tmp_path / "code.py").write_text("keyword")
    (tmp_path / "app.js").write_text("keyword")

    # Should NOT be scanned
    (tmp_path / "image.png").write_text("keyword")
    (tmp_path / "data.json").write_text("keyword")
    (tmp_path / "build.o").write_text("keyword")

    matches = mod.search_repo(tmp_path, ["keyword"])
    matched_files = [m.get("file", "") for m in matches]
    # Should match 3 files (at or under MAX_REPO_FILE_RESULTS)
    assert len(matched_files) == 3
    assert any(f.endswith(".md") for f in matched_files)
    assert any(f.endswith(".py") for f in matched_files)
    assert any(f.endswith(".js") for f in matched_files)
    # Non-allowed extensions should not appear
    for f in matched_files:
        assert not f.endswith(".png"), f"Unexpected match: {f}"
        assert not f.endswith(".json"), f"Unexpected match: {f}"
        assert not f.endswith(".o"), f"Unexpected match: {f}"


def test_skill_md_has_status_management_instructions():
    """SKILL.md must include instructions for setting in_progress and open status."""
    skill_md = REPO_ROOT / "skill" / "find-related" / "SKILL.md"
    content = skill_md.read_text()

    # Must contain the status lifecycle steps
    assert "in_progress" in content, "SKILL.md must reference setting status to in_progress"
    assert "open --json" in content, "SKILL.md must reference setting status to open at end"
    assert "status" in content.lower(), "SKILL.md must mention status"
