#!/usr/bin/env python3
"""
ingest_agent.py
---------------
Ingest Agent — Agent 3 of 3 in the CoWork Memory agent stack.

Runs daily (default 11am via launchd). Finds all scout_results rows marked
"interesting", runs each through process_research.py to extract concepts,
beliefs, patterns, and epiphanies, then runs embed_memories.py to index
the new chunks into semantic memory.

This agent is the automated half of the hybrid ingest workflow:
  - During a session: Claude surfaces research, you approve, Claude marks interesting.
  - Overnight (or on next wake): this agent ingests everything marked interesting.
  - Next session: the content is in memory and shows up in semantic retrieval.

BEHAVIOR
--------
  - Checks Ollama is reachable before doing anything.
  - If Ollama is offline: logs a warning and exits cleanly (exit code 1).
  - Finds all scout_results with status='interesting', ordered by relevance_score DESC.
  - For each: formats abstract as a temp .txt file, calls process_research.py,
    marks row as ingested, cleans up temp file.
  - After all ingests: runs embed_memories.py to index new chunks.
  - Startup jitter: random 0-300 second delay to avoid wake pileup with other agents.
    Skip with --no-jitter when running manually.
  - Logs to ~/claude_memory/logs/ingest_agent_YYYY-MM-DD.log (7-day rotation).

MACHINE SLEEP VS OFF
--------------------
  Sleep: launchd fires this agent on the next wake after the scheduled time.
  Off:   launchd drops the missed window. Agent runs at the next scheduled time.
  Manual: run anytime with --no-jitter to process the queue immediately.

USAGE
-----
    python3 ~/claude_memory/scripts/ingest_agent.py
    python3 ~/claude_memory/scripts/ingest_agent.py --no-jitter
    python3 ~/claude_memory/scripts/ingest_agent.py --dry-run
    python3 ~/claude_memory/scripts/ingest_agent.py --quiet

EXIT CODES
----------
    0  All queued items ingested and embedded successfully.
    1  Ollama not available — nothing processed.
    2  Partial success — some items failed; others ingested.
    3  No items queued — nothing to do.
"""

import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE        = Path.home() / "claude_memory"
SCRIPTS_DIR  = _BASE / "scripts"
LOGS_DIR     = _BASE / "logs"
CACHE_DIR    = _BASE / "cache"
DB_PATH      = _BASE / "memory.db"
OLLAMA_URL   = "http://localhost:11434/api/tags"
LOG_RETAIN   = 7
JITTER_MAX   = 300   # seconds — max random startup delay


# ── Logging ────────────────────────────────────────────────────────────────────

class Log:
    def __init__(self, quiet: bool = False):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._date   = datetime.now().strftime("%Y-%m-%d")
        self._path   = LOGS_DIR / f"ingest_agent_{self._date}.log"
        self._fh     = self._path.open("a", encoding="utf-8")
        self._quiet  = quiet

    def write(self, msg: str = ""):
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}" if msg.strip() else ""
        self._fh.write(line + "\n")
        self._fh.flush()
        if not self._quiet:
            print(line if line else "")

    def sep(self, char: str = "=", width: int = 60):
        self.write(char * width)

    def close(self):
        self._fh.close()
        self._rotate()

    def _rotate(self):
        logs = sorted(LOGS_DIR.glob("ingest_agent_*.log"))
        for old in logs[:-LOG_RETAIN]:
            try:
                old.unlink()
            except OSError:
                pass


# ── Ollama check ───────────────────────────────────────────────────────────────

def ollama_is_running() -> bool:
    if REQUESTS_AVAILABLE:
        try:
            r = requests.get(OLLAMA_URL, timeout=5)
            return r.status_code == 200
        except Exception:
            return False
    # Fallback: curl
    try:
        result = subprocess.run(
            ["curl", "-sf", OLLAMA_URL],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Format scout result as research text ───────────────────────────────────────

def _format_as_research_text(row: dict) -> str:
    """Format a scout_results row as plain text for process_research.py."""
    title       = row.get("title")        or "Untitled"
    abstract    = row.get("abstract")     or "(no abstract available)"
    source_name = row.get("source_name")  or "unknown"
    source_url  = row.get("source_url")   or ""
    pub_date    = row.get("publication_date") or "unknown"
    score       = row.get("relevance_score", 0.0)
    doi         = row.get("doi")          or ""
    tags        = row.get("tags")         or ""

    try:
        authors_list = json.loads(row.get("authors") or "[]")
        authors_str  = ", ".join(authors_list) if authors_list else "Unknown"
    except (json.JSONDecodeError, TypeError):
        authors_str  = row.get("authors") or "Unknown"

    lines = [
        f"# Research Paper: {title}",
        f"**Source:** {source_name}",
        f"**URL:** {source_url}",
        f"**Authors:** {authors_str}",
        f"**Publication date:** {pub_date}",
        f"**DOI:** {doi}",
        f"**Relevance score:** {score:.3f}",
        f"**Tags:** {tags}",
        f"**Fetched:** {row.get('date_fetched', 'unknown')}",
        "",
        "## Abstract",
        "",
        abstract,
    ]
    return "\n".join(lines) + "\n"


# ── Core ingest logic ──────────────────────────────────────────────────────────

def fetch_queued(conn: sqlite3.Connection) -> list[dict]:
    """Return all scout_results rows with status='interesting', best first."""
    rows = conn.execute("""
        SELECT id, title, abstract, source_url, source_name, authors,
               relevance_score, doi, tags, date_fetched, publication_date
        FROM scout_results
        WHERE status = 'interesting'
        ORDER BY relevance_score DESC
    """).fetchall()
    keys = ["id", "title", "abstract", "source_url", "source_name", "authors",
            "relevance_score", "doi", "tags", "date_fetched", "publication_date"]
    return [dict(zip(keys, r)) for r in rows]


def ingest_one(row: dict, conn: sqlite3.Connection,
               log: Log, dry_run: bool = False) -> bool:
    """
    Format a scout result, run process_research.py, mark ingested.
    Returns True on success, False on failure.
    """
    rid   = row["id"]
    title = row.get("title") or "Untitled"
    now   = datetime.now().isoformat()

    log.write(f"  [{rid}] {title[:70]}")

    if dry_run:
        log.write(f"       DRY RUN — would format and call process_research.py")
        return True

    # Atomically claim this item to prevent duplicate processing by concurrent agents
    cur = conn.execute(
        "UPDATE scout_results SET status='processing' WHERE id=? AND status='interesting'",
        (rid,)
    )
    conn.commit()
    if cur.rowcount == 0:
        log.write(f"       SKIPPED — already claimed by another process.")
        return False

    # Write temp file
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in title[:40]
    ).strip("_").lower()
    tmp_path = CACHE_DIR / f"scout_{rid}_{slug}.txt"
    tmp_path.write_text(_format_as_research_text(row))

    # Run process_research.py
    script = SCRIPTS_DIR / "process_research.py"
    if not script.exists():
        log.write(f"       ERROR: process_research.py not found at {script}")
        tmp_path.unlink(missing_ok=True)
        return False

    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=3600,
    )

    if result.returncode != 0:
        log.write(f"       FAILED (exit {result.returncode})")
        if result.stderr:
            for line in result.stderr.strip().split("\n")[-5:]:
                log.write(f"       {line}")
        tmp_path.unlink(missing_ok=True)
        conn.execute(
            "UPDATE scout_results SET status='interesting' WHERE id=? AND status='processing'",
            (rid,)
        )
        conn.commit()
        return False

    # Mark as ingested
    conn.execute("""
        UPDATE scout_results
        SET status='ingested', promoted_to='memory_chunks',
            reviewed_at=?, updated_at=?,
            curator_notes=COALESCE(curator_notes || ' | ', '') ||
                          'Auto-ingested by ingest_agent.py'
        WHERE id=?
    """, (now, now, rid))
    conn.commit()

    tmp_path.unlink(missing_ok=True)
    log.write(f"       OK — ingested and marked.")
    return True


def run_embed(log: Log, dry_run: bool = False) -> bool:
    """Run embed_memories.py to index new chunks."""
    script = SCRIPTS_DIR / "embed_memories.py"
    if not script.exists():
        log.write("WARNING: embed_memories.py not found — skipping embedding step.")
        return False

    log.write("Running embed_memories.py...")

    if dry_run:
        log.write("  DRY RUN — would run embed_memories.py")
        return True

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=3600,
    )

    if result.returncode == 0:
        # Surface the last few lines of output for the log
        for line in result.stdout.strip().split("\n")[-5:]:
            if line.strip():
                log.write(f"  {line}")
        log.write("Embedding complete.")
        return True
    else:
        log.write(f"WARNING: embed_memories.py exited with code {result.returncode}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    log = Log(quiet=args.quiet)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log.sep()
    log.write(f"Ingest Agent  —  {now}")
    log.write(f"dry_run={args.dry_run}  no_jitter={args.no_jitter}")
    log.sep()

    # ── Ollama check ──
    if args.dry_run:
        log.write("DRY RUN — skipping Ollama availability check.")
    else:
        log.write("Checking Ollama availability...")
        if not ollama_is_running():
            log.write("Ollama is not running. Nothing to ingest.")
            log.write("Start Ollama and re-run, or wait for the next scheduled window.")
            log.write("Manual run: python3 ~/claude_memory/scripts/ingest_agent.py --no-jitter")
            log.sep()
            log.close()
            return 1
        log.write("Ollama is running.")
    log.write("")

    # ── Load queue ──
    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        log.write(f"ERROR: database not found at {db_path}")
        log.close()
        return 1

    conn = sqlite3.connect(db_path)
    queued = fetch_queued(conn)

    if not queued:
        log.write("No items queued for ingest (status=interesting). Nothing to do.")
        log.sep()
        log.close()
        conn.close()
        return 3

    log.write(f"Found {len(queued)} item(s) queued for ingest:")
    for row in queued:
        log.write(f"  id={row['id']} score={row['relevance_score']:.3f} | {row['title'][:60]}")
    log.write("")

    # ── Ingest each ──
    succeeded, failed = [], []
    for row in queued:
        ok = ingest_one(row, conn, log, dry_run=args.dry_run)
        (succeeded if ok else failed).append(row["id"])

    log.write("")
    log.write(f"Ingest results: {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        log.write(f"  Failed IDs: {failed}")
        log.write("  Failed items remain marked 'interesting' for retry next run.")
    log.write("")

    conn.close()

    # ── Embed ──
    if succeeded:
        run_embed(log, dry_run=args.dry_run)
    else:
        log.write("No successful ingests — skipping embed step.")

    log.write("")
    exit_code = 0 if not failed else 2
    log.write(f"Done. Exit code: {exit_code}")
    log.sep()
    log.close()
    return exit_code


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest Agent — auto-ingest approved scout results")
    p.add_argument("--dry-run",   action="store_true",
                   help="Show what would be ingested without running process_research.py")
    p.add_argument("--no-jitter", action="store_true",
                   help="Skip startup jitter delay (use when running manually)")
    p.add_argument("--quiet",     action="store_true",
                   help="Suppress stdout output; log file still written")
    p.add_argument("--db",        type=str, default=None,
                   help="Path to memory.db (default: ~/claude_memory/memory.db)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if not args.no_jitter and not args.dry_run:
        delay = random.randint(0, JITTER_MAX)
        time.sleep(delay)
    sys.exit(run(args))
