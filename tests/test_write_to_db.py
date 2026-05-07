"""
Tests for write_to_db() in process_conversation.py.

These are integration-style tests that write to a real (temp) SQLite DB
so we catch type errors, schema mismatches, and constraint violations
before they reach production.
"""
import sys, os, sqlite3
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import pytest
import process_conversation as pc


@pytest.fixture(autouse=True)
def patch_db(tmp_path):
    """Redirect all DB writes to a temp file for every test in this module."""
    db_path = str(tmp_path / "test_memory.db")
    # Bootstrap schema using setup_memory
    import setup_db
    conn = sqlite3.connect(db_path)

    setup_db.create_latest_schema(conn)

    conn.close()
    # Patch process_conversation to use the same temp DB
    orig_pc = pc.DB_PATH
    pc.DB_PATH = db_path
    yield db_path
    pc.DB_PATH = orig_pc


def _run_write(db_path, extractions):
    """Helper: call write_to_db with fixed session/conv IDs."""
    pc.write_to_db(
        session_id=1,
        conv_id=1,
        extractions=extractions,
        prompts={},
        conv_text="test conversation text",
        filename="test_2026_01_01_001.md",
        session_date="2026-01-01",
    )
    return sqlite3.connect(db_path)


class TestTagsListCoercion:
    """
    The bug that triggered this test suite: Qwen returns tags as a list
    and SQLite's Python driver rejects it with ProgrammingError.
    Every table with a tags column must handle list input gracefully.
    """

    def _base_extraction(self):
        return {
            "summary": {"title": "t", "summary": "s", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [], "epiphanies": [], "questions": [], "goals": [],
            "entities": [], "concepts": [], "mood": {}, "gratitude": [], "patterns": [],
        }

    def test_belief_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["beliefs"] = [{
            "topic": "t", "position": "p", "confidence": "high",
            "confidence_score": 0.8, "evidence_snippets": [],
            "source_type": "direct_message", "tags": ["a", "b"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM beliefs").fetchone()
        assert row is not None
        assert "a" in row[0] and "b" in row[0]

    def test_epiphany_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["epiphanies"] = [{
            "description": "d", "significance": "high", "confidence_score": 0.8,
            "evidence_snippets": [], "source_type": "model_inference",
            "tags": ["insight", "memory"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM epiphanies").fetchone()
        assert row is not None

    def test_goal_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["goals"] = [{
            "description": "do something", "priority": "immediate",
            "status": "pending", "tags": ["mvp", "testing"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM goals").fetchone()
        assert row is not None

    def test_pattern_tags_as_list(self, patch_db):
        ex = self._base_extraction()
        ex["patterns"] = [{
            "name": "n", "description": "d", "pattern_type": "thinking_pattern",
            "first_observed": "2026-01-01", "recurrence": "once",
            "significance": "s", "supporting_evidence": "e",
            "importance_score": 0.5, "tags": ["tag1", "tag2"],
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM patterns").fetchone()
        assert row is not None

    def test_concept_tags_as_none(self, patch_db):
        """None tags should produce an empty string, not NULL error."""
        ex = self._base_extraction()
        ex["concepts"] = [{
            "name": "n", "description": "d", "tags": None,
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM concepts").fetchone()
        assert row is not None
        assert row[0] == ""

    def test_evidence_snippets_as_list_serialised(self, patch_db):
        """evidence_snippets must be JSON-serialised when stored."""
        ex = self._base_extraction()
        ex["beliefs"] = [{
            "topic": "t", "position": "p", "confidence": "high",
            "confidence_score": 0.8,
            "evidence_snippets": ["quote one", "quote two"],
            "source_type": "direct_message", "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        import json
        row = conn.execute("SELECT evidence_snippets FROM beliefs").fetchone()
        assert row is not None
        parsed = json.loads(row[0])
        assert parsed == ["quote one", "quote two"]


class TestMalformedExtractionHandling:
    """write_to_db should skip malformed items without crashing."""

    def _base(self):
        return {
            "summary": {"title": "", "summary": "", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [], "epiphanies": [], "questions": [], "goals": [],
            "entities": [], "concepts": [], "mood": {}, "gratitude": [], "patterns": [],
        }

    def test_belief_as_string_skipped(self, patch_db):
        """If Qwen returns a string instead of a dict, skip it without crashing."""
        ex = self._base()
        ex["beliefs"] = ["this is not a dict"]
        _run_write(patch_db, ex)   # should not raise
        conn = sqlite3.connect(patch_db)
        count = conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
        assert count == 0

    def test_empty_extractions_no_crash(self, patch_db):
        """Completely empty extraction should not crash."""
        ex = self._base()
        _run_write(patch_db, ex)   # should not raise


class TestQuarantineWrite:
    """Beliefs failing grounding checks must land in needs_review, not proposed."""

    def _base(self):
        return {
            "summary": {"title": "", "summary": "", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [], "epiphanies": [], "questions": [], "goals": [],
            "entities": [], "concepts": [], "mood": {}, "gratitude": [], "patterns": [],
        }

    def test_low_confidence_quarantined(self, patch_db):
        """Belief with confidence_score below threshold must be needs_review."""
        ex = self._base()
        ex["beliefs"] = [{
            "topic": "low conf",
            "position": "This is barely supported.",
            "confidence": "low",
            "confidence_score": 0.25,   # below QUARANTINE_MIN_CONFIDENCE (0.40)
            "evidence_snippets": ["some text"],
            "source_type": "model_inference",
            "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute(
            "SELECT status, quarantine_reason FROM beliefs"
        ).fetchone()
        assert row is not None
        assert row[0] == "needs_review", f"Expected needs_review, got {row[0]}"
        assert "low confidence" in (row[1] or ""), f"Missing reason: {row[1]}"

    def test_no_anchor_quarantined(self, patch_db):
        """Belief with no verbatim anchor must be needs_review."""
        ex = self._base()
        ex["beliefs"] = [{
            "topic": "no anchor",
            "position": "Something was said somewhere.",
            "confidence": "medium",
            "confidence_score": 0.65,
            # evidence_snippets has no text matching conv_text → no anchor produced
            "evidence_snippets": ["xyz totally nonexistent text abcdef"],
            "source_type": "model_inference",
            "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute(
            "SELECT status, quarantine_reason FROM beliefs"
        ).fetchone()
        assert row is not None
        assert row[0] == "needs_review", f"Expected needs_review, got {row[0]}"
        assert "verbatim anchor" in (row[1] or ""), f"Missing reason: {row[1]}"

    def test_well_grounded_belief_proposed(self, patch_db):
        """A belief that passes all checks must land in proposed, not needs_review."""
        ex = self._base()
        # conv_text in _run_write is "test conversation text"
        # Use a snippet that actually appears in it so verbatim anchor fires
        ex["beliefs"] = [{
            "topic": "solid belief",
            "position": "Tests catch bugs before users do.",
            "confidence": "high",
            "confidence_score": 0.85,
            "evidence_snippets": ["test conversation text"],
            "source_type": "direct_message",
            "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute(
            "SELECT status, quarantine_reason FROM beliefs"
        ).fetchone()
        assert row is not None
        assert row[0] == "proposed", f"Expected proposed, got {row[0]}"
        assert row[1] is None, f"Unexpected quarantine_reason: {row[1]}"

    def test_quarantine_reason_stored(self, patch_db):
        """quarantine_reason column must be populated with specific cause."""
        ex = self._base()
        ex["beliefs"] = [{
            "topic": "no evidence",
            "position": "Made up with no backing.",
            "confidence": "low",
            "confidence_score": 0.20,
            "evidence_snippets": [],
            "source_type": "model_inference",
            "tags": "",
        }]
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT quarantine_reason FROM beliefs").fetchone()
        assert row is not None and row[0] is not None
        # Should mention at least one specific reason
        reason = row[0]
        assert any(k in reason for k in ("confidence", "anchor", "evidence")), \
            f"Reason too vague: {reason}"


class TestReadPaths:
    """Verify that data written via write_to_db is correctly readable back."""

    def _base_with_belief(self, score=0.85, snippets=None):
        if snippets is None:
            snippets = ["test conversation text"]
        return {
            "summary": {"title": "Read test", "summary": "A readable session.",
                        "key_themes": ["memory"], "session_type": "project",
                        "bobby_mood": "focused", "claude_mood": "engaged"},
            "beliefs": [{
                "topic": "read path",
                "position": "Written data should be readable.",
                "confidence": "high",
                "confidence_score": score,
                "evidence_snippets": snippets,
                "source_type": "direct_message",
                "tags": ["read", "test"],
            }],
            "epiphanies": [],
            "questions": [{
                "question": "Does the read path work?",
                "category": "technical",
                "status": "open",
                "tags": [],
            }],
            "goals": [],
            "entities": [],
            "concepts": [],
            "mood": {},
            "gratitude": [],
            "patterns": [],
        }

    def test_belief_round_trip(self, patch_db):
        """Write a belief, read it back, verify field fidelity."""
        ex = self._base_with_belief()
        conn = _run_write(patch_db, ex)
        row = conn.execute(
            "SELECT topic, position, confidence_score, status FROM beliefs"
        ).fetchone()
        assert row is not None
        assert row[0] == "read path"
        assert "readable" in row[1]
        assert abs(row[2] - 0.85) < 0.01
        assert row[3] == "proposed"

    def test_question_round_trip(self, patch_db):
        """Write a question, read it back."""
        ex = self._base_with_belief()
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT question, status FROM questions").fetchone()
        assert row is not None
        assert "read path" in row[0]
        assert row[1] == "open"

    def test_tags_readable_as_string(self, patch_db):
        """Tags written as a list should be readable as a non-empty string."""
        ex = self._base_with_belief()
        conn = _run_write(patch_db, ex)
        row = conn.execute("SELECT tags FROM beliefs").fetchone()
        assert row is not None
        tags_str = row[0] or ""
        assert len(tags_str) > 0
        assert "read" in tags_str or "test" in tags_str

    def test_conversation_written(self, patch_db):
        """write_to_db must create a conversations row."""
        ex = self._base_with_belief()
        _run_write(patch_db, ex)
        conn = sqlite3.connect(patch_db)
        count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        assert count >= 1

    def test_provenance_written(self, patch_db):
        """Each extracted belief should have a memory_provenance entry."""
        ex = self._base_with_belief()
        _run_write(patch_db, ex)
        conn = sqlite3.connect(patch_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_provenance WHERE memory_type='belief'"
        ).fetchone()[0]
        assert count >= 1


class TestProvenanceCompleteness:
    """Provenance must be written for every memory type extracted from conversations."""

    def _extraction_with_all_types(self):
        return {
            "summary": {"title": "t", "summary": "s", "key_themes": [],
                        "session_type": "project", "bobby_mood": "", "claude_mood": ""},
            "beliefs": [{
                "topic": "prov belief",
                "position": "Provenance should be complete.",
                "confidence": "high",
                "confidence_score": 0.85,
                "evidence_snippets": ["test conversation text"],
                "source_type": "direct_message",
                "tags": "",
            }],
            "epiphanies": [{
                "description": "Provenance reveals extraction lineage.",
                "preceded_by": "reviewing the write path",
                "implications": "audit trail for every memory",
                "confidence_score": 0.8,
                "evidence_snippets": ["test conversation text"],
                "source_type": "model_inference",
                "tags": "",
            }],
            "questions": [{
                "question": "Is provenance written for questions?",
                "category": "technical",
                "status": "open",
                "tags": "",
            }],
            "goals": [{
                "description": "Verify provenance completeness",
                "category": "technical",
                "status": "pending",
                "priority": "near-term",
                "tags": "",
            }],
            "entities": [{
                "name": "ProvEntity",
                "type": "concept-anchor",
                "description": "Entity used in provenance test.",
                "relationship": "test subject",
                "importance": "medium",
                "tags": "",
            }],
            "concepts": [{
                "name": "ProvConcept",
                "description": "Concept used in provenance test.",
                "evolution_notes": "",
                "tags": "",
            }],
            "patterns": [{
                "name": "ProvPattern",
                "description": "Pattern used in provenance test.",
                "pattern_type": "operational_lesson",
                "first_observed": "during test",
                "recurrence": "once",
                "significance": "test completeness",
                "supporting_evidence": "test evidence",
                "importance_score": 0.6,
                "tags": "",
            }],
            "mood": {},
            "gratitude": [],
            "boundaries": [],
        }

    def _prov_count(self, db_path, memory_type):
        conn = sqlite3.connect(db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM memory_provenance WHERE memory_type=?",
            (memory_type,)
        ).fetchone()[0]
        conn.close()
        return n

    def test_belief_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "belief") >= 1

    def test_epiphany_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "epiphany") >= 1

    def test_question_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "question") >= 1

    def test_goal_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "goal") >= 1

    def test_entity_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "entity") >= 1

    def test_concept_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "concept") >= 1

    def test_pattern_provenance(self, patch_db):
        _run_write(patch_db, self._extraction_with_all_types())
        assert self._prov_count(patch_db, "pattern") >= 1

    def test_all_types_in_one_run(self, patch_db):
        """Single write_to_db call must produce provenance rows for all 7 types."""
        _run_write(patch_db, self._extraction_with_all_types())
        conn = sqlite3.connect(patch_db)
        types_with_provenance = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT memory_type FROM memory_provenance"
            ).fetchall()
        }
        conn.close()
        expected = {"belief", "epiphany", "question", "goal", "entity", "concept", "pattern"}
        assert expected <= types_with_provenance, \
            f"Missing provenance types: {expected - types_with_provenance}"
