#!/usr/bin/env python3
"""
context_snapshot_agent.py
Agent 1 of 3 in the Claude Memory agent stack.

Runs refresh_recent_memory.py and refresh_deep_memory.py in sequence.
Designed to be called on a schedule (launchd), at session start, or manually.

Behavior:
  - Always runs refresh_recent_memory.py (reads DB only, no Ollama required)
  - Checks if Ollama is reachable before running refresh_deep_memory.py
  - If Ollama is offline: logs a warning, skips bootstrap, exits cleanly
  - Writes timestamped log to ~/claude_memory/logs/context_agent_YYYY-MM-DD.log
  - Keeps the 7 most recent log files, deletes older ones

Usage:
    python3 ~/claude_memory/scripts/context_snapshot_agent.py
    python3 ~/claude_memory/scripts/context_snapshot_agent.py --no-bootstrap
    python3 ~/claude_memory/scripts/context_snapshot_agent.py --dry-run
    python3 ~/claude_memory/scripts/context_snapshot_agent.py --quiet

Options:
    --no-bootstrap    Skip refresh_deep_memory.py even if Ollama is online
    --dry-run         Print what would run without actually running anything
    --quiet           Suppress stdout; log file is still written

Exit codes:
    0  Full success (both scripts ran and succeeded)
    1  Partial success (snapshot updated, bootstrap skipped: Ollama offline or --no-bootstrap)
    2  Failure (refresh_recent_memory.py failed)
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# Paths
_BASE       = Path.home() / "claude_memory"
SCRIPTS_DIR = _BASE / "scripts"
LOGS_DIR    = _BASE / "logs"
OLLAMA_URL  = "http://localhost:11434/api/tags"
LOG_RETAIN  = 7   # keep this many daily log files


def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def datestamp():
    return datetime.now().strftime("%Y-%m-%d")


class Logger:
    def __init__(self, log_path, quiet=False):
        self.log_path = log_path
        self.quiet = quiet
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.log_path, "a", encoding="utf-8")

    def write(self, message):
        line = f"[{timestamp()}] {message}"
        self._file.write(line + "\n")
        self._file.flush()
        if not self.quiet:
            print(line)

    def close(self):
        self._file.close()

    def separator(self):
        self.write("=" * 60)


def purge_old_logs(log_dir, retain):
    """Delete log files beyond the most recent N, sorted by name (YYYY-MM-DD order)."""
    logs = sorted(log_dir.glob("context_agent_*.log"))
    to_delete = logs[:-retain] if len(logs) > retain else []
    for f in to_delete:
        try:
            f.unlink()
        except OSError:
            pass
    return len(to_delete)


def is_ollama_running():
    """Return True if Ollama is reachable at localhost:11434."""
    if not REQUESTS_AVAILABLE:
        # Fall back to a socket check if requests is not installed
        import socket
        try:
            with socket.create_connection(("localhost", 11434), timeout=5):
                return True
        except OSError:
            return False
    try:
        resp = requests.get(OLLAMA_URL, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def run_script(script_name, log, dry_run=False):
    """
    Run a script in the scripts directory as a subprocess.
    Streams stdout/stderr to the log in real time.
    Returns the exit code.
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        log.write(f"ERROR: script not found: {script_path}")
        return 2

    cmd = [sys.executable, str(script_path)]
    log.write(f"Running: {' '.join(cmd)}")

    if dry_run:
        log.write("  (dry-run: skipped)")
        return 0

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(_BASE)
        )
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                log.write(f"  {stripped}")
        proc.wait()
        return proc.returncode
    except Exception as e:
        log.write(f"ERROR running {script_name}: {e}")
        return 2


def main():
    parser = argparse.ArgumentParser(
        description="Context Snapshot Agent: refresh recent_memory.md and deep_memory.md"
    )
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="Skip refresh_deep_memory.py regardless of Ollama status")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would run without executing")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress stdout (log file is still written)")
    args = parser.parse_args()

    log_path = LOGS_DIR / f"context_agent_{datestamp()}.log"
    log = Logger(log_path, quiet=args.quiet)

    try:
        log.separator()
        log.write("Context Snapshot Agent starting")
        if args.dry_run:
            log.write("Mode: DRY RUN")
        log.separator()

        exit_code = 0

        # Step 1: refresh_recent_memory.py (always runs)
        log.write("Step 1/2: refresh_recent_memory.py")
        rc = run_script("refresh_recent_memory.py", log, dry_run=args.dry_run)
        if rc != 0:
            log.write(f"FAILED (exit code {rc}). Aborting.")
            log.separator()
            sys.exit(2)
        log.write("Step 1/2 complete.")
        log.write("")

        # Step 2: refresh_deep_memory.py (requires Ollama)
        if args.no_bootstrap:
            log.write("Step 2/2: refresh_deep_memory.py SKIPPED (--no-bootstrap flag)")
            exit_code = 1
        else:
            log.write("Step 2/2: refresh_deep_memory.py")
            log.write("  Checking Ollama availability...")
            if args.dry_run or is_ollama_running():
                if not args.dry_run:
                    log.write("  Ollama is online.")
                rc = run_script("refresh_deep_memory.py", log, dry_run=args.dry_run)
                if rc != 0:
                    log.write(f"  refresh_deep_memory.py failed (exit code {rc}).")
                    log.write("  recent_memory.md is current. deep_memory.md may be stale.")
                    exit_code = 1
                else:
                    log.write("Step 2/2 complete.")
            else:
                log.write("  WARNING: Ollama is not running. Skipping bootstrap.")
                log.write("  recent_memory.md is current. deep_memory.md was NOT updated.")
                log.write("  To update bootstrap manually: python3 ~/claude_memory/scripts/refresh_deep_memory.py")
                exit_code = 1

        # Purge old logs
        deleted = purge_old_logs(LOGS_DIR, LOG_RETAIN)
        if deleted > 0:
            log.write(f"Purged {deleted} old log file(s) (keeping {LOG_RETAIN} most recent).")

        log.separator()
        status = "SUCCESS" if exit_code == 0 else "PARTIAL (see log)"
        log.write(f"Context Snapshot Agent finished. Status: {status}")
        log.write(f"Log: {log_path}")
        log.separator()
    finally:
        log.close()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
