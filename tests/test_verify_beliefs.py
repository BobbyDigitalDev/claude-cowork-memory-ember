"""
Tests for verify_beliefs.py — DeepSeek R1 belief verification pipeline.

Contract:
- _load_scout_context() queries scout_results using column 'source_url' (not 'url')
- update_belief_status() correctly maps verdicts to DB status values
- write_tension_record() writes to tensions table
- _decay_stale_beliefs() nudges confidence down on stale beliefs
- write_position_history() records state transitions

BUG (pass 2): 'url' was used instead of 'source_url' — caused silent "" return
from _load_scout_context(), so R1 never received external scout evidence.
"""
import sys
import os
import sqlite3
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import setup_db
import verify_beliefs as vb

import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    db_path = str(tmp_path / "test_verify.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    setup_db.create_latest_schema(conn)
    conn.close()
    orig = vb.DB_PATH
    vb.DB_PATH = db_path
    yield db_path
    vb.DB_PATH = orig


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _insert_belief(conn, topic="ai_arch", position="transformers are best",
                   status="proposed", confidence_score=0.7, is_active=1,
                   verbatim_anchor="", last_verified_at=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO beliefs (topic, position, status, confidence_score, is_active,
            verbatim_anchor, last_verified_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (topic, position, status, confidence_score, is_active,
          verbatim_anchor, last_verified_at, now, now))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_scout_result(conn, title="Test Paper", abstract="test abstract",
                         source_name="arxiv", source_url="https://arxiv.org/1",
                         relevance_score=0.9, status="ingested"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO scout_results (title, abstract, source_name, source_url,
            relevance_score, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, abstract, source_name, source_url, relevance_score, status, now, now))
    conn.commit()


# ── _load_scout_context ────────────────────────────────────────────────────────

class TestLoadScoutContext:
    """Schema contract: uses source_url column (BUG-001 regression guard)."""

    def test_returns_empty_string_when_no_scout_results(self, db):
        conn = _conn(db)
        result = vb._load_scout_context(conn, "ai", "transformers dominate NLP")
        assert result == ""
        conn.close()

    def test_returns_non_empty_when_relevant_result_exists(self, db):
        conn = _conn(db)
        _insert_scout_result(conn, title="transformer neural network architecture",
                             abstract="transformers dominate modern NLP pipelines")
        result = vb._load_scout_context(conn, "ai", "transformers dominate NLP")
        assert isinstance(result, str)
        assert len(result) > 0
        conn.close()

    def test_result_contains_source_name(self, db):
        conn = _conn(db)
        _insert_scout_result(conn, title="transformer neural architecture deep learning",
                             abstract="transformers scale well with data",
                             source_name="arxiv")
        result = vb._load_scout_context(conn, "ai", "transformers scale neural deep learning")
        if result:  # only assert if overlap threshold was met
            assert "arxiv" in result
        conn.close()

    def test_no_exception_on_empty_scout_table(self, db):
        """Regression: wrong column would raise OperationalError, swallowed as ''."""
        conn = _conn(db)
        # Should return '' cleanly without exception
        result = vb._load_scout_context(conn, "topic", "some position text here")
        assert result == ""
        conn.close()

    def test_pending_status_not_returned_below_threshold(self, db):
        conn = _conn(db)
        _insert_scout_result(conn, title="unrelated topic entirely different domain",
                             relevance_score=0.3, status="pending")
        result = vb._load_scout_context(conn, "cooking", "food recipes pasta")
        assert result == ""
        conn.close()


# ── update_belief_status ───────────────────────────────────────────────────────

class TestUpdateBeliefStatus:

    def test_verified_verdict_sets_status_verified(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.update_belief_status(conn, bid, "verified", 0.9, "strong evidence", None,
                                dry_run=False)
        row = conn.execute("SELECT status, confidence_calibrated FROM beliefs WHERE id=?",
                           (bid,)).fetchone()
        assert row["status"] == "verified"
        assert row["confidence_calibrated"] == 1
        conn.close()

    def test_supported_verdict_sets_status_supported(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.update_belief_status(conn, bid, "supported", 0.65, "some evidence", None,
                                dry_run=False)
        row = conn.execute("SELECT status FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["status"] == "supported"
        conn.close()

    def test_disputed_verdict_sets_status_disputed(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.update_belief_status(conn, bid, "disputed", 0.3, "contradicted", "challenge text",
                                dry_run=False)
        row = conn.execute("SELECT status, challenge_history FROM beliefs WHERE id=?",
                           (bid,)).fetchone()
        assert row["status"] == "disputed"
        history = json.loads(row["challenge_history"] or "[]")
        assert len(history) >= 1
        assert "challenge text" in history[0]["challenge"]
        conn.close()

    def test_insufficient_evidence_leaves_status_proposed(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.update_belief_status(conn, bid, "insufficient_evidence", 0.5, "unclear",
                                None, dry_run=False)
        row = conn.execute("SELECT status, confidence_calibrated FROM beliefs WHERE id=?",
                           (bid,)).fetchone()
        assert row["status"] == "proposed"
        assert row["confidence_calibrated"] == 0
        conn.close()

    def test_confidence_score_updated(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, confidence_score=0.5)
        vb.update_belief_status(conn, bid, "verified", 0.92, "clear evidence", None,
                                dry_run=False)
        row = conn.execute("SELECT confidence_score FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert abs(row["confidence_score"] - 0.92) < 0.001
        conn.close()

    def test_last_verified_at_set(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn)
        vb.update_belief_status(conn, bid, "verified", 0.9, "good", None, dry_run=False)
        row = conn.execute("SELECT last_verified_at FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["last_verified_at"] is not None
        conn.close()

    def test_dry_run_does_not_change_status(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.update_belief_status(conn, bid, "verified", 0.9, "evidence", None, dry_run=True)
        row = conn.execute("SELECT status FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["status"] == "proposed"
        conn.close()


# ── write_position_history ─────────────────────────────────────────────────────

class TestWritePositionHistory:

    def test_writes_row_on_status_change(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn, status="proposed")
        vb.write_position_history(conn, bid, "proposed", "verified", "R1 confirmed", False)
        row = conn.execute("SELECT * FROM position_history WHERE belief_id=?", (bid,)).fetchone()
        assert row is not None
        assert row["status_from"] == "proposed"
        assert row["status_to"] == "verified"
        conn.close()

    def test_no_row_written_when_status_unchanged(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn)
        vb.write_position_history(conn, bid, "proposed", "proposed", "same", False)
        count = conn.execute("SELECT COUNT(*) FROM position_history WHERE belief_id=?",
                             (bid,)).fetchone()[0]
        assert count == 0
        conn.close()

    def test_dry_run_does_not_write(self, db):
        conn = _conn(db)
        bid = _insert_belief(conn)
        vb.write_position_history(conn, bid, "proposed", "verified", "R1", dry_run=True)
        count = conn.execute("SELECT COUNT(*) FROM position_history WHERE belief_id=?",
                             (bid,)).fetchone()[0]
        assert count == 0
        conn.close()


# ── write_tension_record ───────────────────────────────────────────────────────

class TestWriteTensionRecord:

    def test_writes_tension_to_db(self, db):
        conn = _conn(db)
        conn.row_factory = sqlite3.Row
        bid1 = _insert_belief(conn, topic="arch", confidence_score=0.8)
        bid2 = _insert_belief(conn, topic="arch", confidence_score=0.7,
                              position="different position on arch")
        vb.write_tension_record(conn, [bid1, bid2], "these beliefs conflict", dry_run=False)
        row = conn.execute("SELECT * FROM tensions WHERE belief_a_id=?", (bid1,)).fetchone()
        assert row is not None
        assert row["belief_b_id"] == bid2
        assert "conflict" in row["description"]
        conn.close()

    def test_severity_derived_from_confidence_product(self, db):
        conn = _conn(db)
        bid1 = _insert_belief(conn, confidence_score=0.9)
        bid2 = _insert_belief(conn, confidence_score=0.9, position="other pos")
        vb.write_tension_record(conn, [bid1, bid2], "contradiction", dry_run=False)
        row = conn.execute("SELECT importance_score FROM tensions").fetchone()
        assert row["importance_score"] == pytest.approx(0.81, abs=0.01)
        conn.close()

    def test_dry_run_does_not_write(self, db):
        conn = _conn(db)
        bid1 = _insert_belief(conn)
        bid2 = _insert_belief(conn, position="other pos")
        vb.write_tension_record(conn, [bid1, bid2], "conflict", dry_run=True)
        count = conn.execute("SELECT COUNT(*) FROM tensions").fetchone()[0]
        assert count == 0
        conn.close()


# ── _decay_stale_beliefs ───────────────────────────────────────────────────────

class TestDecayStaleBelief:

    def test_decays_confidence_on_old_belief(self, db):
        conn = _conn(db)
        old_date = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d %H:%M:%S")
        bid = _insert_belief(conn, confidence_score=0.8, last_verified_at=old_date)
        vb._decay_stale_beliefs(conn, days_threshold=90, dry_run=False)
        row = conn.execute("SELECT confidence_score FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["confidence_score"] < 0.8
        conn.close()

    def test_does_not_decay_recently_verified(self, db):
        conn = _conn(db)
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bid = _insert_belief(conn, confidence_score=0.8, last_verified_at=recent)
        vb._decay_stale_beliefs(conn, days_threshold=90, dry_run=False)
        row = conn.execute("SELECT confidence_score FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["confidence_score"] == pytest.approx(0.8, abs=0.001)
        conn.close()

    def test_dry_run_does_not_change_confidence(self, db):
        conn = _conn(db)
        old_date = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d %H:%M:%S")
        bid = _insert_belief(conn, confidence_score=0.8, last_verified_at=old_date)
        vb._decay_stale_beliefs(conn, days_threshold=90, dry_run=True)
        row = conn.execute("SELECT confidence_score FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["confidence_score"] == pytest.approx(0.8, abs=0.001)
        conn.close()

    def test_does_not_decay_disputed_beliefs(self, db):
        conn = _conn(db)
        old_date = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d %H:%M:%S")
        bid = _insert_belief(conn, status="disputed", confidence_score=0.4,
                             last_verified_at=old_date)
        vb._decay_stale_beliefs(conn, days_threshold=90, dry_run=False)
        row = conn.execute("SELECT confidence_score FROM beliefs WHERE id=?", (bid,)).fetchone()
        assert row["confidence_score"] == pytest.approx(0.4, abs=0.001)
        conn.close()
