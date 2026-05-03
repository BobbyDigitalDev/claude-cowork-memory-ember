#!/usr/bin/env python3
"""
auto_ingest.py
--------------
Triggered automatically by launchd WatchPaths whenever the conversations/
directory changes. Acts as a debounced gate before running ingest.py --scan.

The problem it solves:
  ingest.py --scan should run once at the END of a session, not on every
  transcript append. Since transcripts are written incrementally during a
  session, WatchPaths fires many times while the session is still active.
  This script checks how long it has been since the most recently modified
  .md file was last touched. If it was modified within the DEBOUNCE_MINUTES
  window, the session is probably still live — exit without running ingest.
  If the last modification is older than the debounce window, the session
  is done — run ingest.py --scan.

Debounce:
  Default window: 15 minutes. Configurable via --debounce.

Usage (called by launchd, not usually run manually):
    python3 ~/claude_memory/scripts/auto_ingest.py
    python3 ~/claude_memory/scripts/auto_ingest.py --debounce 20
    python3 ~/claude_memory/scripts/auto_ingest.py --dry-run

Manual trigger (force ingest regardless of debounce):
    python3 ~/claude_memory/scripts/auto_ingest.py --force
"""

import os
import sys
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

CONV_DIR       = Path.home() / "claude_memory/conversations"
INGEST_SCRIPT  = Path.home() / "claude_memory/scripts/ingest.py"
LOCK_FILE      = Path.home() / "claude_memory/logs/auto_ingest.lock"
LOG_FILE       = Path.home() / "claude_memory/logs/auto_ingest.log"
DEBOUNCE_MIN   = 15  # minutes of silence before treating session as complete


def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def most_recent_md_mtime():
    """Return the mtime (seconds since epoch) of the most recently modified
    .md file in CONV_DIR, or 0 if none found."""
    mtimes = [
        f.stat().st_mtime
        for f in CONV_DIR.glob("*.md")
        if f.is_file()
    ]
    return max(mtimes) if mtimes else 0


def is_locked():
    """Return True if an ingest is already running (lock file present and PID alive)."""
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        # Check if PID is still alive
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        # Lock file stale
        LOCK_FILE.unlink(missing_ok=True)
        return False


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def run_ingest(dry_run=False):
    """Run ingest.py --scan. Returns True on success."""
    cmd = [sys.executable, str(INGEST_SCRIPT), "--scan"]
    if dry_run:
        cmd.append("--dry-run")

    log(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=False, text=True)
        if result.returncode == 0:
            log("ingest.py --scan completed successfully.")
            return True
        else:
            log(f"ingest.py exited with code {result.returncode}.")
            return False
    except Exception as e:
        log(f"ERROR running ingest.py: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Debounced auto-trigger for ingest.py --scan"
    )
    parser.add_argument("--debounce", type=int, default=DEBOUNCE_MIN,
                        help=f"Minutes of inactivity before triggering ingest (default: {DEBOUNCE_MIN})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass --dry-run to ingest.py (no DB writes)")
    parser.add_argument("--force", action="store_true",
                        help="Skip debounce check and run ingest immediately")
    args = parser.parse_args()

    log("auto_ingest.py triggered by WatchPaths")

    # ── Guard: already running ────────────────────────────────────────────────
    if is_locked():
        log("Ingest already in progress (lock file active). Exiting.")
        sys.exit(0)

    # Acquire lock immediately after the check to close the TOCTOU window.
    # sys.exit() raises SystemExit so the finally block always runs and releases it.
    acquire_lock()
    try:
        # ── Debounce check ────────────────────────────────────────────────────
        if not args.force:
            mtime     = most_recent_md_mtime()
            age_secs  = time.time() - mtime
            age_min   = age_secs / 60

            if mtime == 0:
                log("No .md files found in conversations/. Nothing to ingest.")
                sys.exit(0)

            if age_min < args.debounce:
                log(f"Most recent transcript modified {age_min:.1f} min ago "
                    f"(debounce: {args.debounce} min). Session still active — skipping.")
                sys.exit(0)

            log(f"Last transcript modification: {age_min:.1f} min ago. "
                f"Session appears complete. Proceeding with ingest.")
        else:
            log("--force flag set. Skipping debounce check.")

        # ── Run ingest ────────────────────────────────────────────────────────
        run_ingest(dry_run=args.dry_run)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
