"""
Tests for memory_curator.py — cosine(), run_stale_goals(), run_question_audit().
No Ollama required: embed_text and Ollama-dependent paths are not tested.
"""
import math
import sqlite3
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db
import scripts.memory_curator as mc


def _conn(db_path=":memory:"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    setup_db.create_latest_schema(conn)
    return conn


def _insert_goal(conn, description="test goal", status="pending", updated_at=None):
    updated = updated_at or "2020-01-01 00:00:00"
    c = conn.execute(
        "INSERT INTO goals (description, status, updated_at, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        (description, status, updated),
    )
    conn.commit()
    return c.lastrowid


def _insert_question(conn, question="test question?", status="open", created_at=None):
    created = created_at or "2020-01-01 00:00:00"
    c = conn.execute(
        "INSERT INTO questions (question, status, created_at) VALUES (?, ?, ?)",
        (question, status, created),
    )
    conn.commit()
    return c.lastrowid


# ---------------------------------------------------------------------------
# cosine()
# ---------------------------------------------------------------------------

class TestCosine:
    def test_identical_vectors_return_1(self):
        v = [1.0, 2.0, 3.0]
        result = mc.cosine(v, v)
        assert abs(result - 1.0) < 1e-9

    def test_orthogonal_vectors_return_0(self):
        assert abs(mc.cosine([1, 0, 0], [0, 1, 0])) < 1e-9

    def test_opposite_vectors_return_minus_1(self):
        result = mc.cosine([1.0, 0.0], [-1.0, 0.0])
        assert abs(result - (-1.0)) < 1e-9

    def test_zero_vector_returns_0(self):
        assert mc.cosine([0, 0, 0], [1, 2, 3]) == 0.0

    def test_known_value(self):
        a = [1, 0]
        b = [1, 1]
        expected = 1 / math.sqrt(2)
        assert abs(mc.cosine(a, b) - expected) < 1e-9

    def test_result_bounded_between_minus1_and_1(self):
        import random
        random.seed(42)
        for _ in range(20):
            a = [random.uniform(-1, 1) for _ in range(10)]
            b = [random.uniform(-1, 1) for _ in range(10)]
            result = mc.cosine(a, b)
            assert -1.0 - 1e-9 <= result <= 1.0 + 1e-9


# ---------------------------------------------------------------------------
# run_stale_goals()
# ---------------------------------------------------------------------------

class TestRunStaleGoals:
    def test_returns_empty_when_no_goals(self):
        conn = _conn()
        result = mc.run_stale_goals(conn, stale_days=30, recent_sessions=5)
        assert result["stale_goals"] == []

    def test_flags_old_pending_goal(self):
        conn = _conn()
        _insert_goal(conn, description="finish the widget", status="pending",
                     updated_at="2020-01-01 00:00:00")
        result = mc.run_stale_goals(conn, stale_days=30, recent_sessions=5)
        assert len(result["stale_goals"]) == 1
        assert result["stale_goals"][0]["description"] == "finish the widget"

    def test_excludes_active_goal(self):
        conn = _conn()
        _insert_goal(conn, description="active goal", status="active",
                     updated_at="2020-01-01 00:00:00")
        result = mc.run_stale_goals(conn, stale_days=30, recent_sessions=5)
        assert result["stale_goals"] == []

    def test_excludes_completed_goal(self):
        conn = _conn()
        _insert_goal(conn, description="done goal", status="completed",
                     updated_at="2020-01-01 00:00:00")
        result = mc.run_stale_goals(conn, stale_days=30, recent_sessions=5)
        assert result["stale_goals"] == []

    def test_result_has_expected_keys(self):
        conn = _conn()
        result = mc.run_stale_goals(conn, stale_days=30, recent_sessions=5)
        assert "stale_cutoff_days" in result
        assert "stale_goals" in result
        assert result["stale_cutoff_days"] == 30

    def test_no_exception_on_empty_db(self):
        conn = _conn()
        try:
            mc.run_stale_goals(conn, stale_days=7, recent_sessions=3)
        except Exception as e:
            pytest.fail(f"run_stale_goals raised unexpectedly: {e}")


# ---------------------------------------------------------------------------
# run_question_audit()
# ---------------------------------------------------------------------------

class TestRunQuestionAudit:
    def test_returns_empty_when_no_questions(self):
        conn = _conn()
        result = mc.run_question_audit(conn, stale_days=30, recent_sessions=5)
        assert result["stale_questions"] == []

    def test_flags_old_open_question(self):
        conn = _conn()
        _insert_question(conn, question="what is the plan?", status="open",
                         created_at="2020-01-01 00:00:00")
        result = mc.run_question_audit(conn, stale_days=30, recent_sessions=5)
        assert len(result["stale_questions"]) == 1

    def test_excludes_closed_question(self):
        conn = _conn()
        _insert_question(conn, question="closed question?", status="closed",
                         created_at="2020-01-01 00:00:00")
        result = mc.run_question_audit(conn, stale_days=30, recent_sessions=5)
        assert result["stale_questions"] == []

    def test_result_has_expected_keys(self):
        conn = _conn()
        result = mc.run_question_audit(conn, stale_days=14, recent_sessions=3)
        assert "stale_cutoff_days" in result
        assert "stale_questions" in result
        assert result["stale_cutoff_days"] == 14

    def test_no_exception_on_empty_db(self):
        conn = _conn()
        try:
            mc.run_question_audit(conn, stale_days=7, recent_sessions=3)
        except Exception as e:
            pytest.fail(f"run_question_audit raised unexpectedly: {e}")
