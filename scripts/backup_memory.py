#!/usr/bin/env python3
"""
backup_memory.py
----------------
Creates a timestamped backup of memory.db to ~/claude_memory/backups/.
Runs every 6 hours via launchd (com.ember-engine.backup-agent.plist).

Behavior:
  - Copies memory.db to backups/memory_YYYY-MM-DD_HHMMSS.db
  - Keeps the most recent MAX_BACKUPS copies; deletes older ones
  - Checks available disk space before writing; aborts if < MIN_FREE_MB
  - Logs to ~/claude_memory/logs/backup_agent_stdout.log (via launchd redirect)
  - Exits 0 on success, 1 on failure

USAGE
-----
    python3 ~/claude_memory/scripts/backup_memory.py
    python3 ~/claude_memory/scripts/backup_memory.py --dry-run
    python3 ~/claude_memory/scripts/backup_memory.py --max-backups 20
"""

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

_BASE        = Path.home() / "claude_memory"
DB_PATH      = _BASE / "memory.db"
BACKUP_DIR   = _BASE / "backups"
MAX_BACKUPS  = 10     # keep this many timestamped copies
MIN_FREE_MB  = 200    # abort if less than this much disk space is free


# ── Helpers ────────────────────────────────────────────────────────────────────

def free_mb(path: Path) -> float:
    stat = shutil.disk_usage(path)
    return stat.free / (1024 * 1024)


def existing_backups() -> list[Path]:
    """Return existing backups sorted oldest-first."""
    return sorted(BACKUP_DIR.glob("memory_*.db"))


def prune(max_keep: int, dry_run: bool):
    """Delete oldest backups so at most max_keep remain (after the new one is written)."""
    backups = existing_backups()
    to_delete = backups[:max(0, len(backups) - max_keep + 1)]
    for p in to_delete:
        if dry_run:
            print(f"  [DRY RUN] Would delete: {p.name}")
        else:
            p.unlink()
            print(f"  Pruned: {p.name}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run(args):
    now    = datetime.now()
    stamp  = now.strftime("%Y-%m-%d_%H%M%S")
    dest   = BACKUP_DIR / f"memory_{stamp}.db"

    print(f"\n{'='*60}")
    print(f"Memory Backup Agent")
    print(f"Started: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # ── Preflight ─────────────────────────────────────────────────────────────
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    db_mb = DB_PATH.stat().st_size / (1024 * 1024)
    free  = free_mb(BACKUP_DIR)
    print(f"DB size:    {db_mb:.1f} MB")
    print(f"Free space: {free:.0f} MB")

    if free < MIN_FREE_MB:
        print(f"ERROR: Less than {MIN_FREE_MB} MB free. Aborting to protect disk.")
        sys.exit(1)

    # ── Prune old backups first ────────────────────────────────────────────────
    prune(args.max_backups, args.dry_run)

    # ── Copy ──────────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"[DRY RUN] Would copy memory.db → {dest.name}")
    else:
        shutil.copy2(str(DB_PATH), str(dest))
        dest_mb = dest.stat().st_size / (1024 * 1024)
        print(f"Backup written: {dest.name} ({dest_mb:.1f} MB)")

    # ── Summary ───────────────────────────────────────────────────────────────
    backups_after = existing_backups()
    print(f"Backups on disk: {len(backups_after)} (max: {args.max_backups})")
    if backups_after:
        oldest = backups_after[0].name.replace("memory_", "").replace(".db", "")
        print(f"Oldest retained: {oldest}")

    print(f"\nDone. {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backup memory.db to backups/")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Show what would happen without writing anything")
    parser.add_argument("--max-backups", type=int, default=MAX_BACKUPS,
                        help=f"Number of backups to retain (default: {MAX_BACKUPS})")
    args = parser.parse_args()
    run(args)
