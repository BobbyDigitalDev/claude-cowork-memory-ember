"""
Tests for refresh_recent_memory.py — schema contracts, fetch functions, version string.
"""
import sqlite3
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db


def _conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    setup_db.create_latest_schema(conn)
    return conn


def _import_rrm(db_path):
    import importlib
    import scripts.refresh_recent_memory as rrm
    importlib.reload(rrm)
    rrm.DB_PATH = str(db_path)
    return rrm


def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def _insert_belief(conn, position="test belief", confidence=0.8, status="supported", is_active=1):
    c = conn.execute(
        "INSERT INTO beliefs (position, confidence_score, status, is_active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (position, confidence, status, is_active),
    )
    conn.commit()
    return c.lastrowid


def _insert_goal(conn, description="test goal", status="active"):
    conn.execute(
        "INSERT INTO goals (description, status, created_at, updated_at) VALUES (?, ?, datetime('now'), datetime('now'))",
        (description, status),
    )
    conn.commit()


def _insert_question(conn, question="what is X?", status="open"):
    conn.execute(
        "INSERT INTO questions (question, status, created_at) VALUES (?, ?, datetime('now'))",
        (question, status),
    )
    conn.commit()


def _insert_pattern(conn, description="test pattern", frequency=5):
    conn.execute(
        "INSERT INTO patterns (description, frequency, created_at) VALUES (?, ?, datetime('now'))",
        (description, frequency),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# fetch_beliefs schema contract
# ---------------------------------------------------------------------------

class TestFetchBeliefs:
    def test_returns_list(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        result = rrm.fetch_beliefs(conn)
        assert isinstance(result, list)

    def test_returns_active_belief(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_belief(conn, position="sleep matters", confidence=0.85)
        rows = _rows_to_dicts(rrm.fetch_beliefs(conn))
        assert any(r.get("position") == "sleep matters" for r in rows)

    def test_excludes_inactive_belief(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_belief(conn, position="deprecated belief", is_active=0)
        rows = _rows_to_dicts(rrm.fetch_beliefs(conn))
        assert not any(r.get("position") == "deprecated belief" for r in rows)

    def test_no_exception_on_empty_table(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        assert rrm.fetch_beliefs(conn) == []


# ---------------------------------------------------------------------------
# fetch_goals schema contract
# ---------------------------------------------------------------------------

class TestFetchGoals:
    def test_returns_active_goal(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_goal(conn, description="build ember", status="active")
        rows = _rows_to_dicts(rrm.fetch_goals(conn))
        assert any(r.get("description") == "build ember" for r in rows)

    def test_excludes_completed_goal(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_goal(conn, description="done goal", status="completed")
        rows = _rows_to_dicts(rrm.fetch_goals(conn))
        assert not any(r.get("description") == "done goal" for r in rows)


# ---------------------------------------------------------------------------
# fetch_questions schema contract
# ---------------------------------------------------------------------------

class TestFetchQuestions:
    def test_returns_open_question(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_question(conn, question="what drives me?", status="open")
        rows = _rows_to_dicts(rrm.fetch_questions(conn))
        assert any(r.get("question") == "what drives me?" for r in rows)

    def test_no_exception_on_empty_table(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        assert isinstance(rrm.fetch_questions(conn), list)


# ---------------------------------------------------------------------------
# fetch_reflections — safe with empty table
# ---------------------------------------------------------------------------

class TestFetchReflections:
    def test_returns_list_when_table_empty(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        assert isinstance(rrm.fetch_reflections(conn), list)

    def test_no_exception_on_empty_reflections(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        try:
            rrm.fetch_reflections(conn)
        except Exception as e:
            pytest.fail(f"fetch_reflections raised unexpectedly: {e}")


# ---------------------------------------------------------------------------
# fetch_patterns schema contract
# ---------------------------------------------------------------------------

class TestFetchPatterns:
    def test_returns_pattern(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        _insert_pattern(conn, description="always late on tasks", frequency=7)
        rows = _rows_to_dicts(rrm.fetch_patterns(conn))
        assert any(r.get("description") == "always late on tasks" for r in rows)


# ---------------------------------------------------------------------------
# Schema version string regression guard (ISSUE-004)
# ---------------------------------------------------------------------------

class TestSchemaVersionString:
    def test_snapshot_content_contains_v2_8_0(self, tmp_path):
        """build_snapshot_content() must embed Schema: v2.8.0, not v2.2."""
        conn = _conn(tmp_path / "test.db")
        rrm = _import_rrm(tmp_path / "test.db")
        content = rrm.build_snapshot_content(conn)
        assert "v2.8.0" in content, "Schema version in snapshot must be v2.8.0"
        assert "v2.2" not in content, "Stale v2.2 must not appear in snapshot content"
