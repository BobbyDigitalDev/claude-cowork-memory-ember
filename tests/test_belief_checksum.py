"""
Tests for belief_checksum.py — stress-test high-confidence beliefs against external sources.

Contract:
- get_triggered_beliefs() returns beliefs meeting confidence/epiphany/tension triggers
- --conversation filter joins memory_provenance -> conversations on source_filename
- write_research_task() writes to research_tasks table
- keyword_overlap_score() scores scout results against belief keywords

BUG-003 (pass 2): conv_filter used `processing_jobs WHERE target_type='belief'` which
never matched. Fixed to join memory_provenance -> conversations on source_filename.
"""
import sys
import os
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import setup_db
import belief_checksum as bc

import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    db_path = str(tmp_path / "test_checksum.db")
    conn = sqlite3.connect(db_path)
    setup_db.create_latest_schema(conn)
    conn.close()
    orig = bc.DB_PATH
    bc.DB_PATH = tmp_path / "test_checksum.db"
    yield db_path
    bc.DB_PATH = orig


def _conn(db_path):
    return sqlite3.connect(db_path)


def _insert_belief(conn, topic="test", position="test position text here",
                   confidence_score=0.85, status="proposed", is_active=1):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO beliefs (topic, position, confidence_score, status, is_active,
            created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (topic, position, confidence_score, status, is_active, now, now))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_conversation(conn, source_filename="bobby_2026_05_01_001.md"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO conversations (date, source_filename, created_at, updated_at)
        VALUES (?, ?, ?, ?)
    """, ("2026-05-01", source_filename, now, now))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_provenance(conn, belief_id, conversation_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO memory_provenance (memory_type, memory_id, originating_conversation_id,
            created_at)
        VALUES ('belief', ?, ?, ?)
    """, (belief_id, conversation_id, now))
    conn.commit()


# ── get_triggered_beliefs ──────────────────────────────────────────────────────

class TestGetTriggeredBeliefs:

    def test_returns_empty_when_no_beliefs(self, db):
        conn = _conn(db)
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert result == []
        conn.close()

    def test_returns_high_confidence_belief(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.90)
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert len(result) == 1
        conn.close()

    def test_excludes_below_threshold_belief(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.50)
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert result == []
        conn.close()

    def test_excludes_deprecated_belief(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.95, status="deprecated")
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert result == []
        conn.close()

    def test_excludes_inactive_belief(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.95, is_active=0)
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert result == []
        conn.close()

    def test_result_has_expected_keys(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.90)
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0)
        assert set(result[0].keys()) >= {"id", "topic", "position", "confidence_score", "status"}
        conn.close()

    def test_respects_limit(self, db):
        conn = _conn(db)
        for _ in range(5):
            _insert_belief(conn, confidence_score=0.90)
        result = bc.get_triggered_beliefs(conn, limit=3, recency_days=0)
        assert len(result) <= 3
        conn.close()


class TestConvFilter:
    """BUG-003 regression: --conversation filter must use memory_provenance join."""

    def test_returns_belief_linked_to_conversation(self, db):
        conn = _conn(db)
        conv_id = _insert_conversation(conn, "bobby_2026_05_01_001.md")
        bid = _insert_belief(conn, confidence_score=0.90)
        _insert_provenance(conn, bid, conv_id)

        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0,
                                          conv_filter="bobby_2026_05_01_001.md")
        ids = [r["id"] for r in result]
        assert bid in ids
        conn.close()

    def test_excludes_belief_from_different_conversation(self, db):
        conn = _conn(db)
        conv_id = _insert_conversation(conn, "bobby_2026_05_01_001.md")
        bid = _insert_belief(conn, confidence_score=0.90)
        _insert_provenance(conn, bid, conv_id)

        # Filter for a different file — belief should NOT appear
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0,
                                          conv_filter="bobby_2026_04_15_001.md")
        ids = [r["id"] for r in result]
        assert bid not in ids
        conn.close()

    def test_conv_filter_empty_when_no_provenance(self, db):
        conn = _conn(db)
        _insert_belief(conn, confidence_score=0.90)
        # Belief exists but has no provenance row -> filter returns nothing
        result = bc.get_triggered_beliefs(conn, limit=40, recency_days=0,
                                          conv_filter="bobby_2026_05_01_001.md")
        assert result == []
        conn.close()


# ── write_research_task ────────────────────────────────────────────────────────

class TestWriteResearchTask:

    def test_writes_pending_task_when_no_matches(self, db):
        conn = _conn(db)
        belief = {"id": 1, "topic": "ai", "position": "test position", "confidence_score": 0.9}
        bc.write_research_task(conn, belief, [], dry_run=False)
        row = conn.execute("SELECT status, triggered_by FROM research_tasks").fetchone()
        assert row is not None
        assert row[0] == "pending"
        assert row[1] == "belief:1"
        conn.close()

    def test_writes_fulfilled_task_when_matches_found(self, db):
        conn = _conn(db)
        belief = {"id": 2, "topic": "ai", "position": "test position", "confidence_score": 0.85}
        matches = [(0.7, {"source_name": "arxiv", "title": "relevant paper", "abstract": "..."})]
        bc.write_research_task(conn, belief, matches, dry_run=False)
        row = conn.execute("SELECT status FROM research_tasks WHERE triggered_by='belief:2'").fetchone()
        assert row[0] == "fulfilled"
        conn.close()

    def test_dry_run_does_not_write(self, db):
        conn = _conn(db)
        belief = {"id": 3, "topic": "ai", "position": "test", "confidence_score": 0.9}
        bc.write_research_task(conn, belief, [], dry_run=True)
        count = conn.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0]
        assert count == 0
        conn.close()


# ── keyword_overlap_score ──────────────────────────────────────────────────────

class TestKeywordOverlapScore:

    def test_perfect_overlap(self):
        keywords = ["transformer", "neural", "architecture"]
        score = bc.keyword_overlap_score(keywords, "transformer neural architecture", "")
        assert score == pytest.approx(1.0)

    def test_zero_overlap(self):
        keywords = ["transformer", "neural"]
        score = bc.keyword_overlap_score(keywords, "cooking pasta recipe", "baking bread")
        assert score == 0.0

    def test_partial_overlap(self):
        keywords = ["transformer", "neural", "language"]
        score = bc.keyword_overlap_score(keywords, "transformer model", "")
        assert 0.0 < score < 1.0

    def test_empty_keywords_returns_zero(self):
        score = bc.keyword_overlap_score([], "some title", "some abstract")
        assert score == 0.0
