"""
Tests for reflection_agent.py — higher-order session synthesis pipeline.

Contract:
- Loads recent context_snapshots (using the column name `date`, NOT `snapshot_date`)
- Loads recent active beliefs
- Checks whether a reflection is due (last_reflection_date)
- Synthesizes via Qwen (mocked in tests)
- Writes a reflection row and populates belief_reflection_links

Bug coverage:
- BUG-001: context_snapshots column is `date` not `snapshot_date` — confirmed by
  schema contract test that inserts a row with `date` and verifies load returns it.
- BUG-002: write_reflection accesses b["position"] via column name, which crashes
  without conn.row_factory = sqlite3.Row. Tests set row_factory to match fixed
  production code and verify the write succeeds.
"""

import sys
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import setup_db
import reflection_agent as ra

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    """Temporary database with full schema. Returns a path string."""
    db_path = str(tmp_path / "test_reflection.db")
    conn = sqlite3.connect(db_path)
    setup_db.create_latest_schema(conn)
    conn.close()
    return db_path


def _conn(db_path, row_factory=False):
    """Open a connection to the test DB, optionally with sqlite3.Row factory."""
    conn = sqlite3.connect(db_path)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def _insert_snapshot(conn, date_str, content="snapshot content"):
    """Insert a row into context_snapshots using the correct column name `date`."""
    conn.execute(
        "INSERT INTO context_snapshots (date, session_id, version_number, content, word_count, created_at) "
        "VALUES (?, 1, 1, ?, 10, ?)",
        (date_str, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def _insert_belief(conn, topic="test_topic", position="Test belief position text here",
                   status="proposed", confidence_score=0.7):
    """Insert a minimal belief row."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO beliefs "
        "(uuid, topic, position, status, is_active, confidence_score, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
        (str(uuid.uuid4()), topic, position, status, confidence_score, now, now),
    )
    conn.commit()


SAMPLE_RESULT = {
    "patterns_observed": "Bobby consistently iterates fast on prototypes and ships working code.",
    "growth_noted": "Embedding pipeline matured significantly this period.",
    "concerns": "Verification pass still slow on large belief sets.",
    "meta_insights": "The test suite is becoming a reliable safety net.",
    "importance_score": 0.75,
}


# ── TestLoadRecentSnapshots ───────────────────────────────────────────────────

class TestLoadRecentSnapshots:
    """Schema contract for context_snapshots loading.

    BUG-001: the column is `date`, not `snapshot_date`. These tests confirm the
    correct column name is used and that results are filtered by date range.
    """

    def test_returns_empty_list_when_table_is_empty(self, db):
        """Empty table must return [] not raise an exception."""
        conn = _conn(db)
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=10)
        conn.close()
        assert result == []

    def test_schema_contract_date_column_not_snapshot_date(self, db):
        """Inserting a row with column `date` must be returned by load_recent_snapshots.

        This directly tests BUG-001: the function queries the `date` column, not
        `snapshot_date`. If the wrong column name were used, this would return [].
        """
        conn = _conn(db)
        today = datetime.now().strftime("%Y-%m-%d")
        _insert_snapshot(conn, today, content="recent snapshot")
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=10)
        conn.close()
        assert len(result) >= 1
        assert result[0]["content"] == "recent snapshot"

    def test_snapshot_within_window_is_returned(self, db):
        """A snapshot dated 3 days ago is within the 7-day window."""
        conn = _conn(db)
        three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        _insert_snapshot(conn, three_days_ago, content="within window")
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=10)
        conn.close()
        assert any(r["content"] == "within window" for r in result)

    def test_snapshot_outside_window_not_returned(self, db):
        """A snapshot dated 30 days ago is outside the 7-day window."""
        conn = _conn(db)
        old_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        _insert_snapshot(conn, old_date, content="old snapshot outside window")
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=10)
        conn.close()
        assert not any(r["content"] == "old snapshot outside window" for r in result)

    def test_max_snapshots_limits_results(self, db):
        """max_snapshots=2 must return at most 2 rows even if more exist."""
        conn = _conn(db)
        today = datetime.now().strftime("%Y-%m-%d")
        for i in range(5):
            _insert_snapshot(conn, today, content=f"snapshot {i}")
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=2)
        conn.close()
        assert len(result) <= 2

    def test_returned_dict_has_expected_keys(self, db):
        """Each row must have keys: id, date, content, created_at."""
        conn = _conn(db)
        today = datetime.now().strftime("%Y-%m-%d")
        _insert_snapshot(conn, today, content="key check snapshot")
        result = ra.load_recent_snapshots(conn, n_days=7, max_snapshots=10)
        conn.close()
        assert len(result) >= 1
        row = result[0]
        assert "id" in row
        assert "date" in row
        assert "content" in row
        assert "created_at" in row


# ── TestLoadRecentBeliefs ─────────────────────────────────────────────────────

class TestLoadRecentBeliefs:
    """Tests for load_recent_beliefs — active belief retrieval by recency."""

    def test_returns_empty_list_when_no_beliefs(self, db):
        """Empty beliefs table returns []."""
        conn = _conn(db)
        result = ra.load_recent_beliefs(conn, n_days=7)
        conn.close()
        assert result == []

    def test_active_belief_within_window_is_returned(self, db):
        """A belief with updated_at in the last 7 days must appear in results."""
        conn = _conn(db)
        _insert_belief(conn, topic="active_topic", position="Active belief position text")
        result = ra.load_recent_beliefs(conn, n_days=7)
        conn.close()
        assert any(b["topic"] == "active_topic" for b in result)

    def test_returned_dict_has_required_keys(self, db):
        """Each returned belief must have topic, position, status, score."""
        conn = _conn(db)
        _insert_belief(conn, topic="key_check_topic", position="Key check position here")
        result = ra.load_recent_beliefs(conn, n_days=7)
        conn.close()
        assert len(result) >= 1
        row = result[0]
        assert "topic" in row
        assert "position" in row
        assert "status" in row
        assert "score" in row


# ── TestLastReflectionDate ────────────────────────────────────────────────────

class TestLastReflectionDate:
    """Tests for last_reflection_date — controls the weekly reflection gate."""

    def test_returns_none_when_no_reflections(self, db):
        """An empty reflections table must return None, not raise."""
        conn = _conn(db)
        result = ra.last_reflection_date(conn)
        conn.close()
        assert result is None

    def test_returns_date_string_when_reflection_exists(self, db):
        """After writing a reflection, last_reflection_date returns its date."""
        conn = _conn(db, row_factory=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = now[:10]
        ref_uuid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO reflections "
            "(uuid, date, period_covered, start_date, end_date, patterns_observed, "
            "growth_noted, importance_score, triggered_by, last_processed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref_uuid, today, "test period", today, today,
             "test patterns", "test growth", 0.5, "test", now, now, now),
        )
        conn.commit()
        result = ra.last_reflection_date(conn)
        conn.close()
        assert result == today

    def test_returns_most_recent_date_when_multiple_reflections(self, db):
        """Returns the date of the most recently inserted reflection."""
        conn = _conn(db, row_factory=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for date_str in ["2026-01-01", "2026-03-01", "2026-05-01"]:
            conn.execute(
                "INSERT INTO reflections "
                "(uuid, date, period_covered, start_date, end_date, patterns_observed, "
                "growth_noted, importance_score, triggered_by, last_processed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), date_str, "period", date_str, date_str,
                 "patterns", "growth", 0.5, "test", now, now, now),
            )
        conn.commit()
        # last_reflection_date returns the date from ORDER BY id DESC LIMIT 1
        result = ra.last_reflection_date(conn)
        conn.close()
        assert result == "2026-05-01"


# ── TestWriteReflection ───────────────────────────────────────────────────────

class TestWriteReflection:
    """Tests for write_reflection — DB write with BUG-002 regression coverage.

    BUG-002: write_reflection accesses b["position"] via column name dict-style.
    This crashes without conn.row_factory = sqlite3.Row. Tests set row_factory
    to match fixed production code.
    """

    def test_reflection_row_written_to_db(self, db):
        """write_reflection must insert exactly one row into the reflections table."""
        conn = _conn(db, row_factory=True)
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=3,
            dry_run=False,
        )
        row = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        conn.close()
        assert row == 1

    def test_reflection_columns_written_correctly(self, db):
        """Core columns must match the result dict passed to write_reflection."""
        conn = _conn(db, row_factory=True)
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=3,
            dry_run=False,
        )
        row = conn.execute(
            "SELECT patterns_observed, growth_noted, concerns, meta_insights, "
            "importance_score, start_date, end_date, triggered_by "
            "FROM reflections"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == SAMPLE_RESULT["patterns_observed"]
        assert row[1] == SAMPLE_RESULT["growth_noted"]
        assert row[2] == SAMPLE_RESULT["concerns"]
        assert row[3] == SAMPLE_RESULT["meta_insights"]
        assert abs(row[4] - SAMPLE_RESULT["importance_score"]) < 0.001
        assert row[5] == "2026-04-29"
        assert row[6] == "2026-05-05"
        assert row[7] == "reflection_agent.py"

    def test_dry_run_does_not_write_to_db(self, db):
        """dry_run=True must not insert any row into reflections."""
        conn = _conn(db, row_factory=True)
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=3,
            dry_run=True,
        )
        count = conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        conn.close()
        assert count == 0

    def test_belief_reflection_links_populated_on_overlap(self, db):
        """When belief text overlaps the reflection text, belief_reflection_links must be written.

        This tests BUG-002: the row_factory must be set for b["position"] to work.
        The belief position shares keywords with the reflection result so overlap >= 0.08.
        """
        conn = _conn(db, row_factory=True)
        # Insert a belief whose position shares many tokens with SAMPLE_RESULT
        _insert_belief(
            conn,
            topic="embedding_pipeline",
            position="Embedding pipeline matured significantly this period test suite",
        )
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=3,
            dry_run=False,
        )
        count = conn.execute("SELECT COUNT(*) FROM belief_reflection_links").fetchone()[0]
        conn.close()
        assert count >= 1

    def test_no_belief_links_when_no_beliefs_exist(self, db):
        """When beliefs table is empty, belief_reflection_links stays empty (no crash)."""
        conn = _conn(db, row_factory=True)
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=2,
            dry_run=False,
        )
        count = conn.execute("SELECT COUNT(*) FROM belief_reflection_links").fetchone()[0]
        conn.close()
        assert count == 0

    def test_reflection_uuid_is_written(self, db):
        """The uuid column must be populated (non-null, non-empty)."""
        conn = _conn(db, row_factory=True)
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end="2026-05-05",
            n_sessions=1,
            dry_run=False,
        )
        row = conn.execute("SELECT uuid FROM reflections").fetchone()
        conn.close()
        assert row is not None
        assert row[0]  # non-empty string

    def test_last_reflection_date_updated_after_write(self, db):
        """After write_reflection, last_reflection_date must return today's date."""
        conn = _conn(db, row_factory=True)
        today = datetime.now().strftime("%Y-%m-%d")
        ra.write_reflection(
            conn, SAMPLE_RESULT,
            period_start="2026-04-29",
            period_end=today,
            n_sessions=2,
            dry_run=False,
        )
        result = ra.last_reflection_date(conn)
        conn.close()
        assert result == today
