"""
Tests for retrieve.py — the three-strategy retrieval engine.
Semantic strategy requires Ollama so those tests are skipped.
Structural and temporal strategies are pure SQLite and always run.
"""
import sys, os, sqlite3
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import pytest
import retrieve


@pytest.fixture
def seeded_db(tmp_path):
    """Temp DB with full schema and known seed data for deterministic tests."""
    import setup_db
    db_path = str(tmp_path / "test_memory.db")
    conn = sqlite3.connect(db_path)

    setup_db.create_latest_schema(conn)

    conn.close()

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    now = "2026-01-01 00:00:00"

    c.execute("""
        INSERT INTO beliefs (topic, position, confidence, confidence_score,
                             status, source_type, importance_score, created_at, updated_at,
                             last_updated, valid_from, version, origin)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("substrate independence", "Consciousness may not require biological substrate.",
          "high", 0.9, "verified", "direct_message", 0.9,
          now, now, now, now, 1, "conversation"))

    c.execute("""
        INSERT INTO goals (description, priority, status, created_at)
        VALUES (?, ?, ?, ?)
    """, ("Build session intent declaration feature", "immediate", "pending", now))

    c.execute("""
        INSERT INTO questions (question, status, created_at)
        VALUES (?, ?, ?)
    """, ("How does semantic retrieval handle new topics?", "open", now))

    conn.commit()
    conn.close()
    return db_path


class TestStructuralSearch:

    def test_finds_belief_by_keyword(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._structural_search("substrate consciousness", top=5, conn=conn)
        types = [r["source_type"] for r in results]
        assert "belief" in types

    def test_finds_goal_by_keyword(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._structural_search("session intent", top=5, conn=conn)
        types = [r["source_type"] for r in results]
        assert "goal" in types

    def test_no_results_for_unrelated_query(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._structural_search("xyzzy frobnicator", top=5, conn=conn)
        assert results == []

    def test_returns_at_most_top_n(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._structural_search("the", top=1, conn=conn)
        assert len(results) <= 1

    def test_scores_are_between_zero_and_one(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._structural_search("substrate", top=5, conn=conn)
        for r in results:
            assert 0.0 <= r["score"] <= 1.0


class TestTemporalSearch:

    def test_returns_pending_goals(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._temporal_search(days=9999, top=10, conn=conn)
        types = [r["source_type"] for r in results]
        assert "goal" in types

    def test_returns_open_questions(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._temporal_search(days=9999, top=10, conn=conn)
        types = [r["source_type"] for r in results]
        assert "question" in types

    def test_scores_are_between_zero_and_one(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._temporal_search(days=9999, top=10, conn=conn)
        for r in results:
            assert 0.0 <= r["score"] <= 1.0

    def test_zero_day_window_returns_nothing(self, seeded_db):
        conn = sqlite3.connect(seeded_db)
        results = retrieve._temporal_search(days=0, top=10, conn=conn)
        assert results == []


class TestMerge:
    """_merge takes a flat list of result dicts (not a list of lists)."""

    def test_deduplicates_same_source(self):
        results = [
            {"source_type": "belief", "source_id": 1, "score": 0.9,
             "strategy": "semantic", "content": "x", "label": "x"},
            {"source_type": "belief", "source_id": 1, "score": 0.7,
             "strategy": "structural", "content": "x", "label": "x"},
        ]
        merged = retrieve._merge(results)
        assert len(merged) == 1
        assert merged[0]["score"] == 0.9
        assert set(merged[0]["strategies"]) == {"semantic", "structural"}

    def test_different_sources_kept(self):
        results = [
            {"source_type": "belief", "source_id": 1, "score": 0.9,
             "strategy": "semantic", "content": "x", "label": "x"},
            {"source_type": "goal",   "source_id": 1, "score": 0.7,
             "strategy": "structural", "content": "y", "label": "y"},
        ]
        merged = retrieve._merge(results)
        assert len(merged) == 2

    def test_empty_list_produces_empty_merge(self):
        assert retrieve._merge([]) == []
