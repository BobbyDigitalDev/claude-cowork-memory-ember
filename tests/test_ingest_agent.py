"""
Tests for ingest_agent.py — fetch_queued schema contract, exit code ordering
(queue-before-Ollama fix from ISSUE-006), and run() logic via mocks.
"""
import argparse
import sqlite3
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db
import scripts.ingest_agent as ingest_agent


def _conn(db_path):
    conn = sqlite3.connect(str(db_path))
    setup_db.create_latest_schema(conn)
    return conn


def _insert_scout_result(conn, title="Test Paper", status="interesting",
                          relevance_score=0.9, abstract="Abstract text."):
    c = conn.execute(
        "INSERT INTO scout_results (title, abstract, source_url, source_name, "
        "relevance_score, status, date_fetched) "
        "VALUES (?, ?, 'http://example.com', 'test', ?, ?, datetime('now'))",
        (title, abstract, relevance_score, status),
    )
    conn.commit()
    return c.lastrowid


def _args(dry_run=True, no_jitter=True, quiet=True, db=None):
    ns = argparse.Namespace(
        dry_run=dry_run, no_jitter=no_jitter, quiet=quiet, db=db
    )
    return ns


# ---------------------------------------------------------------------------
# fetch_queued schema contract
# ---------------------------------------------------------------------------

class TestFetchQueued:
    def test_returns_empty_list_when_no_interesting_results(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        result = ingest_agent.fetch_queued(conn)
        assert result == []

    def test_returns_interesting_rows(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        _insert_scout_result(conn, title="Interesting Paper", status="interesting")
        result = ingest_agent.fetch_queued(conn)
        assert len(result) == 1
        assert result[0]["title"] == "Interesting Paper"

    def test_excludes_non_interesting_rows(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        _insert_scout_result(conn, title="Pending Paper", status="pending")
        _insert_scout_result(conn, title="Ingested Paper", status="ingested")
        result = ingest_agent.fetch_queued(conn)
        assert result == []

    def test_result_has_expected_keys(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        _insert_scout_result(conn)
        result = ingest_agent.fetch_queued(conn)
        assert len(result) == 1
        row = result[0]
        for key in ["id", "title", "abstract", "source_url", "relevance_score"]:
            assert key in row, f"Missing key '{key}' in fetch_queued result"

    def test_ordered_by_relevance_desc(self, tmp_path):
        conn = _conn(tmp_path / "test.db")
        _insert_scout_result(conn, title="Low", relevance_score=0.3)
        _insert_scout_result(conn, title="High", relevance_score=0.95)
        result = ingest_agent.fetch_queued(conn)
        assert result[0]["title"] == "High"
        assert result[1]["title"] == "Low"


# ---------------------------------------------------------------------------
# run() exit code: queue-before-Ollama (ISSUE-006 regression)
# ---------------------------------------------------------------------------

class TestRunExitCodes:
    def test_returns_3_when_queue_empty_regardless_of_ollama(self, tmp_path):
        """ISSUE-006: empty queue must exit 3, not 1, even if Ollama is down."""
        db_path = tmp_path / "test.db"
        conn = _conn(db_path)
        conn.close()

        args = _args(dry_run=False, db=str(db_path))

        # Ollama is down — but queue is empty, so should still get exit 3
        with patch.object(ingest_agent, "ollama_is_running", return_value=False):
            result = ingest_agent.run(args)

        assert result == 3, (
            f"Expected exit code 3 (empty queue), got {result}. "
            "ISSUE-006 regression: queue check must happen before Ollama check."
        )

    def test_returns_3_when_no_interesting_items(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = _conn(db_path)
        _insert_scout_result(conn, status="pending")  # not interesting
        conn.close()

        args = _args(dry_run=False, db=str(db_path))
        with patch.object(ingest_agent, "ollama_is_running", return_value=False):
            result = ingest_agent.run(args)

        assert result == 3

    def test_returns_1_when_queue_has_items_and_ollama_down(self, tmp_path):
        """If there ARE items to process but Ollama is down, exit 1."""
        db_path = tmp_path / "test.db"
        conn = _conn(db_path)
        _insert_scout_result(conn, status="interesting")
        conn.close()

        args = _args(dry_run=False, db=str(db_path))
        with patch.object(ingest_agent, "ollama_is_running", return_value=False):
            result = ingest_agent.run(args)

        assert result == 1

    def test_dry_run_skips_ollama_check_and_processes(self, tmp_path):
        """dry_run=True skips the Ollama gate and runs without actually calling scripts."""
        db_path = tmp_path / "test.db"
        conn = _conn(db_path)
        _insert_scout_result(conn, status="interesting")
        conn.close()

        args = _args(dry_run=True, db=str(db_path))
        # ingest_one is called in dry_run mode — patch it to avoid subprocess
        with patch.object(ingest_agent, "ingest_one", return_value=True), \
             patch.object(ingest_agent, "run_embed"):
            result = ingest_agent.run(args)

        assert result == 0
