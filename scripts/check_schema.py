"""
check_schema.py
---------------
Compares the live database schema against the expected schema defined in
setup_db.py. Reports tables and columns that are present in one but missing
from the other.

Run this after any manual DB change, or as part of a CI / pre-commit check.

Usage:
    python3 check_schema.py
    python3 check_schema.py --db /path/to/other.db
    python3 check_schema.py --strict       # exit 1 if any drift detected
    python3 check_schema.py --quiet        # print nothing if schema is clean

Exit codes:
    0   schema matches (or only unregistered extras found — see --strict)
    1   unexpected drift detected (missing tables or columns)
"""

import sqlite3
import argparse
import sys
import tempfile
import os
from pathlib import Path

DB_PATH = Path.home() / "claude_memory" / "memory.db"
SCRIPTS_DIR = Path(__file__).parent


def _live_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return {table_name: {col_name, ...}} for all user tables in the live DB."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    schema: dict[str, set[str]] = {}
    for (tname,) in tables:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tname})").fetchall()}
        schema[tname] = cols
    return schema


def _expected_schema() -> dict[str, set[str]]:
    """
    Build the schema that setup_db.py would create on a fresh install.
    Uses a throwaway in-memory SQLite database so we never touch the real DB.
    """
    import importlib.util, types

    spec = importlib.util.spec_from_file_location(
        "setup_db_check", SCRIPTS_DIR / "setup_db.py"
    )
    mod = importlib.util.module_from_spec(spec)

    # Override DB_PATH in the module so it never touches the real file
    mod.DB_PATH = ":memory:"
    spec.loader.exec_module(mod)

    conn = sqlite3.connect(":memory:")
    mod.create_latest_schema(conn)
    schema = _live_schema(conn)
    conn.close()
    return schema


def check(db_path: Path, strict: bool = False, quiet: bool = False) -> int:
    """
    Compare live schema to expected schema.

    Returns 0 if clean, 1 if drift found (always 1 in strict mode for any delta).
    """
    live_conn = sqlite3.connect(db_path)
    live = _live_schema(live_conn)
    live_conn.close()

    try:
        expected = _expected_schema()
    except Exception as exc:
        print(f"ERROR: could not build expected schema from setup_db.py: {exc}",
              file=sys.stderr)
        return 1

    live_tables = set(live)
    expected_tables = set(expected)

    missing_tables   = expected_tables - live_tables   # in setup_db but not in DB
    extra_tables     = live_tables - expected_tables   # in DB but not in setup_db

    missing_columns: dict[str, set[str]] = {}
    extra_columns:   dict[str, set[str]] = {}

    for table in expected_tables & live_tables:
        miss = expected[table] - live[table]
        extra = live[table] - expected[table]
        if miss:
            missing_columns[table] = miss
        if extra:
            extra_columns[table] = extra

    has_drift = bool(missing_tables or missing_columns)
    has_extras = bool(extra_tables or extra_columns)

    if not has_drift and not has_extras:
        if not quiet:
            print("Schema OK — live DB matches setup_db.py")
        return 0

    if has_drift:
        print(f"\nSCHEMA DRIFT DETECTED — {db_path}\n")
        if missing_tables:
            print("  Tables in setup_db.py but MISSING from live DB:")
            for t in sorted(missing_tables):
                print(f"    - {t}")
        if missing_columns:
            print("  Columns in setup_db.py but MISSING from live DB:")
            for t, cols in sorted(missing_columns.items()):
                for col in sorted(cols):
                    print(f"    - {t}.{col}")
        print()
        print("  Fix: run `python3 scripts/migrate_db.py` to apply pending migrations,")
        print("  or add an ALTER TABLE statement to migrate_db.py MIGRATIONS.")
        print()

    if has_extras and not quiet:
        print("  Tables/columns in live DB not in setup_db.py (may be fine):")
        for t in sorted(extra_tables):
            print(f"    + {t}  (extra table)")
        for t, cols in sorted(extra_columns.items()):
            for col in sorted(cols):
                print(f"    + {t}.{col}  (extra column)")
        print()

    if strict:
        return 1 if (has_drift or has_extras) else 0
    return 1 if has_drift else 0


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare live DB schema against setup_db.py expected schema"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to memory.db (default: ~/claude_memory/memory.db)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any delta found, including extra tables/columns",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print nothing if schema is clean",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    sys.exit(check(args.db, strict=args.strict, quiet=args.quiet))


if __name__ == "__main__":
    _main()
