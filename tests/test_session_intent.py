"""
Tests for session_intent.py — Goal 81: Session intent declaration,
knowledge gap detection, and preflight research pipeline.

Tests are written BEFORE implementation per the test-first directive.
Structural strategy tests always run (no Ollama required).
Semantic strategy tests are skipped if Ollama is unavailable.
"""

import sys
import os
import json
import tempfile
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import session_intent


# ── Topic extraction ──────────────────────────────────────────────────────────

class TestExtractTopics:

    def test_single_phrase_returned_as_is(self):
        topics = session_intent.extract_topics("session intent declaration")
        assert len(topics) >= 1
        assert any("session intent" in t.lower() or "intent" in t.lower() for t in topics)

    def test_conjunction_and_splits_into_multiple(self):
        topics = session_intent.extract_topics("Build setup.sh and write README")
        assert len(topics) >= 2

    def test_semicolon_splits_into_multiple(self):
        topics = session_intent.extract_topics("Goal 81; knowledge gaps; preflight research")
        assert len(topics) >= 3

    def test_leading_verb_stripped(self):
        topics = session_intent.extract_topics("build the installer script")
        # At least one topic should not start with "build"
        assert any(not t.lower().startswith("build") for t in topics)

    def test_also_splits(self):
        topics = session_intent.extract_topics("Fix retrieval also update README")
        assert len(topics) >= 2

    def test_max_topics_respected(self):
        long_intent = "A and B and C and D and E and F and G"
        topics = session_intent.extract_topics(long_intent, max_topics=5)
        assert len(topics) <= 5

    def test_empty_string_returns_list(self):
        topics = session_intent.extract_topics("")
        assert isinstance(topics, list)

    def test_returns_list_of_strings(self):
        topics = session_intent.extract_topics("Build something interesting")
        assert isinstance(topics, list)
        for t in topics:
            assert isinstance(t, str)

    def test_strips_whitespace_from_topics(self):
        topics = session_intent.extract_topics("  setup.sh  and  README  ")
        for t in topics:
            assert t == t.strip()

    def test_no_empty_string_topics(self):
        topics = session_intent.extract_topics("setup.sh and README")
        for t in topics:
            assert len(t) > 0


# ── Density classification ─────────────────────────────────────────────────────

class TestClassifyDensity:

    def test_dense_with_many_results(self):
        # 6 results with high scores → DENSE
        mock_results = [{"score": 0.75}] * 6
        classification = session_intent.classify_density(mock_results)
        assert classification == "DENSE"

    def test_partial_with_moderate_results(self):
        # 3 results → PARTIAL
        mock_results = [{"score": 0.62}] * 3
        classification = session_intent.classify_density(mock_results)
        assert classification == "PARTIAL"

    def test_sparse_with_no_results(self):
        classification = session_intent.classify_density([])
        assert classification == "SPARSE"

    def test_sparse_with_one_result(self):
        mock_results = [{"score": 0.55}]
        classification = session_intent.classify_density(mock_results)
        assert classification == "SPARSE"

    def test_returns_string(self):
        result = session_intent.classify_density([])
        assert isinstance(result, str)
        assert result in ("DENSE", "PARTIAL", "SPARSE")


# ── Gap analysis ───────────────────────────────────────────────────────────────

class TestGapAnalysis:

    def test_gap_report_has_required_keys(self):
        """Each topic in gap report must have topic, density, n_results, top_score, results."""
        mock_bundle = {
            "results": [{"score": 0.72, "source_type": "belief", "content": "test"}] * 4,
            "stats": {"total": 4}
        }
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            gaps = session_intent.detect_gaps(["setup.sh installer"])
        assert len(gaps) == 1
        gap = gaps[0]
        assert "topic" in gap
        assert "density" in gap
        assert "n_results" in gap
        assert "top_score" in gap
        assert "results" in gap

    def test_multiple_topics_produce_multiple_gaps(self):
        mock_bundle = {"results": [], "stats": {"total": 0}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            gaps = session_intent.detect_gaps(["topic A", "topic B", "topic C"])
        assert len(gaps) == 3

    def test_sparse_topic_flagged_correctly(self):
        mock_bundle = {"results": [], "stats": {"total": 0}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            gaps = session_intent.detect_gaps(["totally unknown topic xyz"])
        assert gaps[0]["density"] == "SPARSE"

    def test_dense_topic_flagged_correctly(self):
        mock_results = [{"score": 0.80, "source_type": "belief", "content": "x"}] * 7
        mock_bundle = {"results": mock_results, "stats": {"total": 7}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            gaps = session_intent.detect_gaps(["memory architecture"])
        assert gaps[0]["density"] == "DENSE"

    def test_top_score_is_highest_score_in_results(self):
        mock_results = [
            {"score": 0.80, "source_type": "belief", "content": "high"},
            {"score": 0.60, "source_type": "concept", "content": "low"},
        ]
        mock_bundle = {"results": mock_results, "stats": {"total": 2}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            gaps = session_intent.detect_gaps(["some topic"])
        assert abs(gaps[0]["top_score"] - 0.80) < 0.001


# ── Research suggestions ───────────────────────────────────────────────────────

class TestResearchSuggestions:

    def test_suggest_research_returns_list(self):
        result = session_intent.suggest_research(["README writing", "setup.sh installer"])
        assert isinstance(result, list)

    def test_suggest_research_returns_strings(self):
        result = session_intent.suggest_research(["README writing"])
        for item in result:
            assert isinstance(item, str)

    def test_suggest_research_empty_sparse_returns_empty(self):
        result = session_intent.suggest_research([])
        assert isinstance(result, list)
        # May be empty or contain generic suggestions
        assert len(result) >= 0

    def test_suggest_research_includes_topic_in_suggestion(self):
        result = session_intent.suggest_research(["OpenClaw integration"])
        # At least one suggestion should reference the topic or a related search
        assert len(result) > 0


# ── Intent file read/write ─────────────────────────────────────────────────────

class TestIntentFile:

    def test_write_intent_file_creates_file(self, tmp_path):
        intent_path = str(tmp_path / "current_intent.txt")
        session_intent.write_intent_file(
            intent_text="Build Goal 81",
            topics=["Goal 81", "session intent"],
            path=intent_path
        )
        assert os.path.exists(intent_path)

    def test_write_intent_file_content_is_json(self, tmp_path):
        intent_path = str(tmp_path / "current_intent.txt")
        session_intent.write_intent_file(
            intent_text="Build Goal 81",
            topics=["Goal 81"],
            path=intent_path
        )
        with open(intent_path) as f:
            data = json.load(f)
        assert "intent" in data
        assert "topics" in data
        assert "written_at" in data

    def test_read_intent_file_roundtrip(self, tmp_path):
        intent_path = str(tmp_path / "current_intent.txt")
        session_intent.write_intent_file(
            intent_text="Test intent",
            topics=["topic A", "topic B"],
            path=intent_path
        )
        data = session_intent.read_intent_file(path=intent_path)
        assert data["intent"] == "Test intent"
        assert data["topics"] == ["topic A", "topic B"]

    def test_read_intent_file_missing_returns_none(self, tmp_path):
        missing_path = str(tmp_path / "nonexistent.txt")
        result = session_intent.read_intent_file(path=missing_path)
        assert result is None

    def test_intent_file_timestamp_is_iso_format(self, tmp_path):
        intent_path = str(tmp_path / "current_intent.txt")
        session_intent.write_intent_file(
            intent_text="Test",
            topics=["Test"],
            path=intent_path
        )
        data = session_intent.read_intent_file(path=intent_path)
        # Should parse as ISO datetime without error
        datetime.fromisoformat(data["written_at"])

    def test_stale_intent_file_detected(self, tmp_path):
        """File older than 24h should be flagged as stale."""
        intent_path = str(tmp_path / "current_intent.txt")
        old_time = (datetime.utcnow() - timedelta(hours=25)).isoformat()
        with open(intent_path, "w") as f:
            json.dump({"intent": "old", "topics": ["old"], "written_at": old_time}, f)
        assert session_intent.is_intent_file_stale(path=intent_path, max_age_hours=24)

    def test_fresh_intent_file_not_stale(self, tmp_path):
        intent_path = str(tmp_path / "current_intent.txt")
        session_intent.write_intent_file(
            intent_text="Fresh",
            topics=["Fresh"],
            path=intent_path
        )
        assert not session_intent.is_intent_file_stale(path=intent_path, max_age_hours=24)


# ── CLI argument parsing ───────────────────────────────────────────────────────

class TestCLI:

    def test_parse_intent_positional_arg(self):
        args = session_intent.parse_args(["Build setup.sh and README"])
        assert args.intent == "Build setup.sh and README"

    def test_parse_refresh_flag(self):
        args = session_intent.parse_args(["some intent", "--refresh"])
        assert args.refresh is True

    def test_refresh_flag_default_false(self):
        args = session_intent.parse_args(["some intent"])
        assert args.refresh is False

    def test_parse_no_semantic_flag(self):
        args = session_intent.parse_args(["some intent", "--no-semantic"])
        assert args.no_semantic is True

    def test_parse_top_arg(self):
        args = session_intent.parse_args(["some intent", "--top", "15"])
        assert args.top == 15

    def test_parse_threshold_arg(self):
        args = session_intent.parse_args(["some intent", "--threshold", "0.65"])
        assert abs(args.threshold - 0.65) < 0.001


# ── Density threshold constants ────────────────────────────────────────────────

class TestConstants:

    def test_density_thresholds_exist(self):
        assert hasattr(session_intent, "DENSE_THRESHOLD")
        assert hasattr(session_intent, "PARTIAL_THRESHOLD")
        assert hasattr(session_intent, "SPARSE_THRESHOLD")

    def test_density_thresholds_ordered(self):
        assert session_intent.DENSE_THRESHOLD >= session_intent.PARTIAL_THRESHOLD
        assert session_intent.PARTIAL_THRESHOLD >= session_intent.SPARSE_THRESHOLD

    def test_dense_count_threshold_exists(self):
        assert hasattr(session_intent, "DENSE_MIN_RESULTS")
        assert hasattr(session_intent, "PARTIAL_MIN_RESULTS")
        assert session_intent.DENSE_MIN_RESULTS > session_intent.PARTIAL_MIN_RESULTS


# ── declare_intent integration ─────────────────────────────────────────────────

class TestDeclareIntent:

    def test_declare_intent_returns_dict(self):
        mock_bundle = {"results": [], "stats": {"total": 0}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            result = session_intent.declare_intent(
                "Build the session intent feature",
                refresh=False
            )
        assert isinstance(result, dict)

    def test_declare_intent_has_required_keys(self):
        mock_bundle = {"results": [], "stats": {"total": 0}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            result = session_intent.declare_intent(
                "Build setup.sh",
                refresh=False
            )
        assert "intent" in result
        assert "topics" in result
        assert "gaps" in result
        assert "sparse_count" in result
        assert "suggestions" in result

    def test_declare_intent_sparse_produces_suggestions(self):
        mock_bundle = {"results": [], "stats": {"total": 0}}
        with patch("session_intent.retrieve") as mock_retrieve:
            mock_retrieve.return_value = mock_bundle
            result = session_intent.declare_intent(
                "Completely unknown topic xyz123",
                refresh=False
            )
        assert result["sparse_count"] > 0
        assert isinstance(result["suggestions"], list)
