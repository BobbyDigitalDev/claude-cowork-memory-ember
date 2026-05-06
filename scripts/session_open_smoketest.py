#!/usr/bin/env python3
"""
session_open_smoketest.py
-------------------------
Validates that the tables and columns referenced in the SESSION OPEN PROTOCOL
actually exist in memory.db. Run at session open (Step 0 of SESSION OPEN PROTOCOL).

Exits 0 if all critical checks pass (optional missing tables only warn).
Exits 1 if any critical table or column is missing.

USAGE
-----
    python3 ~/claude_memory/scripts/session_open_smoketest.py
    python3 ~/claude_memory/scripts/session_open_smoketest.py --quiet   # only print failures
"""

import sqlite3
import sys
import argparse
from pathlib import Path

# Try standard install location first, then probe sandbox mounts as fallback.
def _find_db() -> Path:
    standard = Path.home() / "claude_memory" / "memory.db"
    if standard.exists():
        return standard
    # Cowork bash sandbox: DB is mounted under /sessions/<session>/mnt/
    sessions_root = Path("/sessions")
    if sessions_root.exists():
        try:
            for session_dir in sorted(sessions_root.iterdir()):
                candidate = session_dir / "mnt" / "claude_memory" / "memory.db"
                try:
                    if candidate.exists():
                        return candidate
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass
    return standard  # fall through; open() will produce a clear error

DB_PATH = _find_db()

# Tables and columns that SESSION OPEN PROTOCOL queries directly.
# A missing column here causes a runtime error — these are hard failures.
CRITICAL_CHECKS = [
    ("processing_jobs", ["id", "source_file", "status", "created_at", "call_name"]),
    ("goals",           ["id", "description", "category", "status", "priority"]),
    ("beliefs",         ["id", "topic", "is_active", "confidence_score", "status"]),
    ("questions",       ["id", "question", "status", "category"]),
    ("sessions",        ["id", "date"]),
    ("memory_chunks",   ["id", "content", "embedding_vector"]),
]

# Tables that may not exist yet (agent not built), but are referenced in instructions.
# Missing table = warning only. Missing column within an existing table = hard failure.
OPTIONAL_CHECKS = [
    ("scout_results", ["id", "title", "source_name", "relevance_score", "status"]),
]


def get_columns(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] > 0


def run_checks(conn, checks: list, optional: bool, quiet: bool) -> list:
    """Run a list of (table, columns) checks. Returns list of failure strings."""
    failures = []
    for table, expected_cols in checks:
        exists = table_exists(conn, table)
        if not exists:
            if optional:
                if not quiet:
                    print(f"  [WARN]  {table} — table not found (optional, agent not yet built)")
            else:
                msg = f"{table} — table missing entirely"
                print(f"  [FAIL]  {msg}")
                failures.append(msg)
            continue

        actual_cols = get_columns(conn, table)
        missing = [c for c in expected_cols if c not in actual_cols]
        if missing:
            for col in missing:
                msg = f"{table}.{col} — column missing (actual cols: {sorted(actual_cols)})"
                print(f"  [FAIL]  {msg}")
                failures.append(msg)
        else:
            if not quiet:
                label = "(optional)" if optional else ""
                print(f"  [OK]    {table} {label}")

    return failures


def main():
    parser = argparse.ArgumentParser(
        description="Validate memory.db schema against SESSION OPEN PROTOCOL expectations"
    )
    parser.add_argument("--quiet", action="store_true",
                        help="Only print failures and warnings, not passing checks")
    parser.add_argument("--db", default=str(DB_PATH),
                        help=f"Path to memory.db (default: {DB_PATH})")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: database not found at {db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db)

    if not args.quiet:
        print("Session open smoketest — checking schema against SESSION OPEN PROTOCOL")
        print(f"DB: {db}")
        print()
        print("Critical tables:")

    critical_failures = run_checks(conn, CRITICAL_CHECKS, optional=False, quiet=args.quiet)

    if not args.quiet:
        print()
        print("Optional tables (agent-dependent):")

    run_checks(conn, OPTIONAL_CHECKS, optional=True, quiet=args.quiet)

    conn.close()

    if not args.quiet:
        print()

    if critical_failures:
        print(f"SMOKETEST FAILED — {len(critical_failures)} critical issue(s).")
        print("Run: python3 ~/claude_memory/scripts/setup_db.py --check to diagnose.")
        print("Or inspect with: PRAGMA table_info(<table>)")
        sys.exit(1)
    else:
        if not args.quiet:
            print("SMOKETEST PASSED — schema matches SESSION OPEN PROTOCOL expectations.")
        sys.exit(0)


if __name__ == "__main__":
    main()
