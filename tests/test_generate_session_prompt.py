"""
Tests for generate_session_prompt.py — gather_state(), render(), and empty DB handling.
"""
import sqlite3
import sys
import pytest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db
import scripts.generate_session_prompt as gsp


def _conn(db_path=":memory:"):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    setup_db.create_latest_schema(conn)
    return conn


def _insert_belief(conn, position="test belief", is_active=1, confidence_score=0.8):
    c = conn.execute(
        "INSERT INTO beliefs (position, is_active, confidence_score, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'supported', datetime('now'), datetime('now'))",
        (position, is_active, confidence_score),
    )
    conn.commit()
    return c.lastrowid


def _insert_goal(conn, description="test goal", status="pending", priority="near-term"):
    c = conn.execute(
        "INSERT INTO goals (description, status, priority, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (description, status, priority),
    )
    conn.commit()
    return c.lastrowid


# ---------------------------------------------------------------------------
# gather_state() schema contract
# ---------------------------------------------------------------------------

class TestGatherState:
    def test_returns_dict(self):
        conn = _conn()
        state = gsp.gather_state(conn, date.today())
        assert isinstance(state, dict)

    def test_has_all_expected_keys(self):
        conn = _conn()
        state = gsp.gather_state(conn, date.today())
        for key in [
            "n_chunks", "n_beliefs", "n_questions", "n_goals_pending",
            "n_sessions", "last_session_date", "immediate_goals",
            "near_term_goals", "interesting_scout", "session_date",
        ]:
            assert key in state, f"Missing key '{key}' in gather_state() result"

    def test_counts_are_zero_on_empty_db(self):
        conn = _conn()
        state = gsp.gather_state(conn, date.today())
        assert state["n_beliefs"] == 0
        assert state["n_questions"] == 0
        assert state["n_goals_pending"] == 0
        assert state["n_sessions"] == 0

    def test_counts_active_beliefs(self):
        conn = _conn()
        _insert_belief(conn, is_active=1)
        _insert_belief(conn, is_active=0)  # should not count
        state = gsp.gather_state(conn, date.today())
        assert state["n_beliefs"] == 1

    def test_immediate_goals_list(self):
        conn = _conn()
        _insert_goal(conn, description="urgent thing", priority="immediate")
        _insert_goal(conn, description="later thing", priority="near-term")
        state = gsp.gather_state(conn, date.today())
        descriptions = [g["description"] for g in state["immediate_goals"]]
        assert "urgent thing" in descriptions
        assert "later thing" not in descriptions

    def test_no_exception_on_empty_db(self):
        conn = _conn()
        try:
            gsp.gather_state(conn, date.today())
        except Exception as e:
            pytest.fail(f"gather_state raised unexpectedly on empty DB: {e}")

    def test_session_date_matches_input(self):
        conn = _conn()
        today = date.today()
        state = gsp.gather_state(conn, today)
        assert state["session_date"] == today


# ---------------------------------------------------------------------------
# render() output quality
# ---------------------------------------------------------------------------

class TestRender:
    def _state(self):
        conn = _conn()
        return gsp.gather_state(conn, date.today())

    def test_returns_string(self):
        output = gsp.render(self._state(), generated_at=datetime.now())
        assert isinstance(output, str)

    def test_output_is_non_empty(self):
        output = gsp.render(self._state(), generated_at=datetime.now())
        assert len(output) > 100

    def test_output_contains_session_starter_header(self):
        output = gsp.render(self._state(), generated_at=datetime.now())
        assert "Session Starter" in output or "Session" in output

    def test_output_contains_ember_context_reference(self):
        """The rendered prompt must tell Claude to read ember_engine_context.md."""
        output = gsp.render(self._state(), generated_at=datetime.now())
        assert "ember_engine_context" in output

    def test_render_with_belief_data(self):
        conn = _conn()
        _insert_belief(conn, position="I work best with constraints")
        state = gsp.gather_state(conn, date.today())
        output = gsp.render(state, generated_at=datetime.now())
        assert isinstance(output, str)
        assert len(output) > 50

    def test_render_with_goals(self):
        conn = _conn()
        _insert_goal(conn, description="finish test suite", priority="immediate")
        state = gsp.gather_state(conn, date.today())
        output = gsp.render(state, generated_at=datetime.now())
        assert isinstance(output, str)
