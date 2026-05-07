"""
Tests for migrate_db.py — migration detection, application, idempotency,
and version tracking in schema_migrations table.
"""
import sqlite3
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import scripts.migrate_db as migrate_db
import scripts.setup_db as setup_db


def _fresh_db(tmp_path, name="test.db"):
    """Create a DB with create_latest_schema already applied."""
    db_path = tmp_path / name
    conn = sqlite3.connect(str(db_path))
    setup_db.create_latest_schema(conn)
    conn.close()
    return db_path


def _applied_versions(db_path):
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# MIGRATIONS list integrity
# ---------------------------------------------------------------------------

class TestMigrationsList:
    def test_all_expected_versions_present(self):
        versions = [m["version"] for m in migrate_db.MIGRATIONS]
        for v in ["2.3.0", "2.4.0", "2.5.0", "2.6.0", "2.7.0", "2.8.0"]:
            assert v in versions, f"Migration {v} missing from MIGRATIONS list"

    def test_versions_in_ascending_order(self):
        from packaging.version import Version
        versions = [m["version"] for m in migrate_db.MIGRATIONS]
        assert versions == sorted(versions, key=Version)

    def test_each_migration_has_required_keys(self):
        for m in migrate_db.MIGRATIONS:
            assert "version" in m
            assert "name" in m
            assert "detect" in m
            assert "statements" in m
            assert callable(m["detect"])
            assert isinstance(m["statements"], list)

    def test_each_migration_has_at_least_one_statement(self):
        for m in migrate_db.MIGRATIONS:
            assert len(m["statements"]) >= 1, f"Migration {m['version']} has no SQL statements"


# ---------------------------------------------------------------------------
# apply_migrations on a fresh (already-migrated) DB
# ---------------------------------------------------------------------------

class TestApplyMigrationsAlreadyMigrated:
    def test_returns_zero_when_all_already_applied(self, tmp_path):
        """create_latest_schema() already has all columns; detect() should
        bootstrap-record them without re-applying, so newly_applied == 0."""
        db_path = _fresh_db(tmp_path)
        count = migrate_db.apply_migrations(db_path)
        assert count == 0

    def test_idempotent_second_run(self, tmp_path):
        db_path = _fresh_db(tmp_path)
        migrate_db.apply_migrations(db_path)
        count2 = migrate_db.apply_migrations(db_path)
        assert count2 == 0

    def test_all_versions_recorded_after_run(self, tmp_path):
        db_path = _fresh_db(tmp_path)
        migrate_db.apply_migrations(db_path)
        applied = _applied_versions(db_path)
        for m in migrate_db.MIGRATIONS:
            assert m["version"] in applied, f"{m['version']} not recorded in schema_migrations"


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_does_not_modify_schema_migrations(self, tmp_path):
        """On a DB that has no migrations recorded but all columns present,
        dry_run should not write anything to schema_migrations."""
        db_path = tmp_path / "dry.db"
        # Create a DB with the full schema but NO schema_migrations entries
        conn = sqlite3.connect(str(db_path))
        setup_db.create_latest_schema(conn)
        # Wipe the migrations table to simulate un-tracked state
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
        conn.close()

        migrate_db.apply_migrations(db_path, dry_run=True)

        # Nothing should have been recorded
        applied = _applied_versions(db_path)
        assert len(applied) == 0


# ---------------------------------------------------------------------------
# detect() lambdas work correctly on a schema that has the columns
# ---------------------------------------------------------------------------

class TestDetectLambdas:
    def test_all_detects_return_true_on_fresh_db(self, tmp_path):
        """Every migration's detect() should return True on a DB built by
        create_latest_schema() — meaning the effect is already present."""
        db_path = _fresh_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        for m in migrate_db.MIGRATIONS:
            result = m["detect"](conn)
            assert result is True, (
                f"detect() for {m['version']} returned {result!r} on fresh schema — "
                "the detection logic or the schema may be out of sync"
            )
        conn.close()

    def test_detects_return_false_on_empty_db(self, tmp_path):
        """On a completely empty DB, detect() should return False for all
        migrations that check for specific columns/tables."""
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        # No tables at all — every detect should return False
        for m in migrate_db.MIGRATIONS:
            result = m["detect"](conn)
            assert result is False, (
                f"detect() for {m['version']} returned True on empty DB — "
                "detection logic may be inverted"
            )
        conn.close()
