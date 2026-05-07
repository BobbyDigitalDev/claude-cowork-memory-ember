"""
Tests for process_research.py — external content extraction pipeline.

Contract:
- Extracts concepts, beliefs, entities, patterns, questions, epiphanies from
  research files (YouTube transcripts, articles, etc.)
- Writes to DB with memory_origin="research" so content is distinguishable
  from conversation-derived memory
- Deduplicates: won't reprocess the same file unless --force is passed
- Strips the YouTube header block before passing content to Qwen
- Gracefully handles Qwen returning malformed or empty JSON
"""
import sys, os, sqlite3
from unittest.mock import patch
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import setup_db
import process_research as pr


# ── Fixtures ───────────────────��──────────────────────────────────────────────

YOUTUBE_HEADER = """# YouTube Transcript

**Title:** OpenClaw 4.24: New AI Voice + Browser Updates

**Video ID:** 4nqtyCSS7Fg
**URL:** https://www.youtube.com/watch?v=4nqtyCSS7Fg
**Fetched:** 2026-04-25 21:45
**Words:** 2,342
**Format:** clean text

---

"""

SAMPLE_CONTENT = """OpenClaw now supports voice input through a new browser extension.
The extension hooks into the browser's Web Speech API and streams audio
directly to the agent. This removes the need to type long instructions.

The browser automation layer was also updated to handle dynamic JavaScript
rendered pages more reliably using a new wait-for-selector strategy."""

SAMPLE_FILE_CONTENT = YOUTUBE_HEADER + SAMPLE_CONTENT

MINIMAL_EXTRACTION = {
    "concepts": [
        {"name": "Voice Input Integration", "description": "OpenClaw supports voice via Web Speech API", "tags": ["voice", "browser"]}
    ],
    "beliefs": [
        {"topic": "voice_input", "position": "Voice input reduces friction for long instructions", "confidence": "medium", "tags": ["ux"]}
    ],
    "entities": [
        {"name": "OpenClaw", "type": "tool", "description": "AI agent platform", "importance": "high"}
    ],
    "patterns": [
        {"description": "Wait-for-selector strategy improves JS page reliability", "pattern_type": "technical_approach", "tags": ["browser", "automation"]}
    ],
    "questions": [
        {"question": "Does voice input work offline or require cloud STT?", "category": "technical"}
    ],
    "epiphanies": [
        {"description": "Removing typing friction could change how agents are prompted", "implications": "Shorter, more natural instructions"}
    ],
}


import pytest

@pytest.fixture()
def db(tmp_path):
    db_path = str(tmp_path / "test_research.db")
    conn = sqlite3.connect(db_path)

    setup_db.create_latest_schema(conn)

    conn.close()
    orig_pr = pr.DB_PATH
    pr.DB_PATH = db_path
    yield db_path
    pr.DB_PATH = orig_pr


# ── Content parsing ───────────────────────────────────────────────────────────

class TestStripResearchHeader:

    def test_youtube_header_stripped(self):
        result = pr.strip_research_header(SAMPLE_FILE_CONTENT)
        assert "# YouTube Transcript" not in result
        assert "**Fetched:**" not in result

    def test_content_preserved(self):
        result = pr.strip_research_header(SAMPLE_FILE_CONTENT)
        assert "Web Speech API" in result

    def test_file_with_no_header_unchanged(self):
        plain = "Just some plain text with no header block."
        result = pr.strip_research_header(plain)
        assert result.strip() == plain.strip()

    def test_returns_string(self):
        assert isinstance(pr.strip_research_header(SAMPLE_FILE_CONTENT), str)


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestAlreadyProcessed:

    def test_unprocessed_file_returns_false(self, db):
        conn = sqlite3.connect(db)
        assert pr.already_processed(conn, "2026_04_25_openclaw_424_transcript.txt") is False
        conn.close()

    def test_processed_file_returns_true(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO processing_jobs (job_type, target_type, source_file, status, model_used) "
            "VALUES ('research_extraction', 'research_file', ?, 'completed', 'qwen2.5:14b')",
            ("2026_04_25_openclaw_424_transcript.txt",)
        )
        conn.commit()
        assert pr.already_processed(conn, "2026_04_25_openclaw_424_transcript.txt") is True
        conn.close()

    def test_failed_job_not_considered_processed(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO processing_jobs (job_type, target_type, source_file, status, model_used) "
            "VALUES ('research_extraction', 'research_file', ?, 'failed', 'qwen2.5:14b')",
            ("2026_04_25_openclaw_424_transcript.txt",)
        )
        conn.commit()
        assert pr.already_processed(conn, "2026_04_25_openclaw_424_transcript.txt") is False
        conn.close()


# ── DB writes ─────────────────────────────────────────────────────────────────

class TestWriteResearchToDb:

    def test_concepts_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute("SELECT name FROM concepts WHERE tags LIKE '%voice%'").fetchall()
        assert len(rows) >= 1
        conn.close()

    def test_beliefs_written_with_research_origin(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute(
            "SELECT memory_origin FROM beliefs WHERE topic='voice_input'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "research"
        conn.close()

    def test_entities_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute("SELECT name FROM entities WHERE name='OpenClaw'").fetchall()
        assert len(rows) >= 1
        conn.close()

    def test_questions_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute("SELECT question FROM questions WHERE category='technical'").fetchall()
        assert len(rows) >= 1
        conn.close()

    def test_epiphanies_written_with_research_origin(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute(
            "SELECT memory_origin FROM epiphanies WHERE memory_origin='research'"
        ).fetchall()
        assert len(rows) >= 1
        conn.close()

    def test_empty_extraction_does_not_crash(self, db):
        conn = sqlite3.connect(db)
        empty = {k: [] for k in ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]}
        pr.write_research_to_db(conn, empty, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        conn.close()

    def test_malformed_entry_skipped_not_crashed(self, db):
        conn = sqlite3.connect(db)
        bad = {
            "concepts": ["not a dict"],
            "beliefs": [{"topic": "ok", "position": "fine", "confidence": "high", "tags": []}],
            "entities": [], "patterns": [], "questions": [], "epiphanies": [],
        }
        pr.write_research_to_db(conn, bad, "test_transcript.txt", date(2026, 4, 25))
        conn.commit()
        rows = conn.execute("SELECT topic FROM beliefs WHERE topic='ok'").fetchall()
        assert len(rows) == 1
        conn.close()


# ── Processing job tracking ───────────────────────────────────────────────────

class TestProcessingJobTracking:

    def test_completed_job_recorded(self, db):
        conn = sqlite3.connect(db)
        extraction = {k: [] for k in ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]}
        with patch("process_research.run_extractions", return_value=extraction):
            pr.process_research_file(
                "2026_04_25_test_transcript.txt",
                content=SAMPLE_CONTENT,
                conn=conn,
                source_date=date(2026, 4, 25),
            )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM processing_jobs WHERE source_file='2026_04_25_test_transcript.txt'"
        ).fetchone()
        assert row is not None
        assert row[0] == "completed"

    def test_job_started_before_writes(self, db):
        """Job row must exist with status='started' before writes begin.

        Verified indirectly: process_research_file records 'started' then
        transitions to 'completed' only after all writes succeed. We confirm
        no job row remains in 'started' state after a clean run.
        """
        conn = sqlite3.connect(db)
        extraction = {k: [] for k in ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]}
        with patch("process_research.run_extractions", return_value=extraction):
            pr.process_research_file(
                "2026_04_25_started_test.txt",
                content=SAMPLE_CONTENT,
                conn=conn,
                source_date=date(2026, 4, 25),
            )
        conn.commit()
        row = conn.execute(
            "SELECT status FROM processing_jobs WHERE source_file='2026_04_25_started_test.txt'"
        ).fetchone()
        assert row is not None
        assert row[0] != "started", "Job should not be left in 'started' state after completion"
        assert row[0] in ("completed", "completed_with_warnings")

    def test_partial_failure_marks_completed_with_warnings(self, db):
        """If write_research_to_db returns failures, job status is completed_with_warnings."""
        conn = sqlite3.connect(db)
        extraction = {k: [] for k in ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]}
        with patch("process_research.run_extractions", return_value=extraction), \
             patch("process_research.write_research_to_db", return_value=["concept write failed: test error"]):
            pr.process_research_file(
                "2026_04_25_warn_test.txt",
                content=SAMPLE_CONTENT,
                conn=conn,
                source_date=date(2026, 4, 25),
            )
        conn.commit()
        row = conn.execute(
            "SELECT status, error_log FROM processing_jobs WHERE source_file='2026_04_25_warn_test.txt'"
        ).fetchone()
        assert row is not None
        assert row[0] == "completed_with_warnings"
        assert "concept write failed" in (row[1] or "")


# ── Provenance writes ─────────────────────────────────────────────────────────

class TestResearchProvenance:
    """write_research_to_db must produce memory_provenance rows for all 6 memory types."""

    def _prov_count(self, conn, memory_type):
        return conn.execute(
            "SELECT COUNT(*) FROM memory_provenance WHERE memory_type=?",
            (memory_type,)
        ).fetchone()[0]

    def test_concept_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "concept") >= 1
        conn.close()

    def test_belief_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "belief") >= 1
        conn.close()

    def test_entity_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "entity") >= 1
        conn.close()

    def test_pattern_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "pattern") >= 1
        conn.close()

    def test_question_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "question") >= 1
        conn.close()

    def test_epiphany_provenance_written(self, db):
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        assert self._prov_count(conn, "epiphany") >= 1
        conn.close()

    def test_extended_provenance_fields_stored(self, db):
        """source_url and source_fetched_at must be written when provided."""
        conn = sqlite3.connect(db)
        pr.write_research_to_db(
            conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25),
            source_url="https://www.youtube.com/watch?v=4nqtyCSS7Fg",
            source_fetched_at="2026-04-25T21:45:00",
            processing_job_id=99,
        )
        conn.commit()
        row = conn.execute(
            "SELECT source_url, source_fetched_at, processing_job_id "
            "FROM memory_provenance WHERE memory_type='belief'"
        ).fetchone()
        assert row is not None
        assert row[0] == "https://www.youtube.com/watch?v=4nqtyCSS7Fg"
        assert row[1] == "2026-04-25T21:45:00"
        assert row[2] == 99
        conn.close()

    def test_all_six_types_produce_provenance(self, db):
        """A single write_research_to_db call must cover all 6 extraction types."""
        conn = sqlite3.connect(db)
        pr.write_research_to_db(conn, MINIMAL_EXTRACTION, "test.txt", date(2026, 4, 25))
        conn.commit()
        types_found = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT memory_type FROM memory_provenance"
            ).fetchall()
        }
        conn.close()
        expected = {"concept", "belief", "entity", "pattern", "question", "epiphany"}
        assert expected <= types_found, f"Missing provenance for: {expected - types_found}"
        conn.close()

    def test_skips_already_processed(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO processing_jobs (job_type, target_type, source_file, status, model_used) "
            "VALUES ('research_extraction', 'research_file', ?, 'completed', 'qwen2.5:14b')",
            ("2026_04_25_test_transcript.txt",)
        )
        conn.commit()
        with patch("process_research.run_extractions") as mock_extract:
            pr.process_research_file(
                "2026_04_25_test_transcript.txt",
                content=SAMPLE_CONTENT,
                conn=conn,
                source_date=date(2026, 4, 25),
            )
            mock_extract.assert_not_called()
        conn.close()

    def test_force_flag_overrides_dedup(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO processing_jobs (job_type, target_type, source_file, status, model_used) "
            "VALUES ('research_extraction', 'research_file', ?, 'completed', 'qwen2.5:14b')",
            ("2026_04_25_test_transcript.txt",)
        )
        conn.commit()
        extraction = {k: [] for k in ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]}
        with patch("process_research.run_extractions", return_value=extraction) as mock_extract:
            pr.process_research_file(
                "2026_04_25_test_transcript.txt",
                content=SAMPLE_CONTENT,
                conn=conn,
                source_date=date(2026, 4, 25),
                force=True,
            )
            mock_extract.assert_called_once()
        conn.close()
