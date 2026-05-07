"""
Tests for setup_db.py — verifies create_latest_schema() produces all expected
tables with the correct critical column names. These are regression guards
against the silent-failure pattern (wrong column name, bare except, no data).
"""
import sqlite3
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    setup_db.create_latest_schema(conn)
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# All expected tables exist
# ---------------------------------------------------------------------------

EXPECTED_TABLES = [
    "beliefs", "goals", "questions", "concepts", "entities", "patterns",
    "epiphanies", "moods", "gratitude", "boundaries", "sessions",
    "conversations", "memory_provenance", "scout_results", "processing_jobs",
    "reflections", "context_snapshots", "schema_migrations",
    "belief_reflection_links", "memory_relationships",
]

class TestTablesExist:
    @pytest.mark.parametrize("table", EXPECTED_TABLES)
    def test_table_exists(self, db, table):
        assert table in _tables(db), f"Expected table '{table}' not found in schema"


# ---------------------------------------------------------------------------
# Critical column names — regression guards for silent SQL failures
# ---------------------------------------------------------------------------

class TestCriticalColumns:
    # scout_results: BUG-001 in verify_beliefs.py was url vs source_url
    def test_scout_results_has_source_url(self, db):
        cols = _cols(db, "scout_results")
        assert "source_url" in cols
        assert "url" not in cols, "'url' is the wrong column name that caused BUG-001"

    # context_snapshots: BUG-001 in reflection_agent.py was snapshot_date vs date
    def test_context_snapshots_has_date_not_snapshot_date(self, db):
        cols = _cols(db, "context_snapshots")
        assert "date" in cols
        assert "snapshot_date" not in cols, "'snapshot_date' is the wrong column name that caused reflection BUG-001"

    # memory_provenance: must have all extended provenance columns
    def test_memory_provenance_has_source_url(self, db):
        assert "source_url" in _cols(db, "memory_provenance")

    def test_memory_provenance_has_source_fetched_at(self, db):
        assert "source_fetched_at" in _cols(db, "memory_provenance")

    def test_memory_provenance_has_processing_job_id(self, db):
        assert "processing_job_id" in _cols(db, "memory_provenance")

    def test_memory_provenance_has_originating_conversation_id(self, db):
        assert "originating_conversation_id" in _cols(db, "memory_provenance")

    # beliefs: quarantine support
    def test_beliefs_has_quarantine_reason(self, db):
        assert "quarantine_reason" in _cols(db, "beliefs")

    # beliefs: column name confidence_score (not just 'confidence')
    def test_beliefs_has_confidence_score(self, db):
        assert "confidence_score" in _cols(db, "beliefs")

    # questions: column is 'question' not 'text'
    def test_questions_column_is_question_not_text(self, db):
        cols = _cols(db, "questions")
        assert "question" in cols
        assert "text" not in cols, "'text' is wrong — caused INSERT failures in tests"

    # concepts, entities, patterns, questions: memory_origin for provenance
    @pytest.mark.parametrize("table", ["concepts", "entities", "patterns", "questions"])
    def test_table_has_memory_origin(self, db, table):
        assert "memory_origin" in _cols(db, table), f"{table} missing memory_origin column"

    # processing_jobs: status field exists
    def test_processing_jobs_has_status(self, db):
        assert "status" in _cols(db, "processing_jobs")

    # reflections: all 14 expected columns
    def test_reflections_has_uuid(self, db):
        assert "uuid" in _cols(db, "reflections")

    def test_reflections_has_patterns_observed(self, db):
        assert "patterns_observed" in _cols(db, "reflections")

    def test_reflections_has_meta_insights(self, db):
        assert "meta_insights" in _cols(db, "reflections")


# ---------------------------------------------------------------------------
# CURRENT_SCHEMA_VERSION constant
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_current_schema_version_is_2_8_0(self):
        assert setup_db.CURRENT_SCHEMA_VERSION == "2.8.0"

    def test_schema_version_not_stale(self):
        # Guard against accidentally bumping back to an old version
        from packaging.version import Version
        assert Version(setup_db.CURRENT_SCHEMA_VERSION) >= Version("2.8.0")
