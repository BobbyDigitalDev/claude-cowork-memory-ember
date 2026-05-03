"""
Shared fixtures for the cowork-memory test suite.
"""
import pytest
import sqlite3
import os
import sys
import tempfile

# Make scripts importable
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS_DIR))


@pytest.fixture
def tmp_db(tmp_path):
    """
    Spin up a temporary SQLite database with the full schema.
    Uses setup_db.py's create_schema() so the test DB always
    matches production.
    """
    import setup_db
    db_path = str(tmp_path / "test_memory.db")
    original = setup_db.DB_PATH
    setup_db.DB_PATH = db_path
    setup_db.setup_database()
    setup_db.DB_PATH = original
    return db_path


@pytest.fixture
def minimal_extraction():
    """
    A realistic extraction dict with all fields that write_to_db expects,
    including edge-case values (list tags, empty strings, None).
    """
    return {
        "summary": {
            "title": "Test session",
            "summary": "A test conversation.",
            "key_themes": ["testing", "memory"],
            "session_type": "project",
            "bobby_mood": "focused",
            "claude_mood": "engaged",
        },
        "beliefs": [
            {
                "topic": "test belief",
                "position": "Tests catch bugs before users do.",
                "confidence": "high",
                "confidence_score": 0.9,
                "evidence_snippets": ["we should write tests", "this bug would have been caught"],
                "source_type": "direct_message",
                "tags": ["testing", "quality"],   # list — the bug we fixed
            }
        ],
        "epiphanies": [
            {
                "description": "Tests are living documentation.",
                "significance": "high",
                "confidence_score": 0.8,
                "evidence_snippets": ["tests tell future Claude what behavior must be preserved"],
                "source_type": "model_inference",
                "tags": "testing",   # string — both forms should work
            }
        ],
        "questions": [],
        "goals": [
            {
                "description": "Write tests for every new script.",
                "priority": "immediate",
                "status": "pending",
                "tags": ["testing", "quality"],
            }
        ],
        "entities": [],
        "concepts": [
            {
                "name": "Test-Driven Memory",
                "description": "The practice of writing tests alongside memory pipeline scripts.",
                "tags": None,   # None — should produce empty string
            }
        ],
        "mood": {
            "bobby_state": "energized",
            "claude_state": "engaged",
            "session_quality": "high",
            "tags": [],   # empty list — should produce empty string
        },
        "gratitude": [],
        "patterns": [
            {
                "name": "List Coercion",
                "description": "Qwen sometimes returns list where string expected.",
                "pattern_type": "technical_pattern",
                "first_observed": "2026-04-25",
                "recurrence": "occasional",
                "significance": "caught by tests",
                "supporting_evidence": "tags-as-list bug",
                "importance_score": 0.7,
                "tags": ["qwen", "edge-cases"],   # list
            }
        ],
    }
