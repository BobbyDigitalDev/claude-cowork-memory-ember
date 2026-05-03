"""
Tests for write_to_db() in process_conversation.py.

These are integration-style tests that write to a real (temp) SQLite DB
so we catch type errors, schema mismatches, and constraint violations
before they reach production.
"""
import sys, os, sqlite3
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import pytest
import process_conversation as pc


@pytest.fixture(autouse=True)
def patch_db(tmp_path):
    """Redirect all DB writes to a temp file for every test in this module."""
    db_path = str(tmp_path / "test_memory.db")
    # Bootstrap schema using setup_memory
    import setup_db
    orig = setup_db.DB_PATH
    setup_db.DB_PATH = db_path
    setup_db.setup_database()
    setup_db.DB_PATH = orig
    # Patch process_conversation to use the same temp DB
    orig_pc = pc.DB_PATH
    pc.DB_PATH = db_path
    yield db_path
    pc.DB_PATH = orig_pc


def _run_write(db_path, extractions):
    """Helper: call write_to_db with fixed session/conv IDs."""
    pc.write_to_db(
        session_id=1,
        conv_id=1,
        extractions=extractions,
        prompts={},
        conv_text="test conversation text",
        filename="test_2026_01_01_001.md",
        session_date="2026-01-01",
    )
    return sqlite3.connect(db_path)


class TestTagsListCoercion:
    """
    The bug that triggered this test suite: Qwen returns tags as a list
    and SQLite's Python driver rejects it with ProgrammingError.
    Every table with a tags column must handle list input gracefully.
    """

    def _base_extraction(self):
        return {
            "summary": {"title": "t", "summary": "s", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [], "epiphanies": [], "questions": [], "goals": [],
            "entities": [], "concepts": [], "mood": {}, "gratitude": [], "patterns": [],
        }

    def test_belief_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["beliefs"] = [{
            "topic": "t", "position": "p", "confidence": "high",
            "confidence_score": 0.8, "evidence_snippets": [],
            "source_type": "direct_message", "tags": ["a", "b"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM beliefs").fetchone()
        assert row is not None
        assert "a" in row[0] and "b" in row[0]

    def test_epiphany_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["epiphanies"] = [{
            "description": "d", "significance": "high", "confidence_score": 0.8,
            "evidence_snippets": [], "source_type": "model_inference",
            "tags": ["insight", "memory"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM epiphanies").fetchone()
        assert row is not None

    def test_goal_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["goals"] = [{
            "description": "do something", "priority": "immediate",
            "status": "pending", "tags": ["mvp", "testing"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM goals").fetchone()
        assert row is not None

    def test_pattern_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["patterns"] = [{
            "name": "n", "description": "d", "pattern_type": "thinking_pattern",
            "first_observed": "2026-01-01", "recurrence": "once",
            "significance": "s", "supporting_evidence": "e",
            "importance_score": 0.5, "tags": ["tag1", "tag2"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM patterns").fetchone()
        assert row is not None

    def test_concept_tags_as_none(self, patch_db):
        """None tags should produce an empty string, not NULL error."""
        ex = self._base_extraction()
        ex["concepts"] = [{
            "name": "n", "description": "d", "tags": None,
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM concepts").fetchone()
        assert row is not None
        assert row[0] == ""

    def test_evidence_snippets_as_list_serialised(self, patch_db):
        """evidence_snippets must be JSON-serialised when stored."""
        ex = self._base_extraction()
        ex["beliefs"] = [{
            "topic": "t", "position": "p", "confidence": "high",
            "confidence_score": 0.8,
            "evidence_snippets": ["quote one", "quote two"],
            "source_type": "direct_message", "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        import json
        row = conn.execute("SELECT evidence_snippets FROM beliefs").fetchone()
        assert row is not None
        parsed = json.loads(row[0])
        assert parsed == ["quote one", "quote two"]


class TestMalformedExtractionHandling:
    """write_to_db should skip malformed items without crashing."""

    def _base(self):
        return {
            "summary": {"title": "", "summary": "", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [], "epiphanies": [], "questions": [], "goals": [],
            "entities": [], "concepts": [], "mood": {}, "gratitude": [], "patterns": [],
        }

    def test_belief_as_string_skipped(self, patch_db):
        """If Qwen returns a string instead of a dict, skip it without crashing."""
        ex = self._base()
        ex["beliefs"] = ["this is not a dict"]
        _run_write(patch_db, ex)   # should not raise
        conn = sqlite3.connect(patch_db)
        count = conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
        assert count == 0

    def test_empty_extractions_no_crash(self, patch_db):
        """Completely empty extraction should not crash."""
        ex = self._base()
        _run_write(patch_db, ex)   # should not raise
