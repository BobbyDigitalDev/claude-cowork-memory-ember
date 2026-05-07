"""
Tests for ingest.py — check_schema_version error message (BUG-002 regression),
sha256_of_file, and make_stripped_temp_file / strip_private_content.
"""
import hashlib
import sqlite3
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.setup_db as setup_db
import scripts.ingest as ingest


# ---------------------------------------------------------------------------
# check_schema_version — BUG-002 regression guard
# ---------------------------------------------------------------------------

class TestCheckSchemaVersion:
    def test_error_message_references_migrate_db_not_old_script(self, tmp_path):
        """BUG-002: error recovery message must say migrate_db.py, not the
        nonexistent migrate_schema_v2_3.py."""
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE conversations "
            "(id INTEGER PRIMARY KEY, source_filename TEXT)"
        )
        conn.commit()
        conn.close()

        captured = []

        def fake_print(msg="", **kwargs):
            captured.append(str(msg))

        with patch.object(ingest, "get_db", return_value=sqlite3.connect(str(db_path))), \
             patch("builtins.print", side_effect=fake_print), \
             pytest.raises(SystemExit):
            ingest.check_schema_version()

        full_output = "\n".join(captured)
        assert "migrate_db.py" in full_output, (
            "BUG-002 regression: error message must reference migrate_db.py"
        )
        assert "migrate_schema_v2_3.py" not in full_output, (
            "BUG-002 regression: old nonexistent script name must not appear"
        )

    def test_no_exit_when_source_hash_column_present(self, tmp_path):
        db_path = tmp_path / "new.db"
        conn = sqlite3.connect(str(db_path))
        setup_db.create_latest_schema(conn)
        conn.close()

        with patch.object(ingest, "get_db", return_value=sqlite3.connect(str(db_path))):
            ingest.check_schema_version()  # should not raise

    def test_no_check_when_db_is_none(self):
        with patch.object(ingest, "get_db", return_value=None):
            ingest.check_schema_version()  # fresh install, no raise


# ---------------------------------------------------------------------------
# sha256_of_file
# ---------------------------------------------------------------------------

class TestSha256OfFile:
    def test_returns_correct_hash(self, tmp_path):
        f = tmp_path / "test.txt"
        content = b"hello ember engine"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert ingest.sha256_of_file(str(f)) == expected

    def test_different_files_have_different_hashes(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"content one")
        f2.write_bytes(b"content two")
        assert ingest.sha256_of_file(str(f1)) != ingest.sha256_of_file(str(f2))

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"identical content")
        f2.write_bytes(b"identical content")
        assert ingest.sha256_of_file(str(f1)) == ingest.sha256_of_file(str(f2))

    def test_returns_64_char_hex_string(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"x")
        result = ingest.sha256_of_file(str(f))
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# strip_private_content — private block removal
# ---------------------------------------------------------------------------

class TestStripPrivateContent:
    def test_removes_private_block(self):
        text = "Public\n<private>Secret stuff</private>\nMore public"
        result = ingest.strip_private_content(text)
        assert "Secret stuff" not in result
        assert "Public" in result
        assert "More public" in result

    def test_replaces_with_placeholder(self):
        text = "Before <private>hidden</private> after"
        result = ingest.strip_private_content(text)
        assert "[private content omitted]" in result

    def test_no_private_block_unchanged(self):
        text = "All public content here"
        result = ingest.strip_private_content(text)
        assert result == text

    def test_multiple_blocks_all_removed(self):
        text = "<private>one</private> mid <private>two</private>"
        result = ingest.strip_private_content(text)
        assert "one" not in result
        assert "two" not in result


# ---------------------------------------------------------------------------
# make_stripped_temp_file — returns (path, count) tuple
# ---------------------------------------------------------------------------

class TestMakeStrippedTempFile:
    def test_returns_none_false_when_no_private_content(self, tmp_path):
        source = tmp_path / "convo.md"
        source.write_text("No private content here")
        path, was_stripped = ingest.make_stripped_temp_file(source)
        assert path is None
        assert was_stripped is False

    def test_returns_path_and_count_when_private_content_exists(self, tmp_path):
        source = tmp_path / "convo.md"
        source.write_text("Public\n<private>Secret</private>\nMore public")
        path, count = ingest.make_stripped_temp_file(source)
        try:
            assert path is not None
            assert count == 1
            assert Path(path).exists()
            content = Path(path).read_text()
            assert "Secret" not in content
            assert "Public" in content
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    def test_temp_file_in_same_directory(self, tmp_path):
        source = tmp_path / "convo.md"
        source.write_text("Public\n<private>Secret</private>")
        path, _ = ingest.make_stripped_temp_file(source)
        try:
            assert Path(path).parent == tmp_path
        finally:
            if path:
                Path(path).unlink(missing_ok=True)
