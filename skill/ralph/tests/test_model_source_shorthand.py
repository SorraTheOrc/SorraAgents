"""Tests for model source shorthand syntax in ralph_loop.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skill.ralph.scripts.ralph_loop import _preprocess_args, build_parser, MODEL_SOURCES


class TestPreprocessArgs:
    """Tests for the _preprocess_args function that handles shorthand syntax."""

    def test_no_args_returns_empty(self):
        assert _preprocess_args(None) == []
        assert _preprocess_args([]) == []

    def test_only_work_item_id(self):
        assert _preprocess_args(["SA-123"]) == ["SA-123"]

    def test_remote_shorthand(self):
        result = _preprocess_args(["SA-123", "remote"])
        assert result == ["SA-123", "--model-source", "remote"]

    def test_local_shorthand(self):
        result = _preprocess_args(["SA-123", "local"])
        assert result == ["SA-123", "--model-source", "local"]

    def test_shorthand_with_other_flags(self):
        result = _preprocess_args(["SA-123", "remote", "--verbose", "--json"])
        assert result == ["SA-123", "--model-source", "remote", "--verbose", "--json"]

    def test_existing_model_source_flag_not_duplicated(self):
        result = _preprocess_args(["SA-123", "--model-source", "remote"])
        assert result == ["SA-123", "--model-source", "remote"]

    def test_shorthand_with_other_flags_before(self):
        result = _preprocess_args(["--verbose", "SA-123", "remote"])
        assert result == ["--verbose", "SA-123", "--model-source", "remote"]

    def test_non_model_source_string_after_work_item_id(self):
        result = _preprocess_args(["SA-123", "--verbose"])
        assert result == ["SA-123", "--verbose"]

    def test_model_source_with_hyphen_not_treated_as_shorthand(self):
        result = _preprocess_args(["SA-123", "-v"])
        assert result == ["SA-123", "-v"]


class TestBuildParser:
    """Tests for the build_parser function."""

    def test_parse_with_remote_shorthand(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "remote"]))
        assert args.work_item_id == "SA-123"
        assert args.model_source == "remote"

    def test_parse_with_local_shorthand(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "local"]))
        assert args.work_item_id == "SA-123"
        assert args.model_source == "local"

    def test_parse_with_explicit_model_source_flag(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "--model-source", "remote"]))
        assert args.work_item_id == "SA-123"
        assert args.model_source == "remote"

    def test_parse_without_model_source(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123"]))
        assert args.work_item_id == "SA-123"
        assert args.model_source is None

    def test_parse_with_all_options(self):
        parser = build_parser()
        args = parser.parse_args(_preprocess_args(["SA-123", "local", "--verbose", "--json", "--max-attempts", "5"]))
        assert args.work_item_id == "SA-123"
        assert args.model_source == "local"
        assert args.verbose is True
        assert args.json is True
        assert args.max_attempts == 5


class TestMainIntegration:
    """Integration tests for the main function with shorthand syntax."""

    def test_main_with_preprocessed_args(self):
        """Test that _preprocess_args correctly converts shorthand to flag."""
        from skill.ralph.scripts.ralph_loop import main

        # Test that preprocessed args work correctly
        args = _preprocess_args(["SA-123", "remote"])
        parser = build_parser()
        parsed = parser.parse_args(args)
        assert parsed.work_item_id == "SA-123"
        assert parsed.model_source == "remote"

    def test_main_with_explicit_flag(self):
        """Test that explicit --model-source flag still works."""
        from skill.ralph.scripts.ralph_loop import main

        args = _preprocess_args(["SA-123", "--model-source", "remote"])
        parser = build_parser()
        parsed = parser.parse_args(args)
        assert parsed.work_item_id == "SA-123"
        assert parsed.model_source == "remote"
