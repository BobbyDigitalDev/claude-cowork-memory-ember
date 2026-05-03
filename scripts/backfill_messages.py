#!/usr/bin/env python3
"""
backfill_messages.py
--------------------
Populates the messages table for conversations that were ingested before
_write_messages() was added to process_conversation.py.

For each conversation in the conversations table that has no messages rows,
this script:
  1. Finds the source .md file in ~/claude_memory/conversations/
  2. Parses **Bobby:** / **Claude:** speaker blocks
  3. Inserts each message into the messages table

Safe to re-run: content_hash deduplication prevents double inserts.

USAGE
-----
    python3 ~/claude_memory/scripts/backfill_messages.py
    python3 ~/claude_memory/scripts/backfill_messages.py --dry-run
    python3 ~/claude_memory/scripts/backfill_messages.py --all   # include convs that already have messages
"""

import sqlite3
import hashlib
import uuid
import re
import sys
import argparse
from pathlib import Path
from datetime import datetime

BASE      = Path.home() / "claude_memory"
DB_PATH   = BASE / "memory.db"
CONV_DIR  = BASE / "conversations"


def _read_username() -> str:
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()


def parse_messages(text):
    """
    Split conversation text into (speaker, content) pairs.
    Handles **Bobby:** and **Claude:** markers.
    Returns list of (speaker_str, content_str).
    """
    pattern = re.compile(r'\*\*(' + re.escape(USERNAME) + r'|Claude)\*\*\s*:', re.IGNORECASE)
    parts   = pattern.split(text)
    messages = []
    idx = 1
    while idx < len(parts) - 1:
        speaker = parts[idx].strip()
        content = parts[idx + 1].strip()
        if content:
            messages.append((speaker, content))
        idx += 2
    return messages


def find_conv_file(conv_id, conv_date, conn):
    """
    Try to find the source .md file for a conversation_id.
    Strategy: look for files matching USERNAME_YYYY_MM_DD_NNN.md where date matches.
    Falls back to any .md file if conv_id matches (some older files use sequential IDs).
    """
    date_str = (conv_date or "")[:10].replace("-", "_")
    # Try date-based match first
    if date_str:
        candidates = sorted(CONV_DIR.glob(f"{USERNAME}_{date_str}_*.md"))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Pick the one whose serial matches conv_id last digit(s) if possible
            for f in candidates:
                stem = f.stem  # e.g. alice_2026_04_12_001
                parts = stem.split("_")
                try:
                    serial = int(parts[-1])
                    if serial == conv_id:
                        return f
                except (ValueError, IndexError):
                    pass
            return candidates[0]  # fallback to first

    # Try conversation_NNN.md
    numbered = CONV_DIR / f"conversation_{conv_id:03d}.md"
    if numbered.exists():
        return numbered

    # Try USERNAME_*.md ordered by modification time and pick by index
    all_md = sorted(CONV_DIR.glob(f"{USERNAME}_*.md"))
    if conv_id - 1 < len(all_md):
        return all_md[conv_id - 1]

    return None


def backfill_conversation(c, conv_id, conv_date, filepath, dry_run):
    text = filepath.read_text(encoding="utf-8", errors="replace")
    messages = parse_messages(text)
    if not messages:
        print(f"  [conv {conv_id}] No speaker blocks found in {filepath.name}")
        return 0

    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = (conv_date or "")[:10] or datetime.now().strftime("%Y-%m-%d")

    inserted = 0
    for msg_index, (speaker, content) in enumerate(messages):
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        existing = c.execute(
            "SELECT id FROM messages WHERE content_hash = ? AND conversation_id = ?",
            (content_hash, conv_id)
        ).fetchone()
        if existing:
            continue

        if not dry_run:
            c.execute("""
                INSERT INTO messages
                    (uuid, conversation_id, timestamp, content, content_hash,
                     token_count, message_index, source_type, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(uuid.uuid4()),
                conv_id,
                today,
                content,
                content_hash,
                max(1, len(content) // 4),
                msg_index,
                "conversation",
                speaker.lower(),
                now,
            ))
        inserted += 1

    return inserted


def main():
    ap = argparse.ArgumentParser(description="Backfill messages table from existing conversation files")
    ap.add_argument("--dry-run", action="store_true", help="Report counts without writing")
    ap.add_argument("--all",     action="store_true", help="Process all conversations, even those with existing messages")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    # Get conversations with no messages rows (or all if --all)
    if args.all:
        rows = c.execute("SELECT id, date FROM conversations ORDER BY id").fetchall()
    else:
        rows = c.execute("""
            SELECT c.id, c.date FROM conversations c
            WHERE c.id NOT IN (SELECT DISTINCT conversation_id FROM messages WHERE conversation_id IS NOT NULL)
            ORDER BY c.id
        """).fetchall()

    if not rows:
        print("No conversations need backfilling.")
        conn.close()
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Backfilling {len(rows)} conversation(s)...")
    print()

    total_inserted = 0
    total_missed   = 0

    for conv_id, conv_date in rows:
        filepath = find_conv_file(conv_id, conv_date, conn)
        if not filepath or not filepath.exists():
            print(f"  [conv {conv_id}] file not found (date={conv_date}) -- skipping")
            total_missed += 1
            continue

        n = backfill_conversation(c, conv_id, conv_date, filepath, args.dry_run)
        print(f"  [conv {conv_id}] {filepath.name}: {n} message(s) inserted")
        total_inserted += n

    if not args.dry_run:
        conn.commit()

    conn.close()
    print()
    print(f"Done. {total_inserted} message(s) inserted, {total_missed} conversation(s) without source file.")
    if args.dry_run:
        print("(dry run -- nothing written)")


if __name__ == "__main__":
    main()
