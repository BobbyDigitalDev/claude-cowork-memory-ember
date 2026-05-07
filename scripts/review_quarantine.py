"""
review_quarantine.py
--------------------
Interactive reviewer for beliefs in 'needs_review' (quarantine) status.

Quarantined beliefs were extracted by Qwen but failed one or more grounding
checks at write time (low confidence, missing verbatim anchor, no evidence).
They are excluded from normal memory operations until manually reviewed.

Usage:
    python3 review_quarantine.py           # interactive review session
    python3 review_quarantine.py --list    # list all quarantined beliefs (no interaction)
    python3 review_quarantine.py --stats   # summary counts by quarantine reason

Actions during interactive review:
    a  approve    promote to 'proposed' — enters normal belief lifecycle
    d  dismiss    mark as 'archived'    — removes from active memory
    s  skip       leave as 'needs_review' for next time
    q  quit       exit without processing remaining beliefs
"""

import sqlite3
import argparse
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / "claude_memory" / "memory.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_quarantined(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT b.id, b.topic, b.position, b.confidence_score,
               b.quarantine_reason, b.verbatim_anchor, b.evidence_snippets,
               b.created_at, c.date AS conv_date, c.id AS conv_id
        FROM   beliefs b
        LEFT   JOIN conversations c ON c.id = b.source_conversation_id
        WHERE  b.status = 'needs_review'
        AND    b.is_active = 1
        ORDER  BY b.created_at DESC
    """).fetchall()


def _promote(conn: sqlite3.Connection, belief_id: int) -> None:
    now = datetime.now().isoformat()
    conn.execute("""
        UPDATE beliefs
        SET    status = 'proposed', quarantine_reason = NULL, updated_at = ?
        WHERE  id = ?
    """, (now, belief_id))
    conn.commit()


def _dismiss(conn: sqlite3.Connection, belief_id: int) -> None:
    now = datetime.now().isoformat()
    conn.execute("""
        UPDATE beliefs
        SET    status = 'archived', is_active = 0, archived_at = ?, updated_at = ?
        WHERE  id = ?
    """, (now, now, belief_id))
    conn.commit()


def _list(db_path: Path) -> None:
    conn = _connect(db_path)
    rows = _fetch_quarantined(conn)
    conn.close()

    if not rows:
        print("No quarantined beliefs.")
        return

    print(f"\n{'='*70}")
    print(f"  Quarantined beliefs: {len(rows)}")
    print(f"{'='*70}\n")

    for r in rows:
        print(f"  ID {r['id']} | conf={r['confidence_score']:.2f} | {r['conv_date'] or 'no date'}")
        print(f"  Topic:  {r['topic']}")
        print(f"  Why:    {r['quarantine_reason']}")
        print(f"  Pos:    {(r['position'] or '')[:120]}")
        print()


def _stats(db_path: Path) -> None:
    conn = _connect(db_path)
    total = conn.execute(
        "SELECT COUNT(*) FROM beliefs WHERE status = 'needs_review' AND is_active = 1"
    ).fetchone()[0]

    reasons = conn.execute("""
        SELECT quarantine_reason, COUNT(*) AS n
        FROM   beliefs
        WHERE  status = 'needs_review' AND is_active = 1
        GROUP  BY quarantine_reason
        ORDER  BY n DESC
    """).fetchall()
    conn.close()

    print(f"\nQuarantined beliefs: {total}\n")
    for r in reasons:
        print(f"  {r[1]:>4}  {r[0]}")
    print()


def _interactive(db_path: Path) -> None:
    conn = _connect(db_path)
    rows = _fetch_quarantined(conn)

    if not rows:
        print("No quarantined beliefs to review.")
        conn.close()
        return

    approved = dismissed = skipped = 0
    print(f"\n{len(rows)} quarantined belief(s) to review.")
    print("Actions: [a]pprove  [d]ismiss  [s]kip  [q]uit\n")

    for i, r in enumerate(rows, 1):
        print(f"─── {i}/{len(rows)} ─── ID {r['id']} ───────────────────────────────")
        print(f"  Topic:      {r['topic']}")
        print(f"  Position:   {(r['position'] or '')[:200]}")
        print(f"  Confidence: {r['confidence_score']:.2f}")
        print(f"  Quarantine: {r['quarantine_reason']}")
        if r["verbatim_anchor"]:
            print(f"  Anchor:     {r['verbatim_anchor'][:120]}")
        print()

        while True:
            try:
                choice = input("  Action [a/d/s/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            if choice == "a":
                _promote(conn, r["id"])
                print(f"  Approved — promoted to 'proposed'\n")
                approved += 1
                break
            elif choice == "d":
                _dismiss(conn, r["id"])
                print(f"  Dismissed — archived\n")
                dismissed += 1
                break
            elif choice == "s":
                print(f"  Skipped\n")
                skipped += 1
                break
            elif choice == "q":
                print("\nExiting.")
                conn.close()
                print(f"\nSession: {approved} approved, {dismissed} dismissed, {skipped} skipped.")
                return
            else:
                print("  Enter a, d, s, or q.")

    conn.close()
    print(f"Review complete: {approved} approved, {dismissed} dismissed, {skipped} skipped.")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Review quarantined (needs_review) beliefs"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to memory.db (default: ~/claude_memory/memory.db)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all quarantined beliefs without interaction",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show quarantine reason summary",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: Database not found at {args.db}", file=sys.stderr)
        sys.exit(1)

    if args.stats:
        _stats(args.db)
    elif args.list:
        _list(args.db)
    else:
        _interactive(args.db)


if __name__ == "__main__":
    _main()
