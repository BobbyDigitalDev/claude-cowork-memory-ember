"""
migrate_db.py
-------------
Incremental schema migration runner for ember-engine.

Maintains a `schema_migrations` table that records every applied migration
with its version, description, and a checksum of the SQL statements.

Safe to run against any existing DB, including ones with no prior migration
tracking: the detect() function for each migration inspects the actual live
schema (column presence, table existence) and marks already-applied
migrations as complete without re-running them.

Usage:
    python3 migrate_db.py                  # apply all pending migrations
    python3 migrate_db.py --status         # show applied / pending status
    python3 migrate_db.py --dry-run        # show SQL without executing
    python3 migrate_db.py --db /path/to/db # target a specific database
"""

import sqlite3
import hashlib
import argparse
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / "claude_memory" / "memory.db"

# ── Schema helpers ─────────────────────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return row[0] > 0


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def _checksum(statements: list[str]) -> str:
    blob = "\n".join(s.strip() for s in statements).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


# ── Migration registry ─────────────────────────────────────────────────────────
#
# Each entry is a dict:
#   version     unique semver string — primary key in schema_migrations
#   name        human-readable description logged to schema_migrations
#   detect      callable(conn) -> bool
#               Returns True if the migration's effect is already present
#               in the live DB. Used for bootstrap: marks applied without
#               re-running when first introducing migration tracking.
#   statements  ordered list of SQL strings executed in a single transaction

MIGRATIONS: list[dict] = [
    {
        "version": "2.3.0",
        "name": (
            "user_id/agent_id on 10 core tables; "
            "source_filename/source_hash/source_timestamp on conversations; "
            "call_name/source_file already in processing_jobs from v2.2 definition"
        ),
        "detect": lambda conn: _column_exists(conn, "conversations", "source_filename"),
        "statements": [
            # conversations — source provenance fields
            "ALTER TABLE conversations ADD COLUMN source_filename TEXT",
            "ALTER TABLE conversations ADD COLUMN source_hash TEXT",
            "ALTER TABLE conversations ADD COLUMN source_timestamp TEXT",
            "ALTER TABLE conversations ADD COLUMN user_id TEXT DEFAULT 'bobby'",
            "ALTER TABLE conversations ADD COLUMN agent_id TEXT DEFAULT 'claude'",
            # core memory tables — multi-user identity
            "ALTER TABLE beliefs     ADD COLUMN user_id  TEXT",
            "ALTER TABLE beliefs     ADD COLUMN agent_id TEXT",
            "ALTER TABLE epiphanies  ADD COLUMN user_id  TEXT",
            "ALTER TABLE epiphanies  ADD COLUMN agent_id TEXT",
            "ALTER TABLE concepts    ADD COLUMN user_id  TEXT",
            "ALTER TABLE concepts    ADD COLUMN agent_id TEXT",
            "ALTER TABLE patterns    ADD COLUMN user_id  TEXT",
            "ALTER TABLE patterns    ADD COLUMN agent_id TEXT",
            "ALTER TABLE questions   ADD COLUMN user_id  TEXT",
            "ALTER TABLE questions   ADD COLUMN agent_id TEXT",
            "ALTER TABLE goals       ADD COLUMN user_id  TEXT",
            "ALTER TABLE goals       ADD COLUMN agent_id TEXT",
            "ALTER TABLE entities    ADD COLUMN user_id  TEXT",
            "ALTER TABLE entities    ADD COLUMN agent_id TEXT",
            "ALTER TABLE moods       ADD COLUMN user_id  TEXT",
            "ALTER TABLE moods       ADD COLUMN agent_id TEXT",
            "ALTER TABLE gratitude   ADD COLUMN user_id  TEXT",
            "ALTER TABLE gratitude   ADD COLUMN agent_id TEXT",
            "ALTER TABLE sessions    ADD COLUMN user_id  TEXT",
            "ALTER TABLE sessions    ADD COLUMN agent_id TEXT",
        ],
    },
    {
        "version": "2.4.0",
        "name": "trusted_sources table for YouTube channels and research publications",
        "detect": lambda conn: _table_exists(conn, "trusted_sources"),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS trusted_sources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type   TEXT    NOT NULL DEFAULT 'youtube_channel',
                channel_id    TEXT,
                channel_name  TEXT,
                channel_url   TEXT,
                topic_focus   TEXT,
                quality_notes TEXT,
                date_added    TEXT,
                approved_by   TEXT    DEFAULT 'user',
                is_active     INTEGER DEFAULT 1,
                notes         TEXT,
                created_at    TEXT    DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_trusted_sources_type    ON trusted_sources (source_type)",
            "CREATE INDEX IF NOT EXISTS idx_trusted_sources_active  ON trusted_sources (is_active)",
            "CREATE INDEX IF NOT EXISTS idx_trusted_sources_channel ON trusted_sources (channel_id)",
        ],
    },
    {
        "version": "2.5.0",
        "name": "scout_results table with challenge_score for research divergence tracking",
        "detect": lambda conn: _table_exists(conn, "scout_results"),
        "statements": [
            """
            CREATE TABLE IF NOT EXISTS scout_results (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid             TEXT NOT NULL DEFAULT (
                    lower(hex(randomblob(4)) || '-' ||
                    hex(randomblob(2)) || '-4' || substr(hex(randomblob(2)),2) ||
                    '-' || substr('89ab', abs(random()) % 4 + 1, 1) ||
                    substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))),
                title            TEXT,
                authors          TEXT,
                abstract         TEXT,
                doi              TEXT,
                source_url       TEXT,
                source_name      TEXT,
                source_type      TEXT,
                publication_date TEXT,
                external_id      TEXT,
                date_fetched     TEXT NOT NULL DEFAULT (date('now')),
                search_query     TEXT,
                search_ring      INTEGER,
                triggered_by     TEXT,
                relevance_score  REAL,
                relevance_notes  TEXT,
                status           TEXT NOT NULL DEFAULT 'pending',
                curator_notes    TEXT,
                promoted_to      TEXT,
                reviewed_at      TEXT,
                tags             TEXT,
                challenge_score  REAL,
                created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_scout_status    ON scout_results (status)",
            "CREATE INDEX IF NOT EXISTS idx_scout_relevance ON scout_results (relevance_score)",
            "CREATE INDEX IF NOT EXISTS idx_scout_date      ON scout_results (date_fetched)",
            "CREATE INDEX IF NOT EXISTS idx_scout_source    ON scout_results (source_type, source_name)",
        ],
    },
    {
        "version": "2.6.0",
        "name": (
            "quarantine_reason column on beliefs; "
            "needs_review status in belief lifecycle for weakly-grounded extractions"
        ),
        "detect": lambda conn: _column_exists(conn, "beliefs", "quarantine_reason"),
        "statements": [
            # quarantine_reason records why a belief was held for review:
            # e.g. "low confidence (0.32)", "no verbatim anchor", "no evidence snippets"
            "ALTER TABLE beliefs ADD COLUMN quarantine_reason TEXT",
            # Partial index — fast lookup of all quarantined beliefs
            "CREATE INDEX IF NOT EXISTS idx_beliefs_needs_review "
            "ON beliefs (status, created_at)",
        ],
    },
    {
        "version": "2.7.0",
        "name": (
            "Extended research provenance: source_url, source_fetched_at, "
            "processing_job_id per memory_provenance row"
        ),
        "detect": lambda conn: _column_exists(conn, "memory_provenance", "source_url"),
        "statements": [
            "ALTER TABLE memory_provenance ADD COLUMN source_url         TEXT",
            "ALTER TABLE memory_provenance ADD COLUMN source_fetched_at  TEXT",
            "ALTER TABLE memory_provenance ADD COLUMN processing_job_id  INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_provenance_job "
            "ON memory_provenance (processing_job_id)",
            "CREATE INDEX IF NOT EXISTS idx_provenance_url "
            "ON memory_provenance (source_url)",
        ],
    },
    {
        "version": "2.8.0",
        "name": (
            "memory_origin column on concepts, entities, patterns, questions; "
            "removes need for ad hoc ALTER TABLE shim in process_research.py"
        ),
        "detect": lambda conn: all(
            _column_exists(conn, tbl, "memory_origin")
            for tbl in ("concepts", "entities", "patterns", "questions")
        ),
        "statements": [
            "ALTER TABLE concepts  ADD COLUMN memory_origin TEXT DEFAULT 'conversation'",
            "ALTER TABLE entities  ADD COLUMN memory_origin TEXT DEFAULT 'conversation'",
            "ALTER TABLE patterns  ADD COLUMN memory_origin TEXT DEFAULT 'conversation'",
            "ALTER TABLE questions ADD COLUMN memory_origin TEXT DEFAULT 'conversation'",
        ],
    },
]


# ── schema_migrations table ────────────────────────────────────────────────────

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    TEXT UNIQUE NOT NULL,
    name       TEXT,
    applied_at TEXT NOT NULL,
    checksum   TEXT
)
"""

_CREATE_MIGRATIONS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_schema_migrations_version "
    "ON schema_migrations (version)"
)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_MIGRATIONS_TABLE)
    conn.execute(_CREATE_MIGRATIONS_INDEX)
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }


def _record_migration(
    conn: sqlite3.Connection,
    version: str,
    name: str,
    checksum: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum) "
        "VALUES (?, ?, ?, ?)",
        (version, name, datetime.now().isoformat(), checksum),
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def apply_migrations(db_path: Path = DB_PATH, dry_run: bool = False) -> int:
    """
    Apply all pending migrations to the database at db_path.

    Bootstrap-safe: for any migration whose detect() returns True, the
    migration is recorded as applied without re-executing its SQL. This
    handles existing DBs that predate migration tracking.

    Returns the number of migrations applied (excluding bootstrap records).
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_migrations_table(conn)
    applied = _applied_versions(conn)
    newly_applied = 0

    for mig in MIGRATIONS:
        version = mig["version"]
        name = mig["name"]
        stmts = mig["statements"]
        chk = _checksum(stmts)

        if version in applied:
            continue

        # Bootstrap detection: effect already present without prior tracking
        if mig["detect"](conn):
            if not dry_run:
                _record_migration(conn, version, name, chk)
                conn.commit()
            print(f"  [bootstrap] {version} — {name[:60]}")
            continue

        # Normal apply
        if dry_run:
            print(f"  [pending]   {version} — {name[:60]}")
            for s in stmts:
                print(f"    {s.strip()[:80]}")
            continue

        try:
            with conn:
                for stmt in stmts:
                    conn.execute(stmt)
                _record_migration(conn, version, name, chk)
            print(f"  [applied]   {version} — {name[:60]}")
            newly_applied += 1
        except sqlite3.Error as exc:
            print(f"  [ERROR]     {version} — {exc}", file=sys.stderr)
            conn.close()
            raise

    conn.close()
    return newly_applied


def migration_status(db_path: Path = DB_PATH) -> None:
    """Print the applied / pending status of every registered migration."""
    conn = sqlite3.connect(db_path)
    _ensure_migrations_table(conn)
    applied = _applied_versions(conn)

    rows = conn.execute(
        "SELECT version, name, applied_at FROM schema_migrations ORDER BY id"
    ).fetchall()
    applied_detail = {r[0]: (r[1], r[2]) for r in rows}

    print(f"\nSchema migrations — {db_path}\n")
    for mig in MIGRATIONS:
        v = mig["version"]
        if v in applied_detail:
            _name, _at = applied_detail[v]
            print(f"  [applied]  {v}  ({_at[:10]})  {mig['name'][:55]}")
        else:
            # Check if already-applied but untracked
            if mig["detect"](conn):
                status = "present/untracked"
            else:
                status = "PENDING"
            print(f"  [{status:<16}] {v}  {mig['name'][:55]}")

    unregistered = applied - {m["version"] for m in MIGRATIONS}
    if unregistered:
        print(f"\n  Unregistered applied versions: {sorted(unregistered)}")

    conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="ember-engine schema migration runner"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to memory.db (default: ~/claude_memory/memory.db)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show migration status without applying anything",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without executing them",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        print("Run setup_db.py first to create a fresh database.", file=sys.stderr)
        sys.exit(1)

    if args.status:
        migration_status(args.db)
        return

    print(f"\nRunning migrations on {args.db}\n")
    n = apply_migrations(args.db, dry_run=args.dry_run)

    if args.dry_run:
        print("\n(dry-run — no changes written)")
    elif n == 0:
        print("  All migrations already applied.")
    else:
        print(f"\n  {n} migration(s) applied successfully.")


if __name__ == "__main__":
    _main()
