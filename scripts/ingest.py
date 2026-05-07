#!/usr/bin/env python3
"""
ingest.py
---------
Unified ingestion pipeline for the Claude Memory System.

Replaces the 5-command manual sequence with a single entry point.
Designed to be approachable for new users on GitHub.

USAGE
-----
Scan mode (recommended at session close):
    python3 ~/claude_memory/scripts/ingest.py

Process one specific file:
    python3 ~/claude_memory/scripts/ingest.py bobby_2026_04_19_001.md

Process one file with a date override (if filename has wrong date):
    python3 ~/claude_memory/scripts/ingest.py bobby_2026_04_19_001.md --date 2026-04-18

Scan and process all unprocessed files without prompting (automation):
    python3 ~/claude_memory/scripts/ingest.py --scan

Preview what would be processed without doing anything:
    python3 ~/claude_memory/scripts/ingest.py --dry-run

PIPELINE STEPS (run automatically in order)
--------------------------------------------
For each unprocessed file:
  1. Strip <private>...</private> blocks from content before Qwen sees it
  2. Run format_conversation.py  (only if the file is a raw .txt export)
  3. Run process_conversation.py (Qwen extraction -- takes 3-15 minutes per file)

After all files are processed:
  4. Run embed_memories.py            (embed new memory objects into vector store)
  5. Run verify_beliefs.py            (belief verification pass, requires Ollama)
  6. Run refresh_recent_memory.py
  7. Run refresh_deep_memory.py       (skipped if Ollama is offline)
  8. Run generate_session_prompt.py   (regenerate START_HERE.md)

PRIVATE CONTENT
---------------
Wrap any text in <private>...</private> to exclude it from memory storage.
The original file is never modified. A temporary stripped copy is processed
and deleted automatically. Example:

    **Bobby:** <private>My API key is sk-1234...</private> Does this look right?

The entire <private> block is removed before Qwen processes the file.
Only "Does this look right?" reaches the database.

NOTES
-----
- Files are detected as "already processed" by checking the database.
  Re-running ingest.py on an already-ingested file is safe -- it will be skipped.
- Multiple unprocessed files are processed in chronological order (oldest first).
- Embedding, snapshot, and bootstrap run once at the end, not per-file.
- Logs are written to ~/claude_memory/logs/ingest_YYYY-MM-DD.log
"""

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Minimum schema version required to run ingest.py.
# If the database was migrated with migrate_schema_v2_3.py, source_hash exists.
# ingest.py hard-fails if this column is missing -- run the migration first.
REQUIRED_SCHEMA_VERSION = "v2.3"

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE       = Path.home() / "claude_memory"
SCRIPTS_DIR = _BASE / "scripts"
CONV_DIR    = _BASE / "conversations"
LOGS_DIR    = _BASE / "logs"
DB_PATH     = _BASE / "memory.db"
OLLAMA_URL  = "http://localhost:11434/api/tags"

# Filename patterns that indicate a conversation file we should consider.
# Raw .txt exports and formatted .md files are both handled.
CONV_PATTERNS = [
    r"^bobby_\d{4}_\d{2}_\d{2}_\d{3}\.md$",       # bobby_YYYY_MM_DD_NNN.md (current)
    r"^conversation_\d+\.md$",                       # conversation_NNN.md (legacy)
    r"^\w+_\d{4}_\d{2}_\d{2}_\d{3}\.md$",          # any_YYYY_MM_DD_NNN.md (other users)
]

# Raw export pattern -- these need format_conversation.py first
RAW_PATTERNS = [
    r"^conversation_\d+_raw\.txt$",
    r".*_raw\.txt$",
    r".*\.txt$",
]

LOG_RETAIN = 14   # keep 14 days of ingest logs


# ── Utilities ──────────────────────────────────────────────────────────────────

def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def datestamp():
    return datetime.now().strftime("%Y-%m-%d")


class Logger:
    def __init__(self, log_path, quiet=False):
        self.log_path = log_path
        self.quiet = quiet
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(log_path, "a", encoding="utf-8")

    def write(self, message=""):
        line = f"[{timestamp()}] {message}" if message else ""
        self._file.write(line + "\n")
        self._file.flush()
        if not self.quiet:
            print(line if message else "")

    def separator(self, char="=", width=60):
        self.write(char * width)

    def close(self):
        self._file.close()


def purge_old_logs(log_dir, retain):
    logs = sorted(log_dir.glob("ingest_*.log"))
    to_delete = logs[:-retain] if len(logs) > retain else []
    for f in to_delete:
        try:
            f.unlink()
        except OSError:
            pass


def is_ollama_running():
    try:
        import requests
        resp = requests.get(OLLAMA_URL, timeout=5)
        return resp.status_code == 200
    except Exception:
        try:
            import socket
            with socket.create_connection(("localhost", 11434), timeout=5):
                return True
        except OSError:
            return False


# ── Database helpers ───────────────────────────────────────────────────────────

def get_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def check_schema_version(log=None):
    """
    Hard-fail if the database schema is older than REQUIRED_SCHEMA_VERSION.
    Checks for the presence of source_hash column in conversations as the
    canonical indicator that migrate_schema_v2_3.py has been run.
    """
    conn = get_db()
    if conn is None:
        return  # No DB yet -- fresh install, no check needed
    try:
        cols = conn.execute("PRAGMA table_info(conversations)").fetchall()
        col_names = [c[1] for c in cols]
        if "source_hash" not in col_names:
            msg = (
                f"Schema version mismatch. ingest.py requires {REQUIRED_SCHEMA_VERSION} "
                f"but the database appears to be on v2.2.\n"
                f"Run the migration first:\n"
                f"  python3 ~/claude_memory/scripts/migrate_db.py\n"
                f"Then re-run ingest.py."
            )
            if log:
                log.write(f"FATAL: {msg}")
            else:
                print(f"FATAL: {msg}")
            sys.exit(2)
    finally:
        conn.close()


def sha256_of_file(filepath):
    """Return SHA256 hex digest of a file's raw content."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def is_already_processed(filename):
    """
    Return True if this file has already been ingested into the database.

    Detection priority:
      1. source_hash match (v2.3+): most reliable, catches renamed files
      2. source_filename match (v2.3+): fast exact match
      3. raw_export fallback: backward compatibility with v2.2 records
    """
    filepath = CONV_DIR / filename
    conn = get_db()
    if conn is None:
        return False
    try:
        # Priority 1: hash-based (most reliable)
        if filepath.exists():
            file_hash = sha256_of_file(filepath)
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_hash = ?",
                (file_hash,)
            ).fetchone()
            if row:
                return True

        # Priority 2: filename-based (v2.3 column)
        try:
            row = conn.execute(
                "SELECT id FROM conversations WHERE source_filename = ?",
                (filename,)
            ).fetchone()
            if row:
                return True
        except Exception:
            pass

        # Priority 3: legacy raw_export string (v2.2 fallback)
        row = conn.execute(
            "SELECT id FROM conversations WHERE raw_export LIKE ?",
            (f"%{filename}%",)
        ).fetchone()
        return row is not None

    except Exception:
        return False
    finally:
        conn.close()


# ── File discovery ─────────────────────────────────────────────────────────────

def is_conversation_file(filename):
    """Return True if the filename looks like a conversation file we should consider."""
    return any(re.match(p, filename) for p in CONV_PATTERNS)


def is_raw_export(filename):
    """Return True if the file needs format_conversation.py before processing."""
    return any(re.match(p, filename) for p in RAW_PATTERNS)


def extract_date_from_filename(filename):
    """
    Extract YYYY-MM-DD from a filename for sorting purposes.
    Falls back to the filename itself so sorting still works.
    """
    match = re.search(r"(\d{4})_(\d{2})_(\d{2})", filename)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return filename


def scan_for_unprocessed():
    """
    Scan the conversations directory for files that haven't been ingested yet.
    Returns list of filenames sorted chronologically (oldest first).
    """
    if not CONV_DIR.exists():
        return []

    candidates = []
    for f in CONV_DIR.iterdir():
        if not f.is_file():
            continue
        name = f.name
        if not is_conversation_file(name):
            continue
        if is_already_processed(name):
            continue
        candidates.append(name)

    candidates.sort(key=extract_date_from_filename)
    return candidates


def count_processed():
    """Count how many conversation files have been processed."""
    conn = get_db()
    if conn is None:
        return 0
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


# ── Private content stripping ──────────────────────────────────────────────────

PRIVATE_PATTERN = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def has_private_content(text):
    return bool(PRIVATE_PATTERN.search(text))


def strip_private_content(text):
    """
    Remove all <private>...</private> blocks from text.
    Replaced with a placeholder so Qwen knows content was intentionally omitted.
    """
    return PRIVATE_PATTERN.sub("[private content omitted]", text)


def make_stripped_temp_file(filepath):
    """
    If the file contains <private> blocks, create a temp file with them removed.
    Returns (temp_path, was_stripped). Caller is responsible for deleting temp_path.
    If no private content, returns (None, False).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if not has_private_content(content):
        return None, False

    stripped = strip_private_content(content)
    count = len(PRIVATE_PATTERN.findall(content))

    # Write temp file next to the original so process_conversation.py
    # can find it in the conversations directory
    temp_name = f"_private_stripped_{filepath.name}"
    temp_path = filepath.parent / temp_name

    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(stripped)

    return temp_path, count


# ── Subprocess runner ──────────────────────────────────────────────────────────

def run_script(script_name, args_list, log, dry_run=False, stream=True):
    """
    Run a script from the scripts directory as a subprocess.
    Returns exit code. Streams output to log in real time if stream=True.
    args_list: list of string arguments to pass after the script path.
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        log.write(f"ERROR: script not found: {script_path}")
        return 2

    cmd = [sys.executable, "-u", str(script_path)] + args_list
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
        if stream:
            for line in proc.stdout:
                stripped = line.rstrip()
                if stripped:
                    log.write(f"  {stripped}")
        else:
            proc.stdout.read()
        proc.wait()
        return proc.returncode
    except Exception as e:
        log.write(f"ERROR: {e}")
        return 2


# ── Per-file processing ────────────────────────────────────────────────────────

def process_file(filename, log, dry_run=False, date_override=None):
    """
    Process a single conversation file through steps 1-3.
    Returns True on success, False on failure.
    """
    filepath = CONV_DIR / filename
    if not filepath.exists():
        log.write(f"ERROR: file not found: {filepath}")
        return False

    log.separator()
    log.write(f"File: {filename}")
    log.separator("-")

    # Step 1: Check for and strip private content
    temp_path = None
    process_filename = filename

    if not dry_run:
        temp_path, private_count = make_stripped_temp_file(filepath)
        if temp_path:
            log.write(f"Private content: {private_count} block(s) stripped before processing.")
            log.write(f"  Original file is unchanged.")
            process_filename = temp_path.name
        else:
            log.write("Private content: none detected.")

    # Step 2: Format conversion (only for raw .txt exports)
    if is_raw_export(filename):
        log.write("")
        log.write("Step 2/3: format_conversation.py (raw export detected)")
        rc = run_script("format_conversation.py", [filename], log, dry_run=dry_run)
        if rc != 0:
            log.write(f"format_conversation.py failed (exit code {rc}). Skipping this file.")
            if temp_path and temp_path.exists():
                temp_path.unlink()
            return False
        # After formatting, the output will be filename.replace('_raw.txt', '.md')
        process_filename = filename.replace("_raw.txt", ".md").replace(".txt", ".md")
        log.write(f"Formatted output: {process_filename}")
    else:
        log.write("Step 2/3: format check passed (file is already formatted).")

    # Step 2.5: Transcript quality validation
    # Checks for summarized entries instead of verbatim text.
    # Claude instinctively compresses transcript entries mid-session -- this
    # catches it before Qwen tries to extract from thin content.
    log.write("")
    log.write("Step 2.5/3: transcript_validator.py (verbatim quality check)")
    try:
        import importlib.util, types
        validator_path = SCRIPTS_DIR / "transcript_validator.py"
        spec = importlib.util.spec_from_file_location("transcript_validator", validator_path)
        tv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tv)

        check_path = CONV_DIR / process_filename
        if not check_path.exists():
            check_path = CONV_DIR / filename
        warnings, errors = tv.check_file(check_path)

        if errors:
            for e in errors:
                log.write(f"  FATAL: {e}")
            log.write("  Skipping file due to fatal validation error.")
            if temp_path and temp_path.exists():
                temp_path.unlink()
            return False

        if warnings:
            log.write(f"  WARNING: {len(warnings)} suspicious entry/entries found:")
            for w in warnings:
                log.write(f"    ! {w}")
            log.write("")
            log.write("  This transcript may contain summarized entries instead of verbatim text.")
            log.write("  Summarized transcripts produce fewer useful Qwen extractions.")
            log.write("")
            try:
                answer = input("  Proceed with ingestion anyway? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("y", "yes"):
                log.write("  Aborted. Fix the transcript and re-run ingest.py.")
                if temp_path and temp_path.exists():
                    temp_path.unlink()
                return False
            log.write("  Proceeding despite warnings (user confirmed).")
        else:
            log.write("  Transcript looks verbatim. OK.")

    except Exception as e:
        log.write(f"  Validator could not run ({e}). Proceeding without check.")

    # Step 3: Qwen extraction
    log.write("")
    log.write("Step 3/3: process_conversation.py (Qwen extraction)")
    log.write("  This takes 3-15 minutes. Qwen is thinking...")

    extra_args = []
    if date_override:
        extra_args = ["--date", date_override]

    try:
        rc = run_script("process_conversation.py", [process_filename] + extra_args, log, dry_run=dry_run)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                log.write(f"WARNING: could not delete temp file {temp_path}")

    if rc != 0:
        log.write(f"process_conversation.py failed (exit code {rc}).")
        return False

    log.write(f"File processed successfully: {filename}")
    return True


# ── Post-processing (embed + snapshot + bootstrap) ────────────────────────────

def run_post_processing(log, dry_run=False):
    """
    Run steps 4-6 once after all files have been processed.
    """
    log.separator()
    log.write("Post-processing: updating vector store and context files")
    log.separator("-")

    # Step 4: Embed new memory objects
    log.write("")
    log.write("Step 4/8: embed_memories.py")
    rc = run_script("embed_memories.py", [], log, dry_run=dry_run)
    if rc != 0:
        log.write(f"WARNING: embed_memories.py failed (exit code {rc}).")
        log.write("  You can run it manually: python3 ~/claude_memory/scripts/embed_memories.py")

    # Step 5: Verify beliefs against source conversations (requires Ollama / DeepSeek R1)
    log.write("")
    log.write("Step 5/8: verify_beliefs.py")
    if dry_run or is_ollama_running():
        rc = run_script("verify_beliefs.py", ["--limit", "20", "--check-contradictions"], log, dry_run=dry_run)
        if rc != 0:
            log.write(f"WARNING: verify_beliefs.py failed (exit code {rc}).")
            log.write("  You can run it manually: python3 ~/claude_memory/scripts/verify_beliefs.py --limit 20 --check-contradictions")
    else:
        log.write("  Ollama is not running. Skipping belief verification.")
        log.write("  Run manually when Ollama is available:")
        log.write("  python3 ~/claude_memory/scripts/verify_beliefs.py --limit 20 --check-contradictions")

    # Step 5.5: Belief checksum — queue research tasks for high-confidence beliefs
    # Implements the founding vision: "before a belief hardens, stress-test it
    # against external sources." Writes to research_tasks (pending or fulfilled).
    # Does not require Ollama — keyword fallback is used if embeddings unavailable.
    log.write("")
    log.write("Step 5.5/8: belief_checksum.py")
    rc = run_script("belief_checksum.py", ["--recency-days", "3"], log, dry_run=dry_run)
    if rc != 0:
        log.write(f"WARNING: belief_checksum.py failed (exit code {rc}).")
        log.write("  Run manually: python3 ~/claude_memory/scripts/belief_checksum.py")

    # Step 6: Regenerate context snapshot
    log.write("")
    log.write("Step 6/8: refresh_recent_memory.py")
    rc = run_script("refresh_recent_memory.py", [], log, dry_run=dry_run)
    if rc != 0:
        log.write(f"WARNING: refresh_recent_memory.py failed (exit code {rc}).")

    # Step 7: Regenerate bootstrap (requires Ollama)
    log.write("")
    log.write("Step 7/8: refresh_deep_memory.py")
    if dry_run or is_ollama_running():
        rc = run_script("refresh_deep_memory.py", [], log, dry_run=dry_run)
        if rc != 0:
            log.write(f"WARNING: refresh_deep_memory.py failed (exit code {rc}).")
    else:
        log.write("  Ollama is not running. Skipping bootstrap.")
        log.write("  Run manually when Ollama is available:")
        log.write("  python3 ~/claude_memory/scripts/refresh_deep_memory.py")

    # Step 8: Regenerate START_HERE.md (no Ollama required — reads DB + files only)
    # Must run last so it captures the fully updated belief/goal/tension state.
    log.write("")
    log.write("Step 8/8: generate_session_prompt.py")
    rc = run_script("generate_session_prompt.py", ["--skip-context"], log, dry_run=dry_run)
    if rc != 0:
        log.write(f"WARNING: generate_session_prompt.py failed (exit code {rc}).")
        log.write("  Run manually: python3 ~/claude_memory/scripts/generate_session_prompt.py")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest conversation files into Claude Memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 ingest.py                             # scan for unprocessed, prompt to confirm
  python3 ingest.py bobby_2026_04_19_001.md    # process one specific file
  python3 ingest.py --scan                      # process all unprocessed without prompting
  python3 ingest.py --dry-run                   # preview without running anything
        """
    )
    parser.add_argument(
        "filename", nargs="?", default=None,
        help="Specific conversation file to process (e.g. bobby_2026_04_19_001.md)"
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan for all unprocessed files and ingest them without prompting"
    )
    parser.add_argument(
        "--date", default=None,
        help="Override session date (YYYY-MM-DD). Use if the filename has the wrong date."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be processed without actually running anything"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress terminal output (log file is still written)"
    )
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"ingest_{datestamp()}.log"
    log = Logger(log_path, quiet=args.quiet)

    log.separator()
    log.write("Claude Memory -- Ingest Pipeline")
    if args.dry_run:
        log.write("Mode: DRY RUN (nothing will be written)")
    log.separator()
    log.write("")

    # Schema version check -- hard fail before doing anything else
    if not args.dry_run:
        check_schema_version(log)

    # Determine which files to process
    files_to_process = []

    if args.filename:
        # Single file mode
        target = args.filename
        # Accept bare filename or path; strip to basename
        target = Path(target).name

        if is_already_processed(target) and not args.dry_run:
            log.write(f"'{target}' has already been ingested. Nothing to do.")
            log.write("To re-process a file, delete its record from the conversations table first.")
            log.close()
            sys.exit(0)

        files_to_process = [target]

    else:
        # Scan mode
        log.write("Scanning conversations/ for unprocessed files...")
        unprocessed = scan_for_unprocessed()
        already_done = count_processed()

        log.write(f"  Already ingested : {already_done} file(s)")
        log.write(f"  Unprocessed found: {len(unprocessed)} file(s)")

        if not unprocessed:
            log.write("")
            log.write("Everything is up to date. Nothing to ingest.")
            log.close()
            sys.exit(0)

        log.write("")
        log.write("Files to process (chronological order):")
        for i, f in enumerate(unprocessed, 1):
            date = extract_date_from_filename(f)
            log.write(f"  {i}. {f}  ({date})")
        log.write("")

        if not args.scan and not args.dry_run:
            # Interactive prompt
            try:
                answer = input("Proceed with ingestion? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("", "y", "yes"):
                log.write("Aborted by user.")
                log.close()
                sys.exit(0)
            log.write("")

        files_to_process = unprocessed

    # Process each file
    succeeded = []
    failed = []

    for filename in files_to_process:
        # Only pass date_override in single-file mode (doesn't make sense in batch)
        date_override = args.date if args.filename else None
        ok = process_file(filename, log, dry_run=args.dry_run, date_override=date_override)
        if ok:
            succeeded.append(filename)
        else:
            failed.append(filename)

    # Post-processing (runs once at end)
    if succeeded or args.dry_run:
        log.write("")
        run_post_processing(log, dry_run=args.dry_run)

    # Final summary
    log.write("")
    log.separator()
    log.write("SUMMARY")
    log.separator("-")
    if succeeded:
        log.write(f"Successfully ingested ({len(succeeded)}):")
        for f in succeeded:
            log.write(f"  + {f}")
    if failed:
        log.write(f"Failed ({len(failed)}):")
        for f in failed:
            log.write(f"  x {f}")
    if not succeeded and not failed and args.dry_run:
        log.write("Dry run complete. No changes made.")

    log.write(f"Log: {log_path}")
    log.separator()

    purge_old_logs(LOGS_DIR, LOG_RETAIN)
    log.close()

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
