#!/usr/bin/env python3
"""
doctor.py
---------
System health check for ember-engine. Diagnoses the full installation:
Python environment, dependencies, Ollama, models, database, schema,
launchd agents, processing queue, and embedding coverage.

Run any time you suspect something is wrong, or before opening a support
issue or filing a bug report.

Usage:
    python3 ~/claude_memory/scripts/doctor.py
    python3 ~/claude_memory/scripts/doctor.py --quiet     # failures only
    python3 ~/claude_memory/scripts/doctor.py --json      # machine-readable

Exit code:
    0  all checks passed
    1  one or more failures
"""

import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE = Path.home() / "claude_memory"

def _find_db() -> Path:
    standard = _BASE / "memory.db"
    if standard.exists():
        return standard
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
    return standard

DB_PATH    = _find_db()
DAEMONS    = _BASE / "daemons"
OLLAMA_URL = "http://localhost:11434"

REQUIRED_PACKAGES = ["requests", "mcp"]
REQUIRED_MODELS   = ["nomic-embed-text", "qwen2.5:14b", "deepseek-r1:14b"]
OPTIONAL_MODELS   = []

CRITICAL_SCHEMA = [
    ("processing_jobs", ["id", "source_file", "status", "created_at", "call_name"]),
    ("goals",           ["id", "description", "category", "status", "priority"]),
    ("beliefs",         ["id", "topic", "is_active", "confidence_score", "status"]),
    ("questions",       ["id", "question", "status", "category"]),
    ("sessions",        ["id", "date"]),
    ("memory_chunks",   ["id", "content", "embedding_vector"]),
]
OPTIONAL_SCHEMA = [
    ("scout_results", ["id", "title", "source_name", "relevance_score", "status"]),
]

AGENT_PLISTS = [
    "com.ember-engine.context-agent.plist",
    "com.ember-engine.research-scout.plist",
    "com.ember-engine.ingest-agent.plist",
    "com.ember-engine.reflection-agent.plist",
    "com.ember-engine.backup-agent.plist",
]
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"


# ── Result tracking ────────────────────────────────────────────────────────────

class Results:
    def __init__(self, quiet=False):
        self.items   = []    # list of (section, label, status, detail)
        self.quiet   = quiet
        self.failures = 0
        self.warnings = 0

    def ok(self, section, label, detail=""):
        self.items.append((section, label, "ok", detail))
        if not self.quiet:
            suffix = f"  {detail}" if detail else ""
            print(f"  \033[32m✓\033[0m  {label}{suffix}")

    def fail(self, section, label, detail=""):
        self.failures += 1
        self.items.append((section, label, "fail", detail))
        suffix = f"  {detail}" if detail else ""
        print(f"  \033[31m✗\033[0m  {label}{suffix}")

    def warn(self, section, label, detail=""):
        self.warnings += 1
        self.items.append((section, label, "warn", detail))
        suffix = f"  {detail}" if detail else ""
        print(f"  \033[33m!\033[0m  {label}{suffix}")

    def info(self, section, label, detail=""):
        self.items.append((section, label, "info", detail))
        if not self.quiet:
            suffix = f"  {detail}" if detail else ""
            print(f"     {label}{suffix}")

    def section(self, title):
        self.items.append(("__section__", title, "", ""))
        if not self.quiet or True:   # always print section headers
            print(f"\n[{title}]")

    def to_dict(self):
        return {
            "failures": self.failures,
            "warnings": self.warnings,
            "checks": [
                {"section": s, "label": l, "status": st, "detail": d}
                for s, l, st, d in self.items
                if s != "__section__"
            ]
        }


# ── Check implementations ──────────────────────────────────────────────────────

def check_python(r: Results):
    r.section("Python")
    v = sys.version_info
    ver_str = f"{v.major}.{v.minor}.{v.micro}"
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        r.fail("Python", "Python version", f"{ver_str} — requires 3.10+")
    else:
        r.ok("Python", "Python version", ver_str)
    r.info("Python", "Executable", sys.executable)


def check_packages(r: Results):
    r.section("Python packages")
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg.split(">=")[0].split("[")[0])
            r.ok("packages", pkg)
        except ImportError:
            r.fail("packages", pkg, "not installed — run: pip install -r requirements.txt")

    # youtube-transcript-api is optional
    try:
        importlib.import_module("youtube_transcript_api")
        r.ok("packages", "youtube-transcript-api", "(optional)")
    except ImportError:
        r.warn("packages", "youtube-transcript-api",
               "optional — install if using fetch_youtube_transcript.py")


def check_ollama(r: Results):
    r.section("Ollama")
    try:
        import requests
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        data     = resp.json()
        loaded   = {m["name"] for m in data.get("models", [])}
        r.ok("ollama", "Ollama reachable", OLLAMA_URL)
        r.info("ollama", f"Models loaded: {len(loaded)}")

        for model in REQUIRED_MODELS:
            # Match by prefix (nomic-embed-text matches nomic-embed-text:latest etc.)
            found = any(m.startswith(model.split(":")[0]) for m in loaded)
            if found:
                r.ok("ollama", f"  {model}")
            else:
                r.fail("ollama", f"  {model}", f"not found — run: ollama pull {model}")

        for model in OPTIONAL_MODELS:
            found = any(m.startswith(model.split(":")[0]) for m in loaded)
            if found:
                r.ok("ollama", f"  {model}", "(optional)")
            else:
                r.warn("ollama", f"  {model}",
                       f"optional — run: ollama pull {model}  (needed for verify_beliefs.py)")

    except ImportError:
        r.fail("ollama", "requests package missing — cannot check Ollama")
    except Exception as e:
        r.fail("ollama", "Ollama not reachable", f"{e}  — run: ollama serve")


def check_database(r: Results):
    r.section("Database")

    if not DB_PATH.exists():
        r.fail("database", "memory.db", f"not found at {DB_PATH}")
        r.info("database", "Run: python3 ~/claude_memory/scripts/setup_db.py")
        return

    size_mb = DB_PATH.stat().st_size / (1024 * 1024)
    r.ok("database", "memory.db", f"{size_mb:.1f} MB  at {DB_PATH}")

    try:
        conn = sqlite3.connect(DB_PATH)

        # Table counts
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        r.info("database", f"Tables: {len(tables)}")

        # Record counts for key tables
        for tbl in ("beliefs", "memory_chunks", "questions", "goals", "scout_results"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                r.info("database", f"  {tbl}: {n} rows")
            except Exception:
                pass

        # Schema checks
        for table, expected_cols in CRITICAL_SCHEMA:
            existing = {r2[1] for r2 in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                r.fail("schema", f"schema: {table}", "table missing")
                continue
            missing = [c for c in expected_cols if c not in existing]
            if missing:
                r.fail("schema", f"schema: {table}", f"missing columns: {missing}")
            else:
                r.ok("schema", f"schema: {table}")

        for table, expected_cols in OPTIONAL_SCHEMA:
            existing = {r2[1] for r2 in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                r.warn("schema", f"schema: {table}", "optional table missing (agent not yet built)")
                continue
            missing = [c for c in expected_cols if c not in existing]
            if missing:
                r.fail("schema", f"schema: {table}", f"missing columns: {missing}")
            else:
                r.ok("schema", f"schema: {table}", "(optional)")

        conn.close()

    except Exception as e:
        r.fail("database", "Could not open database", str(e))


def check_embedding_coverage(r: Results):
    r.section("Embedding coverage")
    if not DB_PATH.exists():
        r.warn("embeddings", "Skipped — database not found")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        total    = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE embedding_status = 'embedded'"
        ).fetchone()[0]
        pending  = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE embedding_status = 'pending'"
        ).fetchone()[0]
        conn.close()

        if total == 0:
            r.info("embeddings", "No chunks yet (run ingest.py after first session)")
            return

        pct = embedded / total * 100
        coverage = f"{embedded}/{total} embedded ({pct:.0f}%)  {pending} pending"
        if pct < 70:
            r.warn("embeddings", "Embedding coverage", f"{coverage} — run embed_memories.py")
        else:
            r.ok("embeddings", "Embedding coverage", coverage)

    except Exception as e:
        r.warn("embeddings", "Could not check embeddings", str(e))


def check_processing_queue(r: Results):
    r.section("Processing queue")
    if not DB_PATH.exists():
        r.warn("queue", "Skipped — database not found")
        return
    try:
        conn = sqlite3.connect(DB_PATH)

        pending = conn.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status = 'pending'"
        ).fetchone()[0]
        failed  = conn.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status = 'failed'"
        ).fetchone()[0]
        recent_ok = conn.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status = 'completed' "
            "AND created_at >= date('now', '-7 days')"
        ).fetchone()[0]

        if pending > 0:
            r.warn("queue", "Pending jobs", f"{pending} — run ingest_agent.py --no-jitter")
        else:
            r.ok("queue", "Pending jobs", "0")

        if failed > 0:
            r.warn("queue", "Failed jobs", f"{failed} — check logs/")
        else:
            r.ok("queue", "Failed jobs", "0")

        r.info("queue", f"Completed (last 7 days): {recent_ok}")
        conn.close()

    except Exception as e:
        r.warn("queue", "Could not check processing queue", str(e))


def check_config(r: Results):
    r.section("Config")
    config = _BASE / ".ember_config"
    if not config.exists():
        r.fail("config", ".ember_config", f"not found at {config}")
        r.info("config", "Run: ./setup.sh to create it")
        return

    username = ""
    for line in config.read_text().splitlines():
        if line.startswith("USERNAME=") and not line.startswith("#"):
            username = line.split("=", 1)[1].strip().strip('"')
    if username:
        r.ok("config", ".ember_config", f"USERNAME={username}")
    else:
        r.warn("config", ".ember_config", "found but USERNAME not set")


def check_agents(r: Results):
    r.section("launchd agents")

    if not DAEMONS.exists():
        r.warn("agents", "daemons/ directory not found", str(DAEMONS))
        return

    for plist in AGENT_PLISTS:
        plist_src  = DAEMONS / plist
        plist_dest = LAUNCHD_DIR / plist

        if not plist_src.exists():
            r.warn("agents", plist, "plist file not in daemons/")
            continue

        if plist_dest.exists():
            # Check if loaded in launchd
            try:
                label = plist.replace(".plist", "")
                result = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    r.ok("agents", plist, "installed and loaded")
                else:
                    r.warn("agents", plist, "plist present but not loaded in launchctl")
            except Exception:
                r.ok("agents", plist, "plist installed (launchctl check skipped)")
        else:
            r.warn("agents", plist,
                   f"not installed — run: bash ~/claude_memory/scripts/install_{plist.split('.')[2].replace('-', '_')}.sh")


def check_session_prompt(r: Results):
    r.section("Session prompt")
    start_here    = _BASE / "START_HERE.md"
    context_file  = _BASE / "ember_engine_context.md"
    instructions  = _BASE / "ember_engine_instructions.md"

    for f, label in [(start_here, "START_HERE.md"),
                     (context_file, "ember_engine_context.md"),
                     (instructions, "ember_engine_instructions.md")]:
        if f.exists():
            age_h = (datetime.now().timestamp() - f.stat().st_mtime) / 3600
            age_str = f"{age_h:.0f}h old"
            if age_h > 48 and f.name != "ember_engine_instructions.md":
                r.warn("session", label, f"{age_str} — run generate_session_prompt.py")
            else:
                r.ok("session", label, age_str)
        else:
            if f.name == "ember_engine_instructions.md":
                r.fail("session", label, "not found")
            else:
                r.warn("session", label,
                       "not found — run: python3 ~/claude_memory/scripts/generate_session_prompt.py")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ember-engine system health check"
    )
    parser.add_argument("--quiet", action="store_true",
                        help="Only print failures and warnings")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    r = Results(quiet=args.quiet)

    if not args.json:
        print(f"\n{'='*60}")
        print("  ember-engine doctor")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

    check_python(r)
    check_packages(r)
    check_config(r)
    check_ollama(r)
    check_database(r)
    check_embedding_coverage(r)
    check_processing_queue(r)
    check_agents(r)
    check_session_prompt(r)

    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
        sys.exit(0 if r.failures == 0 else 1)

    print(f"\n{'='*60}")
    if r.failures == 0 and r.warnings == 0:
        print("  \033[32mAll checks passed.\033[0m")
    elif r.failures == 0:
        print(f"  \033[33m{r.warnings} warning(s). No failures.\033[0m")
    else:
        print(f"  \033[31m{r.failures} failure(s)  {r.warnings} warning(s).\033[0m")
    print(f"{'='*60}\n")

    sys.exit(0 if r.failures == 0 else 1)


if __name__ == "__main__":
    main()
